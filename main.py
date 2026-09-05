import asyncio
import json
import logging
import argparse
import re
import time
from typing import Dict, Optional, Set, List, Annotated
from enum import Enum
from contextlib import asynccontextmanager, contextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, StringConstraints, Field
import uvicorn

from greeclimate.discovery import Discovery
from greeclimate.device import Device, HorizontalSwing, VerticalSwing, Mode, FanSpeed, HUMIDITY_MIN, HUMIDITY_MAX
from greeclimate.exceptions import DeviceNotBoundError, DeviceTimeoutError


def pascal_to_snake(name):
    """
    Converts PascalCase to snake_case.
    Example: PascalCase -> pascal_case
    """
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def snake_to_pascal(name):
    """
    Converts snake_case to PascalCase.
    Example: snake_case -> SnakeCase
    """
    return "".join(word.capitalize() for word in name.split("_"))


def to_device_enum(enum_value, device_enum_cls):
    """
    Converts an enum (e.g., Mode) to a DeviceEnum (e.g., DeviceMode) based on its name in snake_case.
    Example: to_device_enum(Mode.Cool, DeviceMode) -> DeviceMode.cool

    A value the API does not know falls back to the first member: reporting an
    approximate state is better than letting the polling loop die on it.
    """
    try:
        return device_enum_cls[pascal_to_snake(enum_value.name)]
    except KeyError:
        fallback = list(device_enum_cls)[0]
        logger.warning(
            "Device reported unknown %s value %s, reporting %s", device_enum_cls.__name__, enum_value, fallback
        )
        return fallback


def from_device_enum(device_enum_value, target_enum_cls):
    """
    Converts a DeviceEnum (e.g., DeviceMode) to an enum (e.g., Mode) based on its PascalCase name.
    Example: from_device_enum(DeviceMode.cool, Mode) -> Mode.Cool

    Returns None when the value has no counterpart, so the caller can reject the
    request instead of quietly sending the device something else.
    """
    try:
        return target_enum_cls[snake_to_pascal(device_enum_value.name)]
    except KeyError:
        return None


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def normalize_mac(mac: str) -> str:
    """Reduce a device MAC to the bare lower case hex the API uses as its key"""
    return mac.replace(":", "").replace("-", "").lower()


# The device stores the target humidity as (value - 15) / 5, so only multiples of 5
# survive a write/read round trip. HUMIDITY_MIN/HUMIDITY_MAX come from greeclimate.
HUMIDITY_STEP = 5


class GreeDevice(Device):
    """A greeclimate device whose state requests can be awaited.

    greeclimate speaks UDP and never waits: `update_state()` puts a request on
    the wire and returns, while the answer arrives later through
    `handle_state_update`. Reading the properties straight after the call
    therefore returns the *previous* answer, and a unit that has stopped
    answering is indistinguishable from one that has nothing new to say.

    This subclass signals the arrival of an answer, so a caller can wait for the
    state it just asked for and find out whether it ever came.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._state_received = asyncio.Event()
        self.last_response: Optional[float] = None

    def handle_state_update(self, **kwargs) -> None:
        super().handle_state_update(**kwargs)
        self.last_response = time.monotonic()
        self._state_received.set()

    @property
    def silent_for(self) -> Optional[float]:
        """Seconds since the unit last answered, or None if it never has"""
        return None if self.last_response is None else time.monotonic() - self.last_response

    @contextmanager
    def rollback_on_failure(self):
        """Undo pending property changes when the command fails to land.

        Setting a property writes it straight into the local property cache, so
        a command that never reaches the unit would otherwise be reported as the
        device's state until the next successful read contradicted it.
        """
        properties = dict(self._properties)
        dirty = list(self._dirty)
        try:
            yield
        except Exception:
            self._properties = properties
            self._dirty = dirty
            raise

    async def refresh_state(self, timeout: float) -> bool:
        """Ask for the current state and wait for it.

        Returns True when an answer arrived, False when the unit stayed silent.
        """
        self._state_received.clear()
        await self.update_state()

        try:
            await asyncio.wait_for(self._state_received.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


# Pydantic models for API
type MacAddress = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{12}$"),
]
type IpAddress = Annotated[
    str,
    StringConstraints(pattern=r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$"),
]


class DeviceMode(Enum):
    """Device operating modes"""

    auto = "auto"
    cool = "cool"
    dry = "dry"
    fan = "fan"
    heat = "heat"


class DeviceFanSpeed(Enum):
    """Device fan speed settings"""

    auto = "auto"
    low = "low"
    medium_low = "medium_low"
    medium = "medium"
    medium_high = "medium_high"
    high = "high"


class DeviceHorizontalSwing(Enum):
    """Device horizontal swing settings"""

    default = "default"
    full_swing = "full_swing"
    left = "left"
    left_center = "left_center"
    center = "center"
    right_center = "right_center"
    right = "right"


class DeviceVerticalSwing(Enum):
    """Device vertical swing settings"""

    default = "default"
    full_swing = "full_swing"
    fixed_upper = "fixed_upper"
    fixed_upper_middle = "fixed_upper_middle"
    fixed_middle = "fixed_middle"
    fixed_lower_middle = "fixed_lower_middle"
    fixed_lower = "fixed_lower"
    swing_upper = "swing_upper"
    swing_upper_middle = "swing_upper_middle"
    swing_middle = "swing_middle"
    swing_lower_middle = "swing_lower_middle"
    swing_lower = "swing_lower"


class DeviceViewModel(BaseModel):
    """Device view model for API responses"""

    # The model is built field by field from the device, so without this the
    # declared types and patterns would never actually be checked.
    model_config = ConfigDict(validate_assignment=True)

    mac: MacAddress = Field("000000000000", description="MAC address of the device")
    ip: IpAddress = Field("0.0.0.0", description="IP address of the device")
    power: bool = Field(False, description="Power state of the device")
    mode: DeviceMode = Field(DeviceMode.auto, description="Operating mode of the device")
    current_temperature: Optional[int] = Field(None, description="Current temperature reported by the device")
    target_temperature: int = Field(16, description="Target temperature set on the device")
    current_humidity: Optional[int] = Field(None, description="Current humidity reported by the device")
    target_humidity: Optional[int] = Field(
        None,
        description=f"Target humidity set on the device ({HUMIDITY_MIN}-{HUMIDITY_MAX}, step {HUMIDITY_STEP}), null when the device does not report a settable value",
    )
    fan_speed: DeviceFanSpeed = Field(DeviceFanSpeed.auto, description="Fan speed setting of the device")
    horizontal_swing: DeviceHorizontalSwing = Field(
        DeviceHorizontalSwing.default, description="Horizontal swing setting of the device"
    )
    vertical_swing: DeviceVerticalSwing = Field(
        DeviceVerticalSwing.default, description="Vertical swing setting of the device"
    )
    turbo: Optional[bool] = Field(None, description="Turbo mode state")
    quiet: Optional[bool] = Field(None, description="Quiet mode state")
    light: Optional[bool] = Field(None, description="Light or backlight state")
    fresh_air: Optional[bool] = Field(None, description="Fresh air mode state")
    xfan: Optional[bool] = Field(None, description="XFan mode state")
    anion: Optional[bool] = Field(None, description="Anion mode state")
    sleep: Optional[bool] = Field(None, description="Sleep mode state")
    power_save: Optional[bool] = Field(None, description="Power save mode state")
    buzzer: Optional[bool] = Field(
        None, description="Buzzer state, when disabled the unit does not beep on each command"
    )
    clean_filter: Optional[bool] = Field(None, description="Clean filter indicator state")
    water_full: Optional[bool] = Field(None, description="Water full indicator state")
    steady_heat: Optional[bool] = Field(None, description="Steady heat mode state")


class DeviceUpdateModel(BaseModel):
    """Device update model for API requests"""

    power: Optional[bool] = Field(None, description="Power state of the device to set")
    mode: Optional[DeviceMode] = Field(None, description="Operating mode of the device to set")
    target_temperature: Optional[int] = Field(
        None, ge=16, le=30, description="Target temperature (16-30, step 1) to set", json_schema_extra={"step": 1}
    )
    target_humidity: Optional[int] = Field(
        None,
        ge=HUMIDITY_MIN,
        le=HUMIDITY_MAX,
        multiple_of=HUMIDITY_STEP,
        description=f"Target humidity ({HUMIDITY_MIN}-{HUMIDITY_MAX}, step {HUMIDITY_STEP}) to set",
        json_schema_extra={"step": HUMIDITY_STEP},
    )
    fan_speed: Optional[DeviceFanSpeed] = Field(None, description="Fan speed setting of the device to set")
    horizontal_swing: Optional[DeviceHorizontalSwing] = Field(
        None, description="Horizontal swing setting of the device to set"
    )
    vertical_swing: Optional[DeviceVerticalSwing] = None
    turbo: Optional[bool] = None
    quiet: Optional[bool] = None
    light: Optional[bool] = None
    fresh_air: Optional[bool] = None
    xfan: Optional[bool] = None
    anion: Optional[bool] = None
    sleep: Optional[bool] = None
    power_save: Optional[bool] = None
    buzzer: Optional[bool] = Field(
        None, description="Buzzer state to set, set to false to silence the beep on each command"
    )
    steady_heat: Optional[bool] = None


class RootResponse(BaseModel):
    """Root endpoint response model"""

    app: str
    version: str
    devices: List[MacAddress]


class ConnectionManager:
    """Manages WebSocket connections and broadcasting messages to clients"""

    def __init__(self, args: argparse.Namespace):
        self.active_connections: Set[WebSocket] = set()
        self.args = args

    async def connect(self, websocket: WebSocket):
        """Accept a WebSocket connection and start tracking it"""
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket):
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

    async def broadcast(self, data: dict):
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


def create_view_model() -> DeviceViewModel:
    """Create a default DeviceViewModel instance"""
    return DeviceViewModel(
        mac="000000000000",
        ip="0.0.0.0",
        power=False,
        mode=DeviceMode.auto,
        current_temperature=None,
        target_temperature=16,
        current_humidity=None,
        target_humidity=None,
        fan_speed=DeviceFanSpeed.auto,
        horizontal_swing=DeviceHorizontalSwing.default,
        vertical_swing=DeviceVerticalSwing.default,
        turbo=None,
        quiet=None,
        light=None,
        fresh_air=None,
        xfan=None,
        anion=None,
        sleep=None,
        power_save=None,
        buzzer=None,
        clean_filter=None,
        water_full=None,
        steady_heat=None,
    )


# How many unanswered state requests before a unit is treated as gone and
# rebuilt. Each miss costs a response timeout plus a polling interval, so this
# is a lot longer in wall clock than it looks.
UNRESPONSIVE_AFTER = 5


class GreeClimateManager:
    """Manages Gree devices, discovery, polling, and state updates"""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.devices: Dict[MacAddress, GreeDevice] = {}
        self.missed_responses: Dict[MacAddress, int] = {}
        self.discovery_lock = asyncio.Lock()
        self.discovery = Discovery()
        self.connection_manager = ConnectionManager(args)
        self.view_models: Dict[MacAddress, DeviceViewModel] = {}
        self.measurement_timestamps: Dict[MacAddress, float] = {}
        self.polling_tasks: Dict[MacAddress, asyncio.Task] = {}

    async def discover_devices(self) -> List[MacAddress]:
        """Discover and bind to Gree devices"""
        async with self.discovery_lock:
            return await self._discover_devices()

    async def _scan(self) -> list:
        """Broadcast a discovery request, returning whatever answered"""
        try:
            return await self.discovery.scan(wait_for=self.args.discovery_timeout)
        except Exception as e:
            logger.error("Discovery failed: %s", e)
            return []

    async def _discover_devices(self) -> List[MacAddress]:
        """Refresh the device roster, holding the discovery lock"""
        logger.info("Starting device discovery...")

        await self.stop_polling()

        found_devices = await self._scan()
        found = {normalize_mac(info.mac): info for info in found_devices}

        # A unit that is still answering is left alone: rebinding it would throw
        # away a working session and its socket for nothing.
        to_bind = [info for mac, info in found.items() if not self._is_usable(mac, info)]

        # Units that neither answered the broadcast nor our last requests are gone.
        for mac in [m for m in self.devices if m not in found and not self._is_responsive(m)]:
            logger.info("Device %s no longer answers and was not rediscovered, dropping it", mac)
            self._forget_device(mac)

        # Units are independent, so bind them at the same time rather than
        # paying the handshake once per device in sequence.
        await asyncio.gather(*(self._bind_device(info) for info in to_bind))

        logger.info("Discovery complete. Found %d devices. Initializing polling tasks...", len(self.devices))

        for mac in list(self.devices):
            try:
                # _bind_device already waited for a fresh state, so build the
                # first view from it rather than asking every unit a second time.
                self.view_models[mac] = await self._get_device_view_model(mac, update_state=False)
                await self._start_polling_for_device(mac)

            except (DeviceNotBoundError, DeviceTimeoutError) as e:
                logger.error("Failed to start polling device %s: %s", mac, e)
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
            device = GreeDevice(device_info, bind_timeout=self.args.response_timeout)
            await device.bind()

            # Binding only proves the unit answered the handshake. Waiting for a
            # real state answer is what tells us it is actually usable.
            if not await device.refresh_state(self.args.response_timeout):
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

    def _forget_device(self, mac: MacAddress) -> None:
        """Drop a unit and release everything held on its behalf"""
        task = self.polling_tasks.pop(mac, None)
        if task is not None:
            task.cancel()

        self._close_quietly(self.devices.pop(mac, None))
        self.view_models.pop(mac, None)
        self.measurement_timestamps.pop(mac, None)
        self.missed_responses.pop(mac, None)

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

    async def _get_device_view_model(self, mac: MacAddress, update_state: bool = True) -> DeviceViewModel:
        """Get device status as dictionary for comparison"""

        view_model = create_view_model()

        if mac not in self.devices:
            logger.error("Device with MAC %s not found", mac)
            return view_model

        device = self.devices[mac]

        if update_state:
            try:
                answered = await device.refresh_state(self.args.response_timeout)
            except (DeviceNotBoundError, DeviceTimeoutError) as e:
                logger.error("Failed to update device %s: %s", mac, e)
                self._record_response(mac, False)
                return self.view_models.get(mac, view_model)

            self._record_response(mac, answered)
            if not answered:
                # Nothing new arrived, so keep reporting the last known state
                # rather than republishing a stale read as if it were fresh.
                return self.view_models.get(mac, view_model)

        view_model.mac = normalize_mac(device.device_info.mac)
        view_model.ip = device.device_info.ip

        if device.power is not None:
            view_model.power = device.power
        if device.mode is not None:
            view_model.mode = to_device_enum(Mode(device.mode), DeviceMode)
        if device.current_temperature is not None:
            view_model.current_temperature = device.current_temperature
        if device.target_temperature is not None:
            view_model.target_temperature = device.target_temperature
        if device.current_humidity is not None:
            view_model.current_humidity = device.current_humidity
        # Devices without a dehumidifier report Dwet=0, which the library decodes as 15 -
        # a value the device would refuse, so report it as "not available" instead.
        if device.target_humidity is not None and HUMIDITY_MIN <= device.target_humidity <= HUMIDITY_MAX:
            view_model.target_humidity = device.target_humidity
        if device.fan_speed is not None:
            view_model.fan_speed = to_device_enum(FanSpeed(device.fan_speed), DeviceFanSpeed)
        if device.horizontal_swing is not None:
            view_model.horizontal_swing = to_device_enum(
                HorizontalSwing(device.horizontal_swing), DeviceHorizontalSwing
            )
        if device.vertical_swing is not None:
            view_model.vertical_swing = to_device_enum(VerticalSwing(device.vertical_swing), DeviceVerticalSwing)
        if hasattr(device, "turbo"):
            view_model.turbo = bool(device.turbo)
        if hasattr(device, "quiet"):
            view_model.quiet = bool(device.quiet)
        if hasattr(device, "light"):
            view_model.light = bool(device.light)
        if hasattr(device, "fresh_air"):
            view_model.fresh_air = bool(device.fresh_air)
        if hasattr(device, "xfan"):
            view_model.xfan = bool(device.xfan)
        if hasattr(device, "anion"):
            view_model.anion = bool(device.anion)
        if hasattr(device, "sleep"):
            view_model.sleep = bool(device.sleep)
        if hasattr(device, "power_save"):
            view_model.power_save = bool(device.power_save)
        if hasattr(device, "buzzer"):
            view_model.buzzer = bool(device.buzzer)
        if hasattr(device, "clean_filter"):
            view_model.clean_filter = bool(device.clean_filter)
        if hasattr(device, "water_full"):
            view_model.water_full = bool(device.water_full)
        if hasattr(device, "steady_heat"):
            view_model.steady_heat = bool(device.steady_heat)

        return view_model

    async def _start_polling_for_device(self, mac: MacAddress):
        """Start polling task for a specific device"""
        if mac in self.polling_tasks:
            self.polling_tasks[mac].cancel()

        self.polling_tasks[mac] = asyncio.create_task(self._poll_device_state(mac))
        logger.info("Started polling for device: %s", mac)

    async def _poll_device_state(self, mac: MacAddress):
        """Poll device state notify on changes"""
        while mac in self.devices:
            try:
                current_state = await self._get_device_view_model(mac)

                if current_state.mac == "000000000000":  # Skip if we couldn't get state
                    await asyncio.sleep(self.args.polling_interval)
                    continue

                missed = self.missed_responses.get(mac, 0)
                if missed and missed % UNRESPONSIVE_AFTER == 0:
                    logger.warning("Device %s has missed %d state requests, rebuilding it", mac, missed)
                    if await self._recover_device(mac):
                        logger.info("Device %s recovered", mac)
                    await asyncio.sleep(self.args.polling_interval)
                    continue

                last_state = self.view_models.get(mac, create_view_model())

                current = current_state.model_dump()
                last = last_state.model_dump()
                changes = {}

                def convert_if_enum(val):
                    if isinstance(val, Enum):
                        return val.name
                    return val

                for key in current:
                    if current[key] != last.get(key):
                        old = convert_if_enum(getattr(last_state, key))
                        new = convert_if_enum(getattr(current_state, key))
                        changes[key] = {"old": old, "new": new}

                if changes:
                    # If changes contain only current_temperature or current_humidity (or both), check last measurement time
                    # if less than 1 minute ago, and the diff is only 1, remove it from the changes
                    if set(changes.keys()).issubset({"current_temperature", "current_humidity"}):
                        last_measurement = self.measurement_timestamps.get(mac, 0)
                        if asyncio.get_event_loop().time() - last_measurement < 60:
                            keys_to_remove = []
                            for key, change in changes.items():
                                # A sensor that starts or stops reporting is a real
                                # change; subtracting None would raise here.
                                if change["old"] is None or change["new"] is None:
                                    continue
                                if abs(change["new"] - change["old"]) <= 1:
                                    keys_to_remove.append(key)
                            for key in keys_to_remove:
                                del changes[key]

                if changes:
                    self.view_models[mac] = current_state

                    # if changes contain current_temperature or current_humidity, update last measurement time
                    if "current_temperature" in changes or "current_humidity" in changes:
                        self.measurement_timestamps[mac] = asyncio.get_event_loop().time()

                    logger.info("State change detected for device %s: %s", mac, changes)
                    try:
                        await self.connection_manager.broadcast({"type": "report", "mac": mac, "data": changes})
                    except Exception as e:
                        logger.error("Failed to send state change notification for %s: %s", mac, e)

                await asyncio.sleep(self.args.polling_interval)

            except asyncio.CancelledError:
                logger.info("Polling cancelled for device: %s", mac)
                break
            except Exception as e:
                logger.error("Error polling device %s: %s", mac, e)
                await asyncio.sleep(self.args.polling_interval * 2)  # Wait longer on error

    async def stop_polling(self):
        """Stop all polling tasks and wait for them to finish"""
        tasks = list(self.polling_tasks.values())
        self.polling_tasks.clear()

        for task in tasks:
            task.cancel()

        # Without waiting, a cancelled loop can still be mid-request while its
        # replacement starts, and both then talk to the same device.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def send_update(self, mac: MacAddress, data: DeviceUpdateModel) -> bool:
        """Send an update to a device, rebuilding it once if it has gone quiet"""

        if mac not in self.devices:
            raise HTTPException(status_code=404, detail="Device not found")

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
                    raise HTTPException(
                        status_code=503, detail=f"Device communication error: {str(e)}"
                    ) from retry_error

            raise HTTPException(status_code=503, detail=f"Device communication error: {str(e)}") from e

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Unexpected error sending command to device %s: %s", mac, e)
            raise HTTPException(status_code=500, detail=f"Command failed: {str(e)}") from e

    async def _apply_and_push(self, device: GreeDevice, data: DeviceUpdateModel) -> bool:
        """Apply the requested fields and send them, confirming they landed.

        Everything is rolled back unless the unit acknowledges the command, so a
        command that never arrived is never reported back as the device's state.
        """
        with device.rollback_on_failure():
            modified = self._apply_fields(device, data)
            await device.push_state_update()

            if not await device.refresh_state(self.args.response_timeout):
                raise DeviceTimeoutError("device did not acknowledge the command")

        return modified

    @staticmethod
    def _apply_fields(device: GreeDevice, data: DeviceUpdateModel) -> bool:
        """Copy the requested fields onto the device, reporting whether any differ"""
        modified = False

        if data.power is not None:
            modified = device.power != data.power or modified
            device.power = data.power
        if data.mode is not None:
            mode = from_device_enum(data.mode, Mode)
            if mode is not None:
                modified = device.mode != mode.value or modified
                device.mode = mode.value
            else:
                raise HTTPException(status_code=422, detail=f"Invalid mode: {data.mode}")
        if data.target_temperature is not None:
            modified = device.target_temperature != data.target_temperature or modified
            device.target_temperature = data.target_temperature
        if data.target_humidity is not None:
            modified = device.target_humidity != data.target_humidity or modified
            device.target_humidity = data.target_humidity
        if data.fan_speed is not None:
            fan_speed = from_device_enum(data.fan_speed, FanSpeed)
            if fan_speed is not None:
                modified = device.fan_speed != fan_speed.value or modified
                device.fan_speed = fan_speed.value
            else:
                raise HTTPException(status_code=422, detail=f"Invalid fan speed: {data.fan_speed}")
        if data.horizontal_swing is not None:
            horizontal_swing = from_device_enum(data.horizontal_swing, HorizontalSwing)
            if horizontal_swing is not None:
                modified = device.horizontal_swing != horizontal_swing.value or modified
                device.horizontal_swing = horizontal_swing.value
            else:
                raise HTTPException(status_code=422, detail=f"Invalid horizontal swing: {data.horizontal_swing}")
        if data.vertical_swing is not None:
            vertical_swing = from_device_enum(data.vertical_swing, VerticalSwing)
            if vertical_swing is not None:
                modified = device.vertical_swing != vertical_swing.value or modified
                device.vertical_swing = vertical_swing.value
            else:
                raise HTTPException(status_code=422, detail=f"Invalid vertical swing: {data.vertical_swing}")
        if data.turbo is not None:
            modified = device.turbo != data.turbo or modified
            device.turbo = data.turbo
        if data.quiet is not None:
            # The device stores quiet as 2 or 0, so compare the same way the view
            # model reports it, otherwise every request looks like a change.
            modified = bool(device.quiet) != data.quiet or modified
            device.quiet = data.quiet
        if data.light is not None:
            modified = device.light != data.light or modified
            device.light = data.light
        if data.fresh_air is not None:
            modified = device.fresh_air != data.fresh_air or modified
            device.fresh_air = data.fresh_air
        if data.xfan is not None:
            modified = device.xfan != data.xfan or modified
            device.xfan = data.xfan
        if data.anion is not None:
            modified = device.anion != data.anion or modified
            device.anion = data.anion
        if data.sleep is not None:
            modified = device.sleep != data.sleep or modified
            device.sleep = data.sleep
        if data.power_save is not None:
            modified = device.power_save != data.power_save or modified
            device.power_save = data.power_save
        if data.buzzer is not None:
            modified = device.buzzer != data.buzzer or modified
            device.buzzer = data.buzzer
        if data.steady_heat is not None:
            modified = device.steady_heat != data.steady_heat or modified
            device.steady_heat = data.steady_heat

        return modified


def get_cli_args() -> argparse.Namespace:
    """Parse the command line arguments"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.description = "Gree Climate API - REST and WebSocket API for controlling Gree air conditioners"

    parser.add_argument(
        "--dev_mode", help="Enable development mode with auto-reload", action="store_true", default=False
    )
    parser.add_argument("--port", help="Port to run the server on", type=int, default=8123)
    parser.add_argument("--discovery_timeout", help="Discovery timeout in seconds", type=int, default=3)
    parser.add_argument("--polling_interval", help="Polling interval in seconds", type=int, default=2)
    parser.add_argument(
        "--response_timeout", help="How long to wait for a device to answer, in seconds", type=float, default=5.0
    )
    parser.add_argument("--verbose", help="Enable verbose logging", action="store_true", default=False)

    args = parser.parse_args()

    return args


cli_args = get_cli_args()

logging.getLogger().setLevel(logging.DEBUG if cli_args.verbose else logging.INFO)
logging.getLogger("greeclimate").setLevel(logging.DEBUG if cli_args.verbose else logging.WARNING)

climate_manager = GreeClimateManager(cli_args)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Lifespan context manager for startup and shutdown tasks"""
    # Startup
    logger.info("Starting Gree Climate API...")
    await climate_manager.discover_devices()
    yield
    # Shutdown
    logger.info("Shutting down Gree Climate API...")
    await climate_manager.stop_polling()


# FastAPI app
app = FastAPI(
    title="Gree Climate API",
    description="REST and WebSocket API for controlling Gree air conditioners with real-time state monitoring",
    version="2.0.0",
    lifespan=lifespan,
)


@app.get(
    "/",
    response_model=RootResponse,
    summary="API info",
    description="Basic API info and list of discovered device MAC addresses.",
)
async def root():
    """Root endpoint providing API info and list of devices"""
    return RootResponse(
        app=app.title,
        version=app.version,
        devices=list(climate_manager.view_models.keys()),
    )


@app.get(
    "/devices",
    response_model=List[DeviceViewModel],
    summary="List devices",
    description="List all discovered Gree devices with their current state.",
)
async def list_devices():
    """List all discovered device view models"""
    return list(climate_manager.view_models.values())


@app.get(
    "/devices/{mac}",
    response_model=DeviceViewModel,
    summary="Get device view",
    description="Get detailed view of a specific Gree device by its MAC address.",
)
async def get_device_view(mac: MacAddress):
    """Get device view"""

    if mac not in climate_manager.view_models:
        raise HTTPException(status_code=404, detail="Device not found")

    return climate_manager.view_models[mac]


@app.patch(
    "/devices/{mac}",
    response_class=Response,
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "No Content. Device updated successfully."},
        304: {"description": "Not Modified. No changes made to the device."},
        404: {"description": "Device not found."},
        422: {"description": "Invalid command."},
        500: {"description": "Internal server error."},
    },
    summary="Send device update",
    description="Send update to a specific Gree device by its MAC address. Only fields that are provided in the request body will be updated. If a field is not provided, it will not be changed. The device will be updated immediately.",
)
async def send_device_update(mac: MacAddress, data: DeviceUpdateModel):
    """Send update to device"""
    if mac not in climate_manager.view_models:
        raise HTTPException(status_code=404, detail="Device not found")
    modified = await climate_manager.send_update(mac, data)

    if not modified:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)

    return Response(status_code=status.HTTP_204_NO_CONTENT)


# W endpointzie:
@app.post(
    "/discover",
    response_model=List[MacAddress],
    responses={200: {"description": "List of MAC addresses of discovered devices."}},
    summary="Rediscover devices",
    description="Rediscover devices and return their MAC addresses. This will stop any ongoing polling and start a new discovery process. Useful for refreshing the device list.",
)
async def rediscover_devices():
    """Rediscover devices and return their MAC addresses"""
    return await climate_manager.discover_devices()


# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time device monitoring and control"""
    await climate_manager.connection_manager.connect(websocket)

    try:
        # Send initial status
        await websocket.send_text(
            json.dumps(
                {
                    "type": "list",
                    "data": [v.model_dump(by_alias=True, mode="json") for v in climate_manager.view_models.values()],
                }
            )
        )

        while True:
            # Listen for incoming messages (commands from client)
            data = await websocket.receive_text()
            message = {}

            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"type": "error", "message": "Invalid JSON format"}))
                continue

            message_id = message.get("message_id")

            try:
                message_type = message.get("type")

                if message_type == "update":
                    mac = message.get("mac")
                    if not mac or mac not in climate_manager.view_models:
                        await websocket.send_text(
                            json.dumps(
                                {"type": "error", "message_id": message_id, "message": "Invalid or missing MAC address"}
                            )
                        )
                        continue

                    command = DeviceUpdateModel(**message.get("data", {}))

                    modified = await climate_manager.send_update(mac, command)

                    if not modified:
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "not_changed",
                                    "mac": mac,
                                    "message_id": message_id,
                                    "message": "No changes made to the device by last command",
                                }
                            )
                        )
                else:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "error",
                                "message_id": message_id,
                                "message": f"Unsupported message type: {message_type}",
                            }
                        )
                    )
            except WebSocketDisconnect:
                # The client went away, there is nobody left to report the error to
                raise
            except Exception as e:
                await websocket.send_text(json.dumps({"type": "error", "message_id": message_id, "message": str(e)}))

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    finally:
        # Runs for a clean disconnect and for any unexpected error alike, so a
        # dead connection is never left behind in the broadcast set.
        climate_manager.connection_manager.disconnect(websocket)


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=cli_args.port,
        reload=cli_args.dev_mode,
        log_level="debug" if cli_args.verbose else "info",
        access_log=cli_args.verbose,
    )
