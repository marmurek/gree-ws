"""The HTTP and WebSocket surface."""

import argparse
import json
import logging
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse

from gree_ws import VERSION
from gree_ws.errors import DeviceNotFound, DeviceUnavailable, GreeError, InvalidCommand
from gree_ws.manager import GreeClimateManager
from gree_ws.models import DeviceUpdateModel, DeviceViewModel, MacAddress, RootResponse

logger = logging.getLogger(__name__)

# How the device layer's failures read over HTTP. The manager itself knows
# nothing about status codes.
STATUS_FOR_ERROR = {
    DeviceNotFound: status.HTTP_404_NOT_FOUND,
    InvalidCommand: status.HTTP_422_UNPROCESSABLE_ENTITY,
    DeviceUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def create_app(args: argparse.Namespace) -> FastAPI:
    """Build the application around a manager configured from `args`"""
    climate_manager = GreeClimateManager(args)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """Discover devices on startup and stop polling on shutdown"""
        logger.info("Starting Gree Climate API...")
        await climate_manager.discover_devices()
        yield
        logger.info("Shutting down Gree Climate API...")
        await climate_manager.stop_polling()

    app = FastAPI(
        title="Gree Climate API",
        description="REST and WebSocket API for controlling Gree air conditioners with real-time state monitoring",
        version=VERSION,
        lifespan=lifespan,
    )
    app.state.climate_manager = climate_manager

    @app.exception_handler(GreeError)
    async def device_error_handler(_request: Request, exc: GreeError) -> JSONResponse:
        """Report a device failure with the status code that fits it"""
        code = STATUS_FOR_ERROR.get(type(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)
        return JSONResponse(status_code=code, content={"detail": str(exc)})

    @app.get(
        "/",
        response_model=RootResponse,
        summary="API info",
        description="Basic API info and list of discovered device MAC addresses.",
    )
    async def root() -> RootResponse:
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
    async def list_devices() -> List[DeviceViewModel]:
        """List all discovered device view models"""
        return list(climate_manager.view_models.values())

    @app.get(
        "/devices/{mac}",
        response_model=DeviceViewModel,
        summary="Get device view",
        description="Get detailed view of a specific Gree device by its MAC address.",
    )
    async def get_device_view(mac: MacAddress) -> DeviceViewModel:
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
            503: {"description": "Device did not acknowledge the command."},
        },
        summary="Send device update",
        description=(
            "Send update to a specific Gree device by its MAC address. Only fields that are provided in the "
            "request body will be updated. If a field is not provided, it will not be changed. The device will "
            "be updated immediately."
        ),
    )
    async def send_device_update(mac: MacAddress, data: DeviceUpdateModel) -> Response:
        """Send update to device"""
        if mac not in climate_manager.view_models:
            raise HTTPException(status_code=404, detail="Device not found")

        if not await climate_manager.send_update(mac, data):
            return Response(status_code=status.HTTP_304_NOT_MODIFIED)

        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/discover",
        response_model=List[MacAddress],
        responses={200: {"description": "List of MAC addresses of discovered devices."}},
        summary="Rediscover devices",
        description=(
            "Rediscover devices and return their MAC addresses. This will stop any ongoing polling and start a "
            "new discovery process. Useful for refreshing the device list."
        ),
    )
    async def rediscover_devices() -> List[MacAddress]:
        """Rediscover devices and return their MAC addresses"""
        return await climate_manager.discover_devices()

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """WebSocket endpoint for real-time device monitoring and control"""
        await climate_manager.connection_manager.connect(websocket)

        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "list",
                        "data": [
                            v.model_dump(by_alias=True, mode="json") for v in climate_manager.view_models.values()
                        ],
                    }
                )
            )

            # Availability is announced on change, so a client joining during an
            # outage would otherwise never hear about it.
            for mac in list(climate_manager.unavailable):
                await websocket.send_text(json.dumps(climate_manager.availability_message(mac)))

            while True:
                await _handle_ws_message(websocket, climate_manager)

        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected")
        finally:
            # Runs for a clean disconnect and for any unexpected error alike, so
            # a dead connection is never left behind in the broadcast set.
            climate_manager.connection_manager.disconnect(websocket)

    return app


async def _send_ws(websocket: WebSocket, **payload) -> None:
    """Send one JSON message to a client"""
    await websocket.send_text(json.dumps(payload))


async def _handle_ws_message(websocket: WebSocket, climate_manager: GreeClimateManager) -> None:
    """Read one client message and act on it"""
    data = await websocket.receive_text()

    try:
        message = json.loads(data)
    except json.JSONDecodeError:
        await _send_ws(websocket, type="error", message="Invalid JSON format")
        return

    message_id = message.get("message_id")

    try:
        message_type = message.get("type")

        if message_type != "update":
            await _send_ws(
                websocket, type="error", message_id=message_id, message=f"Unsupported message type: {message_type}"
            )
            return

        mac = message.get("mac")
        if not mac or mac not in climate_manager.view_models:
            await _send_ws(websocket, type="error", message_id=message_id, message="Invalid or missing MAC address")
            return

        command = DeviceUpdateModel(**message.get("data", {}))

        if await climate_manager.send_update(mac, command):
            await _send_ws(
                websocket,
                type="applied",
                mac=mac,
                message_id=message_id,
                message="Command applied and acknowledged by the device",
            )
        else:
            await _send_ws(
                websocket,
                type="not_changed",
                mac=mac,
                message_id=message_id,
                message="No changes made to the device by last command",
            )

    except WebSocketDisconnect:
        # The client went away, there is nobody left to report the error to
        raise
    except Exception as e:
        await _send_ws(websocket, type="error", message_id=message_id, message=str(e))
