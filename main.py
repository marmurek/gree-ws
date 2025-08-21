import asyncio
import json
import logging
import argparse
from typing import Dict, Optional, Set, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response, status
from pydantic import BaseModel, constr, Field
import uvicorn

from greeclimate.discovery import Discovery
from greeclimate.device import Device, HorizontalSwing, VerticalSwing, Mode, FanSpeed
from greeclimate.exceptions import DeviceNotBoundError, DeviceTimeoutError
from threading import Lock
import re

from enum import Enum
from typing import Type, Union

def pascal_to_snake(name):
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()

def snake_to_pascal(name):
    return ''.join(word.capitalize() for word in name.split('_'))

def snake_case_enum(enum_cls):
    """Tworzy nowy Enum z wartościami w snake_case na podstawie enum_cls."""
    members = {pascal_to_snake(e.name): pascal_to_snake(e.name) for e in enum_cls}
    members['unknown'] = 'unknown'
    return Enum(f"Device{enum_cls.__name__}", members)

def to_device_enum(enum_value, device_enum_cls):
    """
    Zamienia enum (np. Mode) na DeviceEnum (np. DeviceMode) na podstawie nazwy w snake_case.
    Przykład: to_device_enum(Mode.Cool, DeviceMode) -> DeviceMode.cool
    """
    return device_enum_cls[pascal_to_snake(enum_value.name)]

def from_device_enum(device_enum_value, target_enum_cls):
    """
    Zamienia DeviceEnum (np. DeviceMode) na enum (np. Mode) na podstawie nazwy w PascalCase.
    Przykład: from_device_enum(DeviceMode.cool, Mode) -> Mode.Cool
    """
    return target_enum_cls[snake_to_pascal(device_enum_value.name)]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Pydantic models for API
MacAddress = constr(pattern=r"^[0-9a-f]{12}$")
IpAddress = constr(pattern=r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$")

DeviceMode = snake_case_enum(Mode)
DeviceFanSpeed = snake_case_enum(FanSpeed)
DeviceHorizontalSwing = snake_case_enum(HorizontalSwing)
DeviceVerticalSwing = snake_case_enum(VerticalSwing)

class DeviceViewModel(BaseModel):
    mac: str = Field(..., pattern=r"^[0-9a-f]{12}$", description="MAC address of a device (12 lowercase hex digits, no separators)")
    ip: str = Field(..., pattern=r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", description="IPv4 address of the device")
    power: bool
    mode: DeviceMode
    current_temperature: Optional[int]
    target_temperature: int
    current_humidity: Optional[int]
    target_humidity: Optional[int]
    fan_speed: DeviceFanSpeed
    horizontal_swing: DeviceHorizontalSwing
    vertical_swing: DeviceVerticalSwing
    turbo: Optional[bool] = None
    quiet: Optional[bool] = None
    light: Optional[bool] = None
    fresh_air: Optional[bool] = None
    xfan: Optional[bool] = None
    anion: Optional[bool] = None
    sleep: Optional[bool] = None
    power_save: Optional[bool] = None
    beep: Optional[bool] = None
    clean_filter: Optional[bool] = None
    water_full: Optional[bool] = None
    steady_heat: Optional[bool] = None

class DeviceUpdateModel(BaseModel):
    power: Optional[bool] = None
    mode: Optional[DeviceMode] = None
    target_temperature: Optional[int] = Field(
        None,
        ge=16,
        le=30,
        description="Target temperature (16-30, step 1)",
        json_schema_extra={"step": 1}
    )
    target_humidity: Optional[int] = Field(
        None,
        ge=40,
        le=90,
        description="Target humidity (40-90, step 1)",
        json_schema_extra={"step": 1}
    )
    fan_speed: Optional[DeviceFanSpeed] = None
    horizontal_swing: Optional[DeviceHorizontalSwing] = None
    vertical_swing: Optional[DeviceVerticalSwing] = None
    turbo: Optional[bool] = None
    quiet: Optional[bool] = None
    light: Optional[bool] = None
    fresh_air: Optional[bool] = None
    xfan: Optional[bool] = None
    anion: Optional[bool] = None
    sleep: Optional[bool] = None
    power_save: Optional[bool] = None
    beep: Optional[bool] = None
    steady_heat: Optional[bool] = None

class RootResponse(BaseModel):
    app: str
    version: str
    devices: List[MacAddress]

class ConnectionManager:
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
                    logger.error(f"Error sending data to WebSocket {connection.client.host}: {e}")
            
            for conn in disconnected:
                self.active_connections.discard(conn)

class GreeClimateManager:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.devices: Dict[MacAddress, Device] = {}
        self.discovery = Discovery()
        self.connection_manager = ConnectionManager(args)
        self.view_models: Dict[MacAddress, DeviceViewModel] = {}
        self.polling_tasks: Dict[MacAddress, asyncio.Task] = {}
        
    async def discover_devices(self) -> List[MacAddress]:
        """Discover and bind to Gree devices"""
        logger.info("Starting device discovery...")

        await self.stop_polling()
        
        try:
            found_devices = await self.discovery.scan(wait_for=args.discovery_timeout)
            for device_info in found_devices:
                try:
                    device = Device(device_info)
                    await device.bind()

                    # Sleep a bit, state update may not work immediately after binding
                    await asyncio.sleep(1)
                    
                    self.devices[device_info.mac] = device
                    
                    status = await self._get_device_view_model(device_info.mac)
                    self.view_models[device_info.mac] = status
                    
                    await self._start_polling_for_device(device_info.mac)
                    
                    logger.info(f"Device discovered and bound: {device_info.name} at {device_info.ip}")
                    
                except (DeviceNotBoundError, DeviceTimeoutError) as e:
                    logger.error(f"Failed to bind device {device_info.ip}: {e}")
                except Exception as e:
                    logger.error(f"Unexpected error binding device {device_info.ip}: {e}")
                    
        except Exception as e:
            logger.error(f"Discovery failed: {e}")
            
        logger.info(f"Discovery complete. Found {len(self.devices)} devices.")
        return list(self.view_models.keys())
    
    async def _get_device_view_model(self, mac: MacAddress, update_state: bool = True) -> DeviceViewModel:
        """Get device status as dictionary for comparison"""
        if mac not in self.devices:
            return {}
            
        device = self.devices[mac]
        
        if update_state:
            try:
                await device.update_state()
            except (DeviceNotBoundError, DeviceTimeoutError) as e:
                logger.error(f"Failed to update device {mac}: {e}")
                return self.view_models.get(mac, {})
        
        return DeviceViewModel(
            mac=device.device_info.mac.replace(":", ""),
            ip=device.device_info.ip,
            power=device.power,
            mode=to_device_enum(Mode(device.mode), DeviceMode),
            current_temperature=device.current_temperature,
            target_temperature=device.target_temperature,
            current_humidity=device.current_humidity if device.current_humidity else None,
            target_humidity=device.target_humidity if device.target_humidity else None,
            fan_speed=to_device_enum(FanSpeed(device.fan_speed), DeviceFanSpeed),
            horizontal_swing=to_device_enum(HorizontalSwing(device.horizontal_swing), DeviceHorizontalSwing),
            vertical_swing=to_device_enum(VerticalSwing(device.vertical_swing), DeviceVerticalSwing),
            turbo=getattr(device, 'turbo', None),
            quiet=getattr(device, 'quiet', None),
            light=getattr(device, 'light', None),
            fresh_air=getattr(device, 'fresh_air', None),
            xfan=getattr(device, 'xfan', None),
            anion=getattr(device, 'anion', None),
            sleep=getattr(device, 'sleep', None),
            power_save=getattr(device, 'power_save', None),
            beep=getattr(device, 'beep', None),
            clean_filter=getattr(device, 'clean_filter', None),
            water_full=getattr(device, 'water_full', None),
            steady_heat=getattr(device, 'steady_heat', None),
        )
    
    async def _start_polling_for_device(self, mac: MacAddress):
        """Start polling task for a specific device"""
        if mac in self.polling_tasks:
            self.polling_tasks[mac].cancel()
        
        self.polling_tasks[mac] = asyncio.create_task(self._poll_device_state(mac))
        logger.info(f"Started polling for device: {mac}")
    
    async def _poll_device_state(self, mac: MacAddress):
        """Poll device state notify on changes"""
        logger.info(f"Starting state polling for device: {mac}")
        
        while mac in self.devices:
            try:
                current_state = await self._get_device_view_model(mac)
                
                if not current_state:  # Skip if we couldn't get state
                    await asyncio.sleep(self.args.polling_interval)
                    continue
                
                last_state = self.view_models.get(mac, {})

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
                    logger.info(f"State change detected for device {mac}: {changes}")
                    
                    self.view_models[mac] = current_state
                    
                    try:
                        await self.connection_manager.broadcast(
                            {
                                "type": "report",
                                "mac": mac,
                                "data": changes
                            }
                        )
                    except Exception as e:
                        logger.error(f"Failed to send state change notification for {mac}: {e}")
                
                await asyncio.sleep(self.args.polling_interval)
                
            except asyncio.CancelledError:
                logger.info(f"Polling cancelled for device: {mac}")
                break
            except Exception as e:
                logger.error(f"Error polling device {mac}: {e}")
                await asyncio.sleep(self.args.polling_interval * 2)  # Wait longer on error
    
    async def stop_polling(self):
        """Stop all polling tasks"""
        for mac, task in self.polling_tasks.items():
            task.cancel()
            logger.info(f"Stopped polling for device: {mac}")
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
        if data.beep is not None:
            modified = device.beep != data.beep or modified
            device.beep = data.beep
        if data.steady_heat is not None:
            modified = device.steady_heat != data.steady_heat or modified
            device.steady_heat = data.steady_heat
        
        try:
            await device.push_state_update()
            
        except (DeviceNotBoundError, DeviceTimeoutError) as e:
            logger.error(f"Failed to send command to device {mac}: {e}")
            # Try to rebind and retry
            try:
                await device.bind()
                await device.push_state_update()
            except Exception as rebind_error:
                logger.error(f"Failed to rebind and retry command for device {mac}: {rebind_error}")
                raise HTTPException(status_code=503, detail=f"Device communication error: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error sending command to device {mac}: {e}")
            raise HTTPException(status_code=500, detail=f"Command failed: {str(e)}")
        
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

args = get_cli_args()

logging.getLogger().setLevel(logging.DEBUG if args.verbose else logging.INFO )
logging.getLogger("greeclimate").setLevel(logging.DEBUG if args.verbose else logging.WARNING )

climate_manager = GreeClimateManager(args)

@asynccontextmanager
async def lifespan(app: FastAPI):
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
    version="1.1.4",
    lifespan=lifespan
)

@app.get("/", response_model=RootResponse, summary="API info", description="Basic API info and list of discovered device MAC addresses.")
async def root():
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
            try:
                message = json.loads(data)

                if message.get("type") == "update":
                    mac = message.get("mac")
                    mac = MacAddress(mac) if mac else None
                    if not mac or mac not in climate_manager.view_models:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message": "Invalid or missing MAC address"
                        }))
                        continue
                    
                    command = DeviceUpdateModel(**message.get("data", {}))
                    
                    modified = await climate_manager.send_update(mac, command)

                    if not modified:
                        await websocket.send_text(json.dumps({
                            "type": "not_changed",
                            "mac": mac,
                            "message": "No changes made to the device by last command"
                        }))
                    
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Invalid JSON format"
                }))
            except Exception as e:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": str(e)
                }))
                
    except WebSocketDisconnect:
        climate_manager.connection_manager.disconnect(websocket)

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=args.port,
        reload=args.dev_mode,
        log_level= "debug" if args.verbose else "info",
    )
