# --- Build stage -------------------------------------------------------------
# greeclimate depends on netifaces, whose last release was in 2021 and which
# publishes no wheel newer than CPython 3.9, so it has to be compiled. The
# compiler is needed for that and for nothing else, so it stays in this stage.
FROM python:3.13-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# --- Runtime stage -----------------------------------------------------------
FROM python:3.13-slim

WORKDIR /app

# Only the finished packages cross over; no compiler and no build tools.
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

COPY gree_ws/ gree_ws/
COPY main.py .
COPY config.yaml .

# The application needs no privileges: it listens above 1024 and its UDP
# broadcast only needs SO_BROADCAST, not a raw socket.
RUN useradd --create-home --uid 10001 gree
USER gree

# The default port. Deliberately not an ENV: the environment takes precedence
# over the configuration file, so an ENV here would silently override the port
# set in a mounted config.yaml.
EXPOSE 8123

# Settings live in config.yaml. Every one of them can be overridden by an
# environment variable, which is how to configure the container without
# mounting a file:
#   HOST  PORT  DISCOVERY_TIMEOUT  POLLING_INTERVAL  RESPONSE_TIMEOUT  VERBOSE
#   AUTH_ENABLED  AUTH_TOKEN  CONFIG_FILE

# Health check. It resolves the address the same way the application does, so
# it follows the configuration file as well as the environment, including an
# interface narrower than every one. /health needs no token, so this works with
# authorisation enabled. Written in Python so the image needs no HTTP client.
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD ["python3", "-m", "gree_ws.probe"]

# Run the application
ENTRYPOINT ["./entrypoint.sh"]
