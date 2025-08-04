
# Gree Climate WebSocket + REST API

Advanced REST and WebSocket API for controlling Gree air conditioners with real-time state monitoring.

**Note**: This API requires Gree devices configured in local mode. Not all features may be available for all air conditioner models.

**Note**: For now, the application uses custom patches to the `greeclimate` library. The patches are taken from the `greeclimate` repository pull requests.

## ✨ Features

- **Automatic device discovery** - Scans the network for Gree air conditioners on startup and on demand
- **REST API** - Full RESTful API for device control
- **WebSocket** - Real-time communication with clients
- **State monitoring** - Automatic polling at a specified interval
- **Change notifications** - Instant notifications about state changes via WebSocket
- **Error handling** - Automatic reconnection to devices

### Application Access
- **API**: http://localhost:8123
- **WebSocket**: ws://localhost:8123/ws

## 🔌 REST API

API documentation is available at: [http://localhost:8123/docs](http://localhost:8123/docs)

## 🔌 WebSocket API

### Connection
```javascript
const ws = new WebSocket('ws://localhost:8123/ws');
```

### Message Types

#### 1. Initial status
Automatically sent after connection:
```json
{
  "type": "list",
  "data": [ /* List of devices, schema exactly the same as for /devices endpoint */ ]
}
```

#### 2. State change (automatic)
Sent to all WebSocket clients when a device state change is detected. The `data` field contains only the changed parameters.
```json
{
  "type": "report",
  "mac": "aabbccddeeff",
  "data": {
    "current_temperature": {
      "old": 22,
      "new": 23
    },
    "current_humidity": {
      "old": 50,
      "new": 55
    }
  },
}
```

#### 3. Updating device state
```javascript
ws.send(JSON.stringify({
  "type": "update",
  "mac": "aabbccddeeff",
  "data": {
    "power": true,
    "target_temperature": 22
    /* [...] */
  }
}));
```

You can update single or multiple parameters at once. The `mac` field is the device's MAC address, and the `data` field contains the parameters to update.
If the command had an effect, you will soon receive a `report` message; otherwise, you will receive a `not_changed` message.

#### 7. Errors
```json
{
  "type": "error",
  "message": "Device not found"
}
```

## 🛠️ Build and run in docker

### Build the Docker image
```bash
docker build -t gree-ws .
``` 


### Run the Docker container (network mode: host required)
The application uses network broadcasts and must be run with `--network host`.

```bash
docker run -it --name gree-ws --rm --network host gree-ws
```

> **Note:** The `-p` option is not needed with `--network host`.


### Environment variables
The following environment variables can be set to control the application:

- `PORT` — Port on which the application will listen (default: 8123)
- `DISCOVERY_TIMEOUT` — Device discovery timeout in seconds (default: 3)
- `POLLING_INTERVAL` — Device polling interval in seconds (default: 2)
- `VERBOSE` — Enable verbose logging (default: false)

Example usage with Docker:
```bash
docker run -it --name gree-ws --rm --network host -e DISCOVERY_TIMEOUT=5 -e POLLING_INTERVAL=5 gree-ws
```

### Run with Docker Compose
You can also use Docker Compose to run the application. Create a `docker-compose.yml` file with the following content:
```yaml
version: '3.8'
services:
  gree-ws:
    build: .
    network_mode: host
    environment:
      - PORT=8123
      - DISCOVERY_TIMEOUT=5
      - POLLING_INTERVAL=1
```

## 🤝 Collaboration

Contributions, suggestions, and bug reports are welcome!

If you would like to contribute, please fork the repository and submit a pull request with your changes.

For bug reports or feature requests, please open an issue on GitHub or contact the author directly.

Feel free to discuss ideas, improvements, or integration with other systems.
