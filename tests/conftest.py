"""Shared fixtures for the Gree Climate API tests.

The tests exercise main.py against a real greeclimate Device whose UDP layer is
replaced by a fake, so the protocol encoding, the cipher and every property
getter and setter are the library's own - only the network is simulated.
"""

import argparse
import sys
from pathlib import Path

import pytest

# main.py parses the command line while it is being imported, so it has to see
# an empty argv rather than pytest's. Removing this need is a later cleanup.
sys.argv = ["main.py"]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# pylint: disable=wrong-import-position
from greeclimate.cipher import CipherV1
from greeclimate.device import Device, DeviceInfo

import main

FAKE_KEY = "abcdefgh12345678"
MAC = "aabbcc001122"


def mock_state(**overrides) -> dict:
    """A plausible status response from a unit, with optional field overrides"""
    state = {
        "Pow": 1,
        "Mod": 4,
        "SetTem": 21,
        "TemSen": 60,
        "TemUn": 0,
        "WdSpd": 2,
        "Air": 0,
        "Blo": 0,
        "Health": 0,
        "SwhSlp": 0,
        "SlpMod": 0,
        "Lig": 1,
        "SwingLfRig": 0,
        "SwUpDn": 0,
        "Quiet": 0,
        "Tur": 0,
        "StHt": 0,
        "SvSt": 0,
        "TemRec": 0,
        "HeatCoolType": 0,
        "hid": "362001000762+U-CS532AE(LT)V3.31.bin",
        "Dmod": 0,
        "Dwet": 5,
        "DwatSen": 47,
        "Dfltr": 0,
        "DwatFul": 0,
    }
    state.update(overrides)
    return state


def device_info(mac: str = MAC, ip: str = "1.1.1.0") -> DeviceInfo:
    """Device info as discovery would report it"""
    return DeviceInfo(ip, 7000, mac, "MockDevice", "MockBrand", "MockModel", "0.0.1-fake")


class FakeTransport:
    """Stands in for a UDP transport, swallowing everything sent through it"""

    def sendto(self, data, addr=None):
        """Discard the datagram"""

    def close(self):
        """No-op"""


class FakeDevice(Device):
    """A real greeclimate Device with the UDP layer replaced.

    `sent` records the plaintext packets, `alive` controls whether the unit
    answers, and `state` is what it answers with.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A test double is deliberately not a real DatagramTransport.
        self._transport = FakeTransport()  # type: ignore[assignment]
        self.sent: list = []
        self.alive = True
        self.state = mock_state()

    async def send(self, obj, addr=None, cipher=None):
        packet_type = obj["pack"]["t"]
        self.sent.append({"t": packet_type, "pack": dict(obj["pack"])})
        await super().send(obj, addr=addr, cipher=cipher)

        if not self.alive:
            return
        if packet_type == "bind":
            self.device_cipher = CipherV1(FAKE_KEY.encode())
            self.handle_device_bound(FAKE_KEY)
        elif packet_type == "status":
            self.handle_state_update(**self.state)

    def last_command(self) -> dict:
        """The properties carried by the most recent command packet"""
        command = [p for p in self.sent if p["t"] == "cmd"][-1]
        return dict(zip(command["pack"]["opt"], command["pack"]["p"]))


@pytest.fixture(name="cli_args")
def cli_args_fixture() -> argparse.Namespace:
    """Command line arguments with a fast polling interval"""
    return argparse.Namespace(discovery_timeout=1, polling_interval=1, verbose=False, port=8123, dev_mode=False)


@pytest.fixture(name="device")
def device_fixture() -> FakeDevice:
    """A bound device that answers with the default state"""
    unit = FakeDevice(device_info(), timeout=1, bind_timeout=1)
    unit.device_cipher = CipherV1(FAKE_KEY.encode())
    unit.handle_state_update(**unit.state)
    return unit


@pytest.fixture(name="manager")
def manager_fixture(cli_args, device) -> main.GreeClimateManager:
    """A manager holding the single fake device, with no polling running"""
    climate_manager = main.GreeClimateManager(cli_args)
    climate_manager.devices[MAC] = device
    return climate_manager
