import asyncio
import json
import logging
import argparse
import re
from typing import Dict, Optional, Set, List, Annotated
from enum import Enum
from threading import Lock
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response, status
from pydantic import BaseModel, StringConstraints, Field
import uvicorn

from greeclimate.discovery import Discovery
from greeclimate.device import Device, HorizontalSwing, VerticalSwing, Mode, FanSpeed, HUMIDITY_MIN, HUMIDITY_MAX
from greeclimate.exceptions import DeviceNotBoundError, DeviceTimeoutError

def pascal_to_snake(name):
    """
    Converts PascalCase to snake_case.
    Example: PascalCase -> pascal_case
    """
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()

def snake_to_pascal(name):
    """
    Converts snake_case to PascalCase.
    Example: snake_case -> SnakeCase
    """
    return ''.join(word.capitalize() for word in name.split('_'))

def to_device_enum(enum_value, device_enum_cls):
    """
    Converts an enum (e.g., Mode) to a DeviceEnum (e.g., DeviceMode) based on its name in snake_case.
    Example: to_device_enum(Mode.Cool, DeviceMode) -> DeviceMode.cool
    """
    val = device_enum_cls[pascal_to_snake(enum_value.name)]
    if val is None:
        return list(device_enum_cls)[0]

    return val

def from_device_enum(device_enum_value, target_enum_cls):
    """
    Converts a DeviceEnum (e.g., DeviceMode) to an enum (e.g., Mode) based on its PascalCase name.
    Example: from_device_enum(DeviceMode.cool, Mode) -> Mode.Cool
    """
    val = target_enum_cls[snake_to_pascal(device_enum_value.name)]
    if val is None:
        return list(target_enum_cls)[0]

    return val

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# The device stores the target humidity as (value - 15) / 5, so only multiples of 5
# survive a write/read round trip. HUMIDITY_MIN/HUMIDITY_MAX come from greeclimate.
HUMIDITY_STEP = 5

# Pydantic models for API
type MacAddress = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{12}$"),]
type IpAddress = Annotated[str, StringConstraints(pattern=r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$"),]
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
    mac: MacAddress = Field("000000000000", description="MAC address of the device")
    ip: IpAddress = Field("0.0.0.0", description="IP address of the device")
    power: bool = Field(False, description="Power state of the device")
    mode: DeviceMode = Field(DeviceMode.auto, description="Operating mode of the device")
    current_temperature: Optional[int] = Field(None, description="Current temperature reported by the device")
    target_temperature: int = Field(16, description="Target temperature set on the device")
    current_humidity: Optional[int] = Field(None, description="Current humidity reported by the device")
    target_humidity: Optional[int] = Field(
        None,
        description=f"Target humidity set on the device ({HUMIDITY_MIN}-{HUMIDITY_MAX}, step {HUMIDITY_STEP}), null when the device does not report a settable value"
    )
    fan_speed: DeviceFanSpeed = Field(DeviceFanSpeed.auto, description="Fan speed setting of the device")
    horizontal_swing: DeviceHorizontalSwing = Field(DeviceHorizontalSwing.default, description="Horizontal swing setting of the device")
    vertical_swing: DeviceVerticalSwing = Field(DeviceVerticalSwing.default, description="Vertical swing setting of the device")
    turbo: Optional[bool] = Field(None, description="Turbo mode state")
    quiet: Optional[bool] = Field(None, description="Quiet mode state")
    light: Optional[bool] = Field(None, description="Light or backlight state")
    fresh_air: Optional[bool] = Field(None, description="Fresh air mode state")
    xfan: Optional[bool] = Field(None, description="XFan mode state")
    anion: Optional[bool] = Field(None, description="Anion mode state")
    sleep: Optional[bool] = Field(None, description="Sleep mode state")
    power_save: Optional[bool] = Field(None, description="Power save mode state")
    buzzer: Optional[bool] = Field(None, description="Buzzer state, when disabled the unit does not beep on each command")
    clean_filter: Optional[bool] = Field(None, description="Clean filter indicator state")
    water_full: Optional[bool] = Field(None, description="Water full indicator state")
    steady_heat: Optional[bool] = Field(None, description="Steady heat mode state")

class DeviceUpdateModel(BaseModel):
    """Device update model for API requests"""
    power: Optional[bool] = Field(None, description="Power state of the device to set")
    mode: Optional[DeviceMode] = Field(None, description="Operating mode of the device to set")
    target_temperature: Optional[int] = Field(
        None,
        ge=16,
        le=30,
        description="Target temperature (16-30, step 1) to set",
        json_schema_extra={"step": 1}
    )
    target_humidity: Optional[int] = Field(
        None,
        ge=HUMIDITY_MIN,
        le=HUMIDITY_MAX,
        multiple_of=HUMIDITY_STEP,
        description=f"Target humidity ({HUMIDITY_MIN}-{HUMIDITY_MAX}, step {HUMIDITY_STEP}) to set",
        json_schema_extra={"step": HUMIDITY_STEP}
    )
    fan_speed: Optional[DeviceFanSpeed] = Field(None, description="Fan speed setting of the device to set")
    horizontal_swing: Optional[DeviceHorizontalSwing] = Field(None, description="Horizontal swing setting of the device to set")
    vertical_swing: Optional[DeviceVerticalSwing] = None
    turbo: Optional[bool] = None
    quiet: Optional[bool] = None
    light: Optional[bool] = None
    fresh_air: Optional[bool] = None
    xfan: Optional[bool] = None
    anion: Optional[bool] = None
    sleep: Optional[bool] = None
    power_save: Optional[bool] = None
    buzzer: Optional[bool] = Field(None, description="Buzzer state to set, set to false to silence the beep on each command")
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
        self.lock = Lock()
        self.args = args

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        with self.lock:
            self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket):
        with self.lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)

    async def broadcast(self, data: dict):
        disconnected = set()
        with self.lock:
            for connection in self.active_connections:
                try:
                    await connection.send_text(json.dumps(data))
                except (WebSocketDisconnect, RuntimeError):
                    # If the connection is closed or invalid, remove it
                    disconnected.add(connection)
                except Exception as e:
                    client_host = connection.client.host if connection.client else "unknown"
                    logger.error("Error sending data to WebSocket %s: %s", client_host, e)

            for conn in disconnected:
                self.active_connections.discard(conn)

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

class GreeClimateManager:
    """Manages Gree devices, discovery, polling, and state updates"""
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.devices: Dict[MacAddress, Device] = {}
        self.discovery = Discovery()
        self.connection_manager = ConnectionManager(args)
        self.view_models: Dict[MacAddress, DeviceViewModel] = {}
        self.measurement_timestamps: Dict[MacAddress, float] = {}
        self.polling_tasks: Dict[MacAddress, asyncio.Task] = {}

    async def discover_devices(self) -> List[MacAddress]:
        """Discover and bind to Gree devices"""
        logger.info("Starting device discovery...")

        await self.stop_polling()

        try:
            found_devices = await self.discovery.scan(wait_for=self.args.discovery_timeout)
            for device_info in found_devices:
                try:
                    device = Device(device_info)
                    await device.bind()

                    await asyncio.sleep(2)  # Wait a bit before starting polling

                    await device.update_state()

                    self.devices[device_info.mac] = device
                    logger.info("Device bound: %s at %s", device_info.name, device_info.ip)

                except (DeviceNotBoundError, DeviceTimeoutError) as e:
                    logger.error("Failed to bind device %s: %s", device_info.ip, e)
                except Exception as e:
                    logger.error("Unexpected error binding device %s: %s", device_info.ip, e)

        except Exception as e:
            logger.error("Discovery failed: %s", e)

        logger.info("Discovery complete. Found %d devices. Initializing polling tasks...", len(self.devices))
        await asyncio.sleep(5)  # Wait a bit before starting polling

        try:
            for mac in self.devices:
                try:
                    current_state = await self._get_device_view_model(mac)
                    self.view_models[mac] = current_state

                    await self._start_polling_for_device(mac)

                except (DeviceNotBoundError, DeviceTimeoutError) as e:
                    logger.error("Failed to start polling device %s: %s", mac, e)
                except Exception as e:
                    logger.error("Unexpected error start polling device %s: %s", mac, e)

        except Exception as e:
            logger.error("Start polling failed: %s", e)

        logger.info("Polling init finished.")

        return list(self.view_models.keys())

    async def _get_device_view_model(self, mac: MacAddress, update_state: bool = True) -> DeviceViewModel:
        """Get device status as dictionary for comparison"""

        view_model = create_view_model()

        if mac not in self.devices:
            logger.error("Device with MAC %s not found", mac)
            return view_model

        device = self.devices[mac]

        if update_state:
            try:
                await device.update_state()
            except (DeviceNotBoundError, DeviceTimeoutError) as e:
                logger.error("Failed to update device %s: %s", mac, e)
                return self.view_models.get(mac, view_model)

        view_model.mac = device.device_info.mac.replace(":", "")
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
            view_model.horizontal_swing = to_device_enum(HorizontalSwing(device.horizontal_swing), DeviceHorizontalSwing)
        if device.vertical_swing is not None:
            view_model.vertical_swing = to_device_enum(VerticalSwing(device.vertical_swing), DeviceVerticalSwing)
        if hasattr(device, 'turbo'):
            view_model.turbo = bool(device.turbo)
        if hasattr(device, 'quiet'):
            view_model.quiet = bool(device.quiet)
        if hasattr(device, 'light'):
            view_model.light = bool(device.light)
        if hasattr(device, 'fresh_air'):
            view_model.fresh_air = bool(device.fresh_air)
        if hasattr(device, 'xfan'):
            view_model.xfan = bool(device.xfan)
        if hasattr(device, 'anion'):
            view_model.anion = bool(device.anion)
        if hasattr(device, 'sleep'):
            view_model.sleep = bool(device.sleep)
        if hasattr(device, 'power_save'):
            view_model.power_save = bool(device.power_save)
        if hasattr(device, 'buzzer'):
            view_model.buzzer = bool(device.buzzer)
        if hasattr(device, 'clean_filter'):
            view_model.clean_filter = bool(device.clean_filter)
        if hasattr(device, 'water_full'):
            view_model.water_full = bool(device.water_full)
        if hasattr(device, 'steady_heat'):
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

                last_state = self.view_models.get(mac, create_view_model())

                current = current_state.dict()
                last = last_state.dict()
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
                            for key in changes:
                                if abs(changes[key]["new"] - changes[key]["old"]) <= 1:
                                    keys_to_remove.append(key)
                            for key in keys_to_remove:
                                del changes[key]


                if changes:
                    self.view_models[mac] = current_state

                    #if changes contain current_temperature or current_humidity, update last measurement time
                    if "current_temperature" in changes or "current_humidity" in changes:
                        self.measurement_timestamps[mac] = asyncio.get_event_loop().time()

                    logger.info("State change detected for device %s: %s", mac, changes)
                    try:
                        await self.connection_manager.broadcast(
                            {
                                "type": "report",
                                "mac": mac,
                                "data": changes
                            }
                        )
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
        """Stop all polling tasks"""
        for mac, task in self.polling_tasks.items():
            task.cancel()
        self.polling_tasks.clear()

    async def send_update(self, mac: MacAddress, data: DeviceUpdateModel) -> bool:
        """Send update to device"""

        device = self.devices[mac]
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
            modified = device.quiet != data.quiet or modified
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

        try:
            await device.push_state_update()

        except (DeviceNotBoundError, DeviceTimeoutError) as e:
            logger.error("Failed to send command to device %s: %s", mac, e)
            # Try to rebind and retry
            try:
                await device.bind()
                await device.push_state_update()
            except Exception as rebind_error:
                logger.error("Failed to rebind and retry command for device %s: %s", mac, rebind_error)
                raise HTTPException(status_code=503, detail=f"Device communication error: {str(e)}") from rebind_error
        except Exception as e:
            logger.error("Unexpected error sending command to device %s: %s", mac, e)
            raise HTTPException(status_code=500, detail=f"Command failed: {str(e)}") from e

        return modified

def get_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.description = "Gree Climate API - REST and WebSocket API for controlling Gree air conditioners"

    parser.add_argument("--dev_mode", help="Enable development mode with auto-reload", action="store_true", default=False)
    parser.add_argument("--port", help="Port to run the server on", type=int, default=8123)
    parser.add_argument("--discovery_timeout", help="Discovery timeout in seconds", type=int, default=3)
    parser.add_argument("--polling_interval", help="Polling interval in seconds", type=int, default=2)
    parser.add_argument("--verbose", help="Enable verbose logging", action="store_true", default=False)

    args = parser.parse_args()

    return args

cli_args = get_cli_args()

logging.getLogger().setLevel(logging.DEBUG if cli_args.verbose else logging.INFO )
logging.getLogger("greeclimate").setLevel(logging.DEBUG if cli_args.verbose else logging.WARNING )

climate_manager = GreeClimateManager(cli_args)

@asynccontextmanager
async def lifespan(app: FastAPI):
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
    lifespan=lifespan
)

@app.get("/", response_model=RootResponse, summary="API info", description="Basic API info and list of discovered device MAC addresses.")
async def root():
    """Root endpoint providing API info and list of devices"""
    return RootResponse(
        app=app.title,
        version=app.version,
        devices=list(climate_manager.view_models.keys()),
    )

@app.get("/devices", response_model=List[DeviceViewModel], summary="List devices", description="List all discovered Gree devices with their current state.")
async def list_devices():
    """List all discovered device view models"""
    return list(climate_manager.view_models.values())

@app.get("/devices/{mac}", response_model=DeviceViewModel, summary="Get device view", description="Get detailed view of a specific Gree device by its MAC address.")
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
    responses={
        200: {"description": "List of MAC addresses of discovered devices."}
    },
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
        await websocket.send_text(json.dumps({
            "type": "list",
            "data": [v.model_dump(by_alias=True, mode='json') for v in climate_manager.view_models.values()]
        }))

        while True:
            # Listen for incoming messages (commands from client)
            data = await websocket.receive_text()
            message = {}

            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Invalid JSON format"
                }))
                continue

            message_id = message.get("message_id")

            try:
                if message.get("type") == "update":
                    mac = message.get("mac")
                    if not mac or mac not in climate_manager.view_models:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message_id": message_id,
                            "message": "Invalid or missing MAC address"
                        }))
                        continue

                    command = DeviceUpdateModel(**message.get("data", {}))

                    modified = await climate_manager.send_update(mac, command)

                    if not modified:
                        await websocket.send_text(json.dumps({
                            "type": "not_changed",
                            "mac": mac,
                            "message_id": message_id,
                            "message": "No changes made to the device by last command"
                        }))
            except Exception as e:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message_id": message_id,
                    "message": str(e)
                }))

    except WebSocketDisconnect:
        climate_manager.connection_manager.disconnect(websocket)

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=cli_args.port,
        reload=cli_args.dev_mode,
        log_level= "debug" if cli_args.verbose else "info",
        access_log=cli_args.verbose
    )
