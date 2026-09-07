FROM python:3.13-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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
#   PORT  DISCOVERY_TIMEOUT  POLLING_INTERVAL  RESPONSE_TIMEOUT  VERBOSE
#   AUTH_ENABLED  AUTH_TOKEN  CONFIG_FILE

# Health check. The port is resolved the same way the application resolves it,
# so the probe follows the configuration file as well as the environment.
# /health stays reachable without a token, so this works with auth enabled.
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD ["sh", "-c", "curl -fsS \"http://localhost:$(python3 -m gree_ws.port)/health\""]

# Run the application
ENTRYPOINT ["./entrypoint.sh"]
