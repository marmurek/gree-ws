"""Print the address the health check should probe.

The check needs an address before it can probe anything, and both the interface
and the port can come from the configuration file as easily as from the
environment. Resolving them through the same loader keeps the probe and the
application from disagreeing.
"""

import os

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


if __name__ == "__main__":
    print(probe_address(load_settings(os.environ.get("CONFIG_FILE", DEFAULT_CONFIG_PATH))))
