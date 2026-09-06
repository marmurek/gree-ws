"""Device discovery, polling and command handling."""

import asyncio
import json
import logging
from enum import Enum
from typing import Dict, List, Optional, Set

from fastapi import WebSocket, WebSocketDisconnect
from greeclimate.discovery import Discovery
from greeclimate.exceptions import DeviceNotBoundError, DeviceTimeoutError

from gree_ws.config import Settings
from gree_ws.conversions import normalize_mac
from gree_ws.device import GreeDevice
from gree_ws.errors import DeviceNotFound, DeviceUnavailable, InvalidCommand
from gree_ws.fields import DEVICE_FIELDS, WRITABLE_FIELDS
from gree_ws.models import DeviceUpdateModel, DeviceViewModel, MacAddress, create_view_model

logger = logging.getLogger(__name__)

# How many unanswered state requests before a unit is treated as gone and
# rebuilt. Each miss costs a response timeout plus a polling interval, so this
# is a lot longer in wall clock than it looks.
UNRESPONSIVE_AFTER = 5

# A one degree wobble reported again within this many seconds of the last
# reading is sensor noise, not a change worth telling anyone about.
JITTER_WINDOW = 60


class ConnectionManager:
    """Manages WebSocket connections and broadcasting messages to clients"""

    def __init__(self) -> None:
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept a WebSocket connection and start tracking it"""
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        """Stop tracking a WebSocket connection"""
        self.active_connections.discard(websocket)

    async def _send_to(self, connection: WebSocket, payload: str) -> Optional[WebSocket]:
        """Send one payload, returning the connection if it has to be dropped"""
        try:
            await connection.send_text(payload)
        except (WebSocketDisconnect, RuntimeError):
            # The connection is closed or invalid
            return connection
        except Exception as e:
            client_host = connection.client.host if connection.client else "unknown"
            logger.error("Error sending data to WebSocket %s: %s", client_host, e)
        return None

    async def broadcast(self, data: dict) -> None:
        """Send data to every connected client, dropping the ones that went away"""
        payload = json.dumps(data)

        # Iterate a snapshot, never the live set: a client connecting or leaving
        # during the sends would otherwise mutate it mid-iteration. No lock is
        # needed around the set itself - the event loop is single threaded and
        # none of the mutations here await, so each one is already atomic.
        connections = list(self.active_connections)
        if not connections:
            return

        results = await asyncio.gather(*(self._send_to(c, payload) for c in connections))

        for connection in results:
            if connection is not None:
                self.active_connections.discard(connection)


class GreeClimateManager:
    """Manages Gree devices, discovery, polling, and state updates"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.devices: Dict[MacAddress, GreeDevice] = {}
        self.missed_responses: Dict[MacAddress, int] = {}
        self.discovery_lock = asyncio.Lock()
        self.discovery = Discovery()
        self.connection_manager = ConnectionManager()
        self.view_models: Dict[MacAddress, DeviceViewModel] = {}
        self.measurement_timestamps: Dict[MacAddress, float] = {}
        self.polling_tasks: Dict[MacAddress, asyncio.Task] = {}

    # Discovery and the device roster

    async def discover_devices(self) -> List[MacAddress]:
        """Discover and bind to Gree devices"""
        async with self.discovery_lock:
            return await self._discover_devices()

    async def _scan(self) -> list:
        """Broadcast a discovery request, returning whatever answered"""
        try:
            return await self.discovery.scan(wait_for=self.settings.discovery_timeout)
        except Exception as e:
            logger.error("Discovery failed: %s", e)
            return []

    async def _discover_devices(self) -> List[MacAddress]:
        """Refresh the device roster, holding the discovery lock"""
        logger.info("Starting device discovery...")

        await self.stop_polling()

        found = {normalize_mac(info.mac): info for info in await self._scan()}

        # A unit that is still answering is left alone: rebinding it would throw
        # away a working session and its socket for nothing.
        to_bind = [info for mac, info in found.items() if not self._is_usable(mac, info)]

        # A unit that answered neither the broadcast nor our last requests keeps
        # its place in the roster, reported as unavailable. It may be switched
        # off rather than gone, and the polling loop keeps trying to rebuild it.

        # Units are independent, so bind them at the same time rather than
        # paying the handshake once per device in sequence.
        await asyncio.gather(*(self._bind_device(info) for info in to_bind))

        logger.info("Discovery complete. Found %d devices. Initializing polling tasks...", len(self.devices))

        for mac in list(self.devices):
            try:
                # _bind_device already waited for a fresh state, so build the
                # first view from it rather than asking every unit a second time.
                self.view_models[mac] = await self.device_view(mac, update_state=False)
                await self._start_polling_for_device(mac)
            except Exception as e:
                logger.error("Unexpected error start polling device %s: %s", mac, e)

        logger.info("Polling init finished.")

        return list(self.view_models.keys())

    async def _bind_device(self, device_info) -> None:
        """Bind one unit and confirm it reports its state before keeping it"""
        device = None
        try:
            # greeclimate always tries CipherV1 first and only falls back to
            # CipherV2 after bind_timeout, so a newer unit costs a full timeout
            # of dead air at startup. Its default is 10s; ours is the same wait
            # we allow for any other answer from a unit.
            device = GreeDevice(device_info, bind_timeout=self.settings.response_timeout)
            await device.bind()

            # Binding only proves the unit answered the handshake. Waiting for a
            # real state answer is what tells us it is actually usable.
            if not await device.refresh_state(self.settings.response_timeout):
                logger.error("Device %s bound but never reported its state", device_info.ip)
                device.close()
                return

            mac = normalize_mac(device_info.mac)
            self._close_quietly(self.devices.get(mac))
            self.devices[mac] = device
            self.missed_responses[mac] = 0
            logger.info("Device bound: %s at %s", device_info.name, device_info.ip)

        except (DeviceNotBoundError, DeviceTimeoutError) as e:
            logger.error("Failed to bind device %s: %s", device_info.ip, e)
            self._close_quietly(device)
        except Exception as e:
            logger.error("Unexpected error binding device %s: %s", device_info.ip, e)
            self._close_quietly(device)

    @staticmethod
    def _close_quietly(device: Optional[GreeDevice]) -> None:
        """Release a device's socket, ignoring one that was never opened"""
        if device is None:
            return
        try:
            device.close()
        except Exception as e:  # the socket may never have been created
            logger.debug("Closing device socket failed: %s", e)

    def _is_responsive(self, mac: MacAddress) -> bool:
        """Has this unit answered us recently enough to be considered alive"""
        return mac in self.devices and self.missed_responses.get(mac, 0) < UNRESPONSIVE_AFTER

    def _is_usable(self, mac: MacAddress, device_info) -> bool:
        """Can the device we already hold keep serving this discovered unit"""
        device = self.devices.get(mac)
        if device is None or not self._is_responsive(mac):
            return False
        # A unit that moved needs a new socket: the old one is bound to its old address.
        return device.device_info.ip == device_info.ip

    async def _recover_device(self, mac: MacAddress) -> bool:
        """Rebuild a unit that stopped answering.

        Rebinding the existing object cannot work: greeclimate never clears its
        `ready` event, so a second bind() returns instantly without waiting for
        the handshake, having already replaced the session key with the generic
        one. The only way back is a new object, with a new socket, a freshly
        negotiated key and whatever address the unit came back on.
        """
        old = self.devices.get(mac)
        if old is None:
            return False

        async with self.discovery_lock:
            device_info = old.device_info
            for info in await self._scan():
                if normalize_mac(info.mac) == mac:
                    if info.ip != device_info.ip:
                        logger.info("Device %s moved from %s to %s", mac, device_info.ip, info.ip)
                    device_info = info
                    break

            logger.info("Rebuilding device %s at %s", mac, device_info.ip)
            await self._bind_device(device_info)

        return self._is_responsive(mac) and self.devices.get(mac) is not old

    def _record_response(self, mac: MacAddress, answered: bool) -> None:
        """Track whether a unit is still talking to us, and say so once"""
        missed = self.missed_responses.get(mac, 0)

        if answered:
            if missed:
                logger.info("Device %s is answering again after %d missed requests", mac, missed)
            self.missed_responses[mac] = 0
            return

        self.missed_responses[mac] = missed + 1
        if missed == 0:
            device = self.devices.get(mac)
            silence = device.silent_for if device else None
            logger.warning(
                "Device %s did not answer a state request (silent for %s)",
                mac,
                "never answered" if silence is None else f"{silence:.0f}s",
            )

    # Reading device state

    async def device_view(self, mac: MacAddress, update_state: bool = True) -> DeviceViewModel:
        """Read a device and express its state as the API reports it"""
        device = self.devices.get(mac)
        if device is None:
            logger.error("Device with MAC %s not found", mac)
            return create_view_model()

        if update_state:
            try:
                answered = await device.refresh_state(self.settings.response_timeout)
            except (DeviceNotBoundError, DeviceTimeoutError) as e:
                logger.error("Failed to update device %s: %s", mac, e)
                self._record_response(mac, False)
                return self._last_known_view(mac)

            self._record_response(mac, answered)
            if not answered:
                # Nothing new arrived, so keep reporting the last known state
                # rather than republishing a stale read as if it were fresh.
                return self._last_known_view(mac)

        view_model = create_view_model()
        view_model.mac = normalize_mac(device.device_info.mac)
        view_model.ip = device.device_info.ip
        view_model.available = self._is_responsive(mac)

        for field in DEVICE_FIELDS:
            value = field.read(device)
            if value is not None:
                setattr(view_model, field.name, value)

        return view_model

    def _last_known_view(self, mac: MacAddress) -> DeviceViewModel:
        """What was last read from a unit, with its availability brought up to date.

        Availability is an ordinary field, so a unit going quiet or coming back
        reaches clients through the same change report as any other value.
        """
        published = self.view_models.get(mac)
        view_model = published.model_copy() if published is not None else create_view_model()

        device = self.devices.get(mac)
        if published is None and device is not None:
            view_model.mac = normalize_mac(device.device_info.mac)
            view_model.ip = device.device_info.ip

        view_model.available = self._is_responsive(mac)
        return view_model

    # Polling

    async def _start_polling_for_device(self, mac: MacAddress) -> None:
        """Start polling task for a specific device"""
        if mac in self.polling_tasks:
            self.polling_tasks[mac].cancel()

        self.polling_tasks[mac] = asyncio.create_task(self._poll_device_state(mac))
        logger.info("Started polling for device: %s", mac)

    def _changes_since(self, mac: MacAddress, current: DeviceViewModel) -> dict:
        """Compare a fresh reading against the last one that was published"""
        last = self.view_models.get(mac, create_view_model())

        def as_reported(value):
            return value.name if isinstance(value, Enum) else value

        changes = {
            name: {"old": as_reported(getattr(last, name)), "new": as_reported(getattr(current, name))}
            for name in current.model_dump()
            if getattr(current, name) != getattr(last, name)
        }

        return self._without_jitter(mac, changes)

    def _without_jitter(self, mac: MacAddress, changes: dict) -> dict:
        """Drop a single degree of sensor wobble seen soon after a real reading"""
        if not changes or not set(changes) <= {"current_temperature", "current_humidity"}:
            return changes

        since_measurement = asyncio.get_event_loop().time() - self.measurement_timestamps.get(mac, 0)
        if since_measurement >= JITTER_WINDOW:
            return changes

        return {
            name: change
            for name, change in changes.items()
            # A sensor that starts or stops reporting is a real change, and
            # subtracting None would raise here.
            if change["old"] is None or change["new"] is None or abs(change["new"] - change["old"]) > 1
        }

    async def _publish_changes(self, mac: MacAddress, current_state: DeviceViewModel) -> None:
        """Store a new reading and tell clients what moved"""
        changes = self._changes_since(mac, current_state)
        if not changes:
            return

        self.view_models[mac] = current_state

        if "current_temperature" in changes or "current_humidity" in changes:
            self.measurement_timestamps[mac] = asyncio.get_event_loop().time()

        logger.info("State change detected for device %s: %s", mac, changes)
        try:
            await self.connection_manager.broadcast({"type": "report", "mac": mac, "data": changes})
        except Exception as e:
            logger.error("Failed to send state change notification for %s: %s", mac, e)

    async def _poll_device_state(self, mac: MacAddress) -> None:
        """Poll device state notify on changes"""
        while mac in self.devices:
            try:
                current_state = await self.device_view(mac)

                if current_state.mac == "000000000000":  # Skip if we couldn't get state
                    await asyncio.sleep(self.settings.polling_interval)
                    continue

                # Report before attempting a rebuild, so clients learn that a unit
                # went offline at the moment it did rather than after the retry.
                await self._publish_changes(mac, current_state)

                missed = self.missed_responses.get(mac, 0)
                if missed and missed % UNRESPONSIVE_AFTER == 0:
                    logger.warning("Device %s has missed %d state requests, rebuilding it", mac, missed)
                    if await self._recover_device(mac):
                        logger.info("Device %s recovered", mac)

                await asyncio.sleep(self.settings.polling_interval)

            except asyncio.CancelledError:
                logger.info("Polling cancelled for device: %s", mac)
                break
            except Exception as e:
                logger.error("Error polling device %s: %s", mac, e)
                await asyncio.sleep(self.settings.polling_interval * 2)  # Wait longer on error

    async def stop_polling(self) -> None:
        """Stop all polling tasks and wait for them to finish"""
        tasks = list(self.polling_tasks.values())
        self.polling_tasks.clear()

        for task in tasks:
            task.cancel()

        # Without waiting, a cancelled loop can still be mid-request while its
        # replacement starts, and both then talk to the same device.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # Commands

    async def send_update(self, mac: MacAddress, data: DeviceUpdateModel) -> bool:
        """Send an update to a device, rebuilding it once if it has gone quiet"""
        if mac not in self.devices:
            raise DeviceNotFound(f"No device registered as {mac}")

        try:
            return await self._apply_and_push(self.devices[mac], data)

        except (DeviceNotBoundError, DeviceTimeoutError) as e:
            logger.error("Failed to send command to device %s: %s", mac, e)

            # The unit stopped answering. Rebuild it and re-apply the command to
            # the new object; the old one's pending changes went with it.
            if await self._recover_device(mac):
                try:
                    return await self._apply_and_push(self.devices[mac], data)
                except Exception as retry_error:
                    logger.error("Command failed again after rebuilding %s: %s", mac, retry_error)
                    raise DeviceUnavailable(f"Device communication error: {e}") from retry_error

            raise DeviceUnavailable(f"Device communication error: {e}") from e

    async def _apply_and_push(self, device: GreeDevice, data: DeviceUpdateModel) -> bool:
        """Apply the requested fields and send them, confirming they landed.

        Everything is rolled back unless the unit acknowledges the command, so a
        command that never arrived is never reported back as the device's state.
        """
        with device.rollback_on_failure():
            modified = self._apply_fields(device, data)
            await device.push_state_update()

            if not await device.refresh_state(self.settings.response_timeout):
                raise DeviceTimeoutError("device did not acknowledge the command")

        return modified

    @staticmethod
    def _apply_fields(device: GreeDevice, data: DeviceUpdateModel) -> bool:
        """Copy the requested fields onto the device, reporting whether any differ"""
        modified = False

        for field in WRITABLE_FIELDS:
            requested = getattr(data, field.name)
            if requested is None:
                continue

            if field.to_device(requested) is None:  # type: ignore[misc]
                raise InvalidCommand(f"Invalid {field.name}: {requested}")

            # Compare as the API reports it, so a device that stores a flag as
            # 2 and 0 is not read as different from the boolean that was asked for.
            modified = field.read(device) != requested or modified
            field.write(device, requested)

        return modified
