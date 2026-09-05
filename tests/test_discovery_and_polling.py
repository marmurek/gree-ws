"""Tests for the discover, bind and poll lifecycle."""

import asyncio

import pytest

import main
from conftest import FAKE_KEY, MAC, FakeDevice, device_info, mock_state


@pytest.fixture(name="discovered")
def discovered_fixture(cli_args, monkeypatch):
    """A manager wired to discover exactly one fake device"""
    climate_manager = main.GreeClimateManager(cli_args)
    created: list[FakeDevice] = []

    def build(info, *args, **kwargs):
        unit = FakeDevice(info, *args, **kwargs)
        created.append(unit)
        return unit

    async def scan(_self, wait_for=0, bcast_ifaces=None):  # pylint: disable=unused-argument
        return [device_info()]

    monkeypatch.setattr(main, "Device", build)
    monkeypatch.setattr("greeclimate.discovery.Discovery.scan", scan)
    return climate_manager, created


@pytest.mark.asyncio
async def test_discovery_binds_and_reports_the_device(discovered):
    """A discovered unit is bound, read and exposed through the view models"""
    climate_manager, created = discovered

    macs = await climate_manager.discover_devices()
    await climate_manager.stop_polling()

    assert macs == [MAC]
    unit = created[0]
    assert unit.device_key == FAKE_KEY
    assert unit.has_valid_state is True
    assert climate_manager.view_models[MAC].target_temperature == 21


@pytest.mark.asyncio
async def test_polling_broadcasts_state_changes(discovered):
    """A change on the unit is reported to WebSocket clients"""
    climate_manager, created = discovered
    await climate_manager.discover_devices()

    broadcasts: list = []

    async def capture(data):
        broadcasts.append(data)

    climate_manager.connection_manager.broadcast = capture
    created[0].state = mock_state(SetTem=24)

    await asyncio.sleep(climate_manager.args.polling_interval + 1)
    await climate_manager.stop_polling()

    reports = [b for b in broadcasts if b["type"] == "report"]
    assert reports, "no state change was reported"
    assert reports[0]["mac"] == MAC
    assert reports[0]["data"]["target_temperature"] == {"old": 21, "new": 24}


@pytest.mark.asyncio
async def test_a_device_that_fails_to_bind_is_skipped(cli_args, monkeypatch):
    """Discovery survives a unit that cannot be bound"""

    def build(info, *args, **kwargs):
        kwargs["bind_timeout"] = 0.2  # do not wait out the real timeout twice
        unit = FakeDevice(info, *args, **kwargs)
        unit.alive = False  # never answers the bind
        return unit

    async def scan(_self, wait_for=0, bcast_ifaces=None):  # pylint: disable=unused-argument
        return [device_info()]

    monkeypatch.setattr(main, "Device", build)
    monkeypatch.setattr("greeclimate.discovery.Discovery.scan", scan)

    climate_manager = main.GreeClimateManager(cli_args)
    macs = await climate_manager.discover_devices()
    await climate_manager.stop_polling()

    assert macs == []


@pytest.mark.asyncio
async def test_stop_polling_cancels_every_task(discovered):
    """Shutdown leaves no polling task behind"""
    climate_manager, _ = discovered
    await climate_manager.discover_devices()
    assert climate_manager.polling_tasks

    await climate_manager.stop_polling()

    assert not climate_manager.polling_tasks
