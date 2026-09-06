"""Tests for reporting whether a device is currently answering.

Availability is an ordinary field on the view model, so it reaches clients over
REST like any other value and over WebSocket through the same change report.
"""

from dataclasses import replace

import pytest

from conftest import MAC, capture_broadcasts, device_info, run_polling_briefly
from gree_ws import manager as manager_module
from gree_ws.manager import GreeClimateManager

# The roster internals are what these tests are about.
# pylint: disable=protected-access


@pytest.mark.asyncio
async def test_a_responding_unit_is_available(manager):
    """A unit that answers is reported as available"""
    view = await manager.device_view(MAC)

    assert view.available is True


@pytest.mark.asyncio
async def test_a_unit_going_quiet_becomes_unavailable(manager, device, monkeypatch):
    """Sustained silence flips the field, and the change is reported once"""
    monkeypatch.setattr(manager_module, "UNRESPONSIVE_AFTER", 2)
    manager.settings = replace(manager.settings, response_timeout=0.2, polling_interval=0.1, discovery_timeout=0)
    manager.view_models[MAC] = await manager.device_view(MAC)
    device.alive = False

    broadcasts = await run_polling_briefly(manager, seconds=2.0)

    reports = [b for b in broadcasts if b["type"] == "report" and "available" in b["data"]]
    assert reports, "the outage was never reported"
    assert reports[0]["mac"] == MAC
    assert reports[0]["data"]["available"] == {"old": True, "new": False}
    assert len(reports) == 1, "the same outage was reported more than once"
    assert manager.view_models[MAC].available is False


@pytest.mark.asyncio
async def test_the_last_known_state_is_kept_while_unavailable(manager, device, monkeypatch):
    """An offline unit keeps the values it last reported"""
    monkeypatch.setattr(manager_module, "UNRESPONSIVE_AFTER", 1)
    manager.settings = replace(manager.settings, response_timeout=0.2)
    manager.view_models[MAC] = await manager.device_view(MAC)
    device.alive = False

    view = await manager.device_view(MAC)

    assert view.available is False
    assert view.mac == MAC
    assert view.target_temperature == 21
    assert view.current_temperature == 20


@pytest.mark.asyncio
async def test_a_unit_that_comes_back_is_available_again(manager, device, monkeypatch):
    """Recovery flips the field back and is reported the same way"""
    monkeypatch.setattr(manager_module, "UNRESPONSIVE_AFTER", 1)
    manager.settings = replace(manager.settings, response_timeout=0.2)
    manager.view_models[MAC] = await manager.device_view(MAC)
    device.alive = False
    manager.view_models[MAC] = await manager.device_view(MAC)
    assert manager.view_models[MAC].available is False

    device.alive = True
    view = await manager.device_view(MAC)

    assert view.available is True


@pytest.mark.asyncio
async def test_a_healthy_unit_reports_no_availability_change(manager):
    """No availability traffic while everything is working"""
    manager.view_models[MAC] = await manager.device_view(MAC)

    broadcasts = await run_polling_briefly(manager, seconds=1.0)

    assert not [b for b in broadcasts if "available" in b.get("data", {})]


@pytest.mark.asyncio
async def test_a_unit_that_vanished_stays_in_the_roster(fast_args, discovery):
    """A device that answers nothing is kept, reported as unavailable.

    It may simply be switched off, so dropping it would lose the address, the
    last known state and any chance of picking it up again by itself.
    """
    infos = [device_info()]
    created = discovery(infos)
    climate_manager = GreeClimateManager(fast_args)
    await climate_manager.discover_devices()

    created[0].alive = False
    climate_manager.missed_responses[MAC] = manager_module.UNRESPONSIVE_AFTER
    infos.clear()  # it does not answer the broadcast either

    macs = await climate_manager.discover_devices()
    await climate_manager.stop_polling()

    assert macs == [MAC]
    assert MAC in climate_manager.devices
    assert climate_manager.view_models[MAC].available is False


@pytest.mark.asyncio
async def test_a_kept_unit_is_still_polled(fast_args, discovery):
    """An offline unit keeps being polled, so it can come back on its own"""
    infos = [device_info()]
    created = discovery(infos)
    climate_manager = GreeClimateManager(fast_args)
    await climate_manager.discover_devices()

    created[0].alive = False
    climate_manager.missed_responses[MAC] = manager_module.UNRESPONSIVE_AFTER
    infos.clear()
    await climate_manager.discover_devices()

    assert MAC in climate_manager.polling_tasks
    assert not climate_manager.polling_tasks[MAC].done()

    await climate_manager.stop_polling()


@pytest.mark.asyncio
async def test_an_offline_unit_that_reappears_is_reported_available(fast_args, discovery):
    """Rediscovering a unit that had gone quiet brings it back"""
    infos = [device_info()]
    created = discovery(infos)
    climate_manager = GreeClimateManager(fast_args)
    await climate_manager.discover_devices()

    created[0].alive = False
    climate_manager.missed_responses[MAC] = manager_module.UNRESPONSIVE_AFTER
    broadcasts = capture_broadcasts(climate_manager)

    await climate_manager.discover_devices()  # the unit answers the broadcast again
    await climate_manager.stop_polling()

    assert climate_manager.view_models[MAC].available is True
    assert not [b for b in broadcasts if b["type"] == "availability"], "the availability message is gone"
