"""Tests for the address the Docker health check probes.

The check runs inside the container and has to reach the application whatever
interface it was told to bind to.
"""

import pytest

from gree_ws.config import Settings
from gree_ws.probe import probe_address


@pytest.mark.parametrize(
    "host, expected",
    [
        ("0.0.0.0", "127.0.0.1:8180"),
        ("*", "127.0.0.1:8180"),
        ("::", "[::1]:8180"),
        ("127.0.0.1", "127.0.0.1:8180"),
        ("192.168.50.10", "192.168.50.10:8180"),
        ("fe80::1", "[fe80::1]:8180"),
        ("[fe80::1]", "[fe80::1]:8180"),
    ],
)
def test_the_probe_follows_the_configured_interface(host, expected):
    """A wildcard is probed over loopback; a named interface is probed directly"""
    assert probe_address(Settings(host=host, port=8180)) == expected


def test_the_probe_follows_the_configured_port():
    """Regression test: the check used to assume the port never moved"""
    assert probe_address(Settings(port=9999)).endswith(":9999")
