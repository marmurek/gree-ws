# Every dependency ships a wheel for this Python, so the image needs no
# compiler. --only-binary makes the build fail rather than compile one that
# stops shipping a wheel.
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt

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
# mounting a file; the Configuration table in README.md lists them.
# CONFIG_FILE selects a different configuration file.

# Health check. It resolves the address the same way the application does, so
# it follows the configuration file as well as the environment, including an
# interface narrower than every one. /health needs no token, so this works with
# authorisation enabled. Written in Python so the image needs no HTTP client.
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD ["python3", "-m", "gree_ws.probe"]

# Run the application
ENTRYPOINT ["./entrypoint.sh"]
