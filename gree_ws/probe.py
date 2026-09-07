"""The container's health check.

Both the interface and the port can come from the configuration file as easily
as from the environment, so the probe resolves them through the same loader as
the application and the two cannot disagree. Doing the request here rather than
with curl keeps an HTTP client out of the image.
"""

import os
import sys
import urllib.error
import urllib.request

from gree_ws.config import DEFAULT_CONFIG_PATH, Settings, load_settings

# Addresses that mean "every interface". A probe cannot connect to those, but
# loopback is always among the interfaces they cover.
WILDCARDS = {"0.0.0.0": "127.0.0.1", "::": "[::1]", "*": "127.0.0.1"}


def probe_address(settings: Settings) -> str:
    """The host and port a local health check should connect to"""
    host = WILDCARDS.get(settings.host, settings.host)

    # A bare IPv6 address needs brackets to be usable in a URL.
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    return f"{host}:{settings.port}"


def main() -> int:
    """Ask the running application whether it is healthy"""
    address = probe_address(load_settings(os.environ.get("CONFIG_FILE", DEFAULT_CONFIG_PATH)))

    try:
        with urllib.request.urlopen(f"http://{address}/health", timeout=10) as response:
            if response.status == 200:
                return 0
            print(f"{address} answered {response.status}", file=sys.stderr)
    except (urllib.error.URLError, OSError) as e:
        print(f"{address} is not answering: {e}", file=sys.stderr)

    return 1


if __name__ == "__main__":
    sys.exit(main())
