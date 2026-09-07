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

# Expose port
ENV PORT=8123
EXPOSE 8123

# Defaults live in config.yaml. These environment variables override it, and
# are the way to configure the container without rebuilding or mounting a file.
#   DISCOVERY_TIMEOUT  POLLING_INTERVAL  RESPONSE_TIMEOUT  VERBOSE
#   AUTH_ENABLED  AUTH_TOKEN  CONFIG_FILE

# Health check
# /health stays reachable without a token, so the probe works with auth enabled.
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8123}/health || exit 1

# Run the application
ENTRYPOINT ["./entrypoint.sh"]
