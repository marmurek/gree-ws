# Gree Climate WebSocket + REST API

Advanced REST and WebSocket API for controlling Gree air conditioners with real-time state monitoring.

**Note**: This API requires Gree devices configured in local mode. Not all features may be available for all air conditioner models.

## ✨ Features

- **Automatic device discovery** - Scans the network for Gree air conditioners on startup and on demand
- **REST API** - Full RESTful API for device control
- **WebSocket** - Real-time communication with clients
- **State monitoring** - Automatic polling at a specified interval
- **Change notifications** - Instant notifications about state changes via WebSocket
- **Error handling** - Detects units that stop answering, reports them as unavailable, rebuilds them and follows a changed IP address
- **Configurable** - Every setting in one YAML file, overridable from the environment
- **Optional authorisation** - Shared token on REST and WebSocket, off by default

### Application Access
- **API**: http://localhost:8123
- **WebSocket**: ws://localhost:8123/ws

## ⚙️ Configuration

All settings live in [`config.yaml`](config.yaml). The file is optional - every setting has a default, so the application runs without it - and each value can be overridden by an environment variable, which is how the Docker image is configured.

```yaml
server:
  host: "0.0.0.0"
  port: 8123
  dev_mode: false

discovery:
  timeout: 3

polling:
  interval: 2
  response_timeout: 5

logging:
  verbose: false

auth:
  enabled: false
  token: ""
```

| Setting | Environment variable | Default | Meaning |
|---|---|---|---|
| `server.host` | `HOST` | `0.0.0.0` | Interface the API listens on |
| `server.port` | `PORT` | `8123` | Port the API listens on |
| `server.dev_mode` | `DEV_MODE` | `false` | Reload on source changes, for development |
| `discovery.timeout` | `DISCOVERY_TIMEOUT` | `3` | How long to wait for units to answer the discovery broadcast, in seconds |
| `polling.interval` | `POLLING_INTERVAL` | `2` | How often to ask each unit for its state, in seconds |
| `polling.response_timeout` | `RESPONSE_TIMEOUT` | `5` | How long to wait for a unit to answer, in seconds |
| `logging.verbose` | `VERBOSE` | `false` | Log every packet exchanged with the units |
| `auth.enabled` | `AUTH_ENABLED` | `false` | Require a token on every request |
| `auth.token` | `AUTH_TOKEN` | *(empty)* | The shared token clients must present |

Point the application at a different file with `--config`, which is the only command line argument:

```bash
python3 main.py --config /etc/gree-ws/config.yaml
```

In Docker, set `CONFIG_FILE` or mount your own file over `/app/config.yaml`.

`polling.response_timeout` also caps the binding handshake. greeclimate tries one cipher, waits for the timeout, then tries the other, so a large value makes startup slow when a newer unit is on the network.

### 🔐 Authorisation

Disabled by default: anyone who can reach the port can control the air conditioners. Since the container runs with `--network host`, that means every device on the local network.

If you would rather not give every client a token, narrowing `server.host` is the other way to close that down — `127.0.0.1` for local access only, or a single interface address:

```yaml
server:
  host: "127.0.0.1"
```

This restricts only the HTTP and WebSocket API. Device discovery keeps working, because it broadcasts over its own socket.

To turn it on, set a token:

```yaml
auth:
  enabled: true
  token: "a-long-random-string"
```

Enabling authorisation without a token is refused at startup rather than silently protecting nothing.

Clients then present the token on every request:

```bash
curl -H "Authorization: Bearer a-long-random-string" http://localhost:8123/devices
```

`X-API-Key: <token>` is accepted as an alternative. For WebSocket connections the token goes in the same header, or - since browsers cannot set headers on a WebSocket - in a query parameter:

```javascript
const ws = new WebSocket('ws://localhost:8123/ws?token=a-long-random-string');
```

> **Note:** query strings end up in proxy and access logs. Prefer the header wherever the client allows it.

A request without a valid token gets `401`; a WebSocket is closed with code `1008`. `GET /health`, `/docs`, `/redoc` and `/openapi.json` stay reachable without a token, so a health probe keeps working and the schema stays browsable.

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

#### 4. Command applied
Sent when the command reached the device and it acknowledged the change.
```json
{
  "type": "applied",
  "mac": "aabbccddeeff",
  "message_id": "abc-123",
  "message": "Command applied and acknowledged by the device"
}
```

#### 5. No change
Sent when the command did not change anything on the device.
```json
{
  "type": "not_changed",
  "mac": "aabbccddeeff",
  "message_id": "abc-123",
  "message": "No changes made to the device by last command"
}
```

#### 6. Errors
```json
{
  "type": "error",
  "message_id": "abc-123",
  "message": "Device not found"
}
```
The `message_id` field is absent when the request could not be parsed as JSON.

## 🌡️ Device fields worth knowing

Full schemas are in [/docs](http://localhost:8123/docs); these do not behave the way the names suggest.

- **`buzzer`** - whether the unit beeps when it receives a command. It is not stored on the air conditioner: it lives in the application's memory, defaults to enabled and goes back to enabled after a restart or a `POST /discover`. Send `"buzzer": false` to silence the unit.
- **`available`** - whether the device is currently answering. It is an ordinary field, so it is served by `/devices` like any other value and a change is reported through the usual `report` message:
  ```json
  {
    "type": "report",
    "mac": "aabbccddeeff",
    "data": { "available": { "old": true, "new": false } }
  }
  ```
  A device that stops answering is **not** removed from the list. It stays with `available: false` and every other field holding the last state it reported, which may be out of date. The application keeps polling it and periodically rebuilds the connection, so a unit that was switched off comes back by itself, following a changed IP address if it got one.
- **`target_humidity`** - the device encodes it as `(value - 15) / 5`, so only multiples of 5 in the 30-80 range are accepted; anything else is rejected with `422`. Units without a dehumidifier report no usable value and are reported as `null`.
- **The optional flags** (`turbo`, `quiet`, `light`, `fresh_air`, `xfan`, `anion`, `sleep`, `power_save`, `steady_heat`, `clean_filter`, `water_full`) are `null` when the unit does not report them at all, rather than `false`. Not every model supports every feature.

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
Every setting in [`config.yaml`](config.yaml) has a matching environment variable — see the [Configuration](#%EF%B8%8F-configuration) table. `CONFIG_FILE` selects a different configuration file inside the container.

Example usage with Docker:
```bash
docker run -it --name gree-ws --rm --network host -e DISCOVERY_TIMEOUT=5 -e POLLING_INTERVAL=5 gree-ws
```

With authorisation enabled, keep the token out of the image:
```bash
docker run -it --name gree-ws --rm --network host \
  -e AUTH_ENABLED=true -e AUTH_TOKEN="$(cat /run/secrets/gree_token)" gree-ws
```

Or mount your own configuration file:
```bash
docker run -it --name gree-ws --rm --network host \
  -v ./my-config.yaml:/app/config.yaml:ro gree-ws
```

The container runs as an unprivileged user and its health check uses `GET /health`, which stays reachable when authorisation is on.

### Run with Docker Compose
Keep the settings in a file next to the compose file and mount it:

```yaml
services:
  gree-ws:
    build: .
    image: gree-ws
    container_name: gree-ws
    # Device discovery uses UDP broadcast, which needs the host network.
    network_mode: host
    restart: unless-stopped
    volumes:
      - ./gree-ws.yaml:/app/config.yaml:ro
```

```yaml
# gree-ws.yaml - only what differs from the defaults
server:
  port: 8180

polling:
  interval: 60
```

Or configure it entirely from the environment, without a file:

```yaml
services:
  gree-ws:
    build: .
    network_mode: host
    environment:
      - PORT=8180
      - POLLING_INTERVAL=60
```

The two can be combined, but remember that an environment variable always wins over the file — a `PORT` left in the compose file will override the `port` in a mounted configuration.

A few things worth knowing about how settings are resolved:

- A blank environment variable (`- PORT=` in compose) counts as unset, not as an override.
- A value that cannot be used — a misspelled number, a boolean that is neither true nor false, a port outside 1-65535, a polling interval below 1 — is reported in the log and the next source down is used, so a typo in an environment variable falls back to the file rather than discarding it too.
- A configuration file that cannot be read or parsed is reported and the application starts on the defaults rather than refusing to run. Watch for this with a mounted file: the container runs as an unprivileged user and a file mounted `root:root` with mode `600` will not be readable.
- Setting `auth.token` without `auth.enabled` leaves the API open; the log says so.

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
pylint main.py gree_ws tests
pytest
```

`mypy` follows the `greeclimate` sources even though the library ships no `py.typed` marker - that is what makes it able to report a mistyped device property instead of letting it fail silently at runtime.

## 🤝 Collaboration

Contributions, suggestions, and bug reports are welcome!

If you would like to contribute, please fork the repository and submit a pull request with your changes.

For bug reports or feature requests, please open an issue on GitHub or contact the author directly.

Feel free to discuss ideas, improvements, or integration with other systems.
