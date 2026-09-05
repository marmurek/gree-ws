"""Shared fixtures for the Gree Climate API tests.

The tests exercise the application against a real greeclimate Device whose UDP layer is
replaced by a fake, so the protocol encoding, the cipher and every property
getter and setter are the library's own - only the network is simulated.
"""

import argparse
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# pylint: disable=wrong-import-position
from greeclimate.cipher import CipherV1
from greeclimate.device import DeviceInfo

from gree_ws import manager as manager_module
from gree_ws.device import GreeDevice
from gree_ws.manager import GreeClimateManager

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


class FakeDevice(GreeDevice):
    """A real greeclimate device with the UDP layer replaced.

    `sent` records the plaintext packets, `alive` controls whether the unit
    answers, `state` is what it answers with and `response_delay` is how long it
    takes to answer.

    Answers arrive through the event loop rather than inside `send`, because
    that is what a real unit does: the reply is a separate UDP datagram. Code
    that reads a property straight after asking for it therefore sees the
    previous answer here too, exactly as it would against real hardware.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A test double is deliberately not a real DatagramTransport.
        self._transport = FakeTransport()  # type: ignore[assignment]
        self.sent: list = []
        self.alive = True
        self.state = mock_state()
        self.response_delay = 0.01

    async def send(self, obj, addr=None, cipher=None):
        packet_type = obj["pack"]["t"]
        self.sent.append({"t": packet_type, "pack": dict(obj["pack"])})
        await super().send(obj, addr=addr, cipher=cipher)

        if not self.alive:
            return

        asyncio.get_running_loop().call_later(self.response_delay, self._answer, packet_type)

    def _answer(self, packet_type: str) -> None:
        """Deliver the unit's reply, the way a datagram would arrive"""
        if packet_type == "bind":
            self.device_cipher = CipherV1(FAKE_KEY.encode())
            self.handle_device_bound(FAKE_KEY)
        elif packet_type == "status":
            self.handle_state_update(**self.state)

    def last_command(self) -> dict:
        """The properties carried by the most recent command packet"""
        command = [p for p in self.sent if p["t"] == "cmd"][-1]
        return dict(zip(command["pack"]["opt"], command["pack"]["p"]))


def capture_broadcasts(climate_manager) -> list:
    """Redirect a manager's broadcasts into a list and return it"""
    broadcasts: list = []

    async def capture(data):
        broadcasts.append(data)

    climate_manager.connection_manager.broadcast = capture
    return broadcasts


async def run_polling_briefly(climate_manager, seconds: float = 2.0) -> list:
    """Run one device's polling loop for a moment and return what it broadcast"""
    broadcasts = capture_broadcasts(climate_manager)

    task = asyncio.create_task(climate_manager._poll_device_state(MAC))  # pylint: disable=protected-access
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    return broadcasts


@pytest.fixture(name="cli_args")
def cli_args_fixture() -> argparse.Namespace:
    """Command line arguments with a fast polling interval"""
    return argparse.Namespace(
        discovery_timeout=1, polling_interval=1, response_timeout=1.0, verbose=False, port=8123, dev_mode=False
    )


@pytest.fixture(name="fast_args")
def fast_args_fixture(cli_args, monkeypatch):
    """Timings short enough to watch a unit be declared gone and rebuilt"""
    cli_args.response_timeout = 0.2
    cli_args.polling_interval = 0.1
    cli_args.discovery_timeout = 0
    monkeypatch.setattr(manager_module, "UNRESPONSIVE_AFTER", 2)
    return cli_args


@pytest.fixture(name="device")
def device_fixture() -> FakeDevice:
    """A bound device that answers with the default state"""
    unit = FakeDevice(device_info(), timeout=1, bind_timeout=1)
    unit.device_cipher = CipherV1(FAKE_KEY.encode())
    unit.handle_state_update(**unit.state)
    return unit


@pytest.fixture(name="discovery")
def discovery_fixture(monkeypatch):
    """Make discovery return chosen units, built by an optional custom factory.

    Returns the list the built units are appended to, so a test can inspect
    what the manager created.
    """

    def configure(infos, factory=None):
        created: list = []
        build_unit = factory or FakeDevice

        def build(info, *args, **kwargs):
            unit = build_unit(info, *args, **kwargs)
            created.append(unit)
            return unit

        async def scan(_self, wait_for=0, bcast_ifaces=None):  # pylint: disable=unused-argument
            return list(infos)

        monkeypatch.setattr(manager_module, "GreeDevice", build)
        monkeypatch.setattr("greeclimate.discovery.Discovery.scan", scan)
        return created

    return configure


@pytest.fixture(name="manager")
def manager_fixture(cli_args, device) -> GreeClimateManager:
    """A manager holding the single fake device, with no polling running"""
    climate_manager = GreeClimateManager(cli_args)
    climate_manager.devices[MAC] = device
    return climate_manager
