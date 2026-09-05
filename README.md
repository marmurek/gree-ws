# Gree Climate WebSocket + REST API

Advanced REST and WebSocket API for controlling Gree air conditioners with real-time state monitoring.

**Note**: This API requires Gree devices configured in local mode. Not all features may be available for all air conditioner models.

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

If the request carries a `message_id`, the same value is echoed back on the `not_changed` and `error` replies, so a client can match a response to its request. `report` messages are broadcast to every client and carry no `message_id`.

#### 4. No change
Sent when the command did not change anything on the device.
```json
{
  "type": "not_changed",
  "mac": "aabbccddeeff",
  "message_id": "abc-123",
  "message": "No changes made to the device by last command"
}
```

#### 5. Errors
```json
{
  "type": "error",
  "message_id": "abc-123",
  "message": "Device not found"
}
```
The `message_id` field is absent when the request could not be parsed as JSON.

## 🌡️ Device fields worth knowing

Full schemas are in [/docs](http://localhost:8123/docs); these two do not behave the way the names suggest.

- **`buzzer`** - whether the unit beeps when it receives a command. It is not stored on the air conditioner: it lives in the application's memory, defaults to enabled and goes back to enabled after a restart or a `POST /discover`. Send `"buzzer": false` to silence the unit.
- **`target_humidity`** - the device encodes it as `(value - 15) / 5`, so only multiples of 5 in the 30-80 range are accepted; anything else is rejected with `422`. Units without a dehumidifier report no usable value and are reported as `null`.

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
- `RESPONSE_TIMEOUT` — How long to wait for a device to answer a state request, in seconds (default: 5)
- `VERBOSE` — Enable verbose logging (default: false)

Example usage with Docker:
```bash
docker run -it --name gree-ws --rm --network host -e DISCOVERY_TIMEOUT=5 -e POLLING_INTERVAL=5 gree-ws
```

### Run with Docker Compose
You can also use Docker Compose to run the application. Create a `docker-compose.yml` file with the following content:
```yaml
services:
  gree-ws:
    build: .
    network_mode: host
    environment:
      - PORT=8123
      - DISCOVERY_TIMEOUT=5
      - POLLING_INTERVAL=1
```

## 🧑‍💻 Development

Run the application outside Docker:

```bash
bash ./boot_python.sh
source .venv/bin/activate
python3 main.py --dev_mode --verbose
```

The code is checked with black, mypy and pylint. All three read their settings from `pyproject.toml`:

```bash
pip install -r requirements-dev.txt
black .
mypy
pylint main.py
```

`mypy` follows the `greeclimate` sources even though the library ships no `py.typed` marker - that is what makes it able to report a mistyped device property instead of letting it fail silently at runtime.

## 🤝 Collaboration

Contributions, suggestions, and bug reports are welcome!

If you would like to contribute, please fork the repository and submit a pull request with your changes.

For bug reports or feature requests, please open an issue on GitHub or contact the author directly.

Feel free to discuss ideas, improvements, or integration with other systems.
