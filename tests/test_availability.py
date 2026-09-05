"""Tests for telling clients when a device can and cannot be relied on."""

from dataclasses import replace

import pytest

from conftest import MAC, capture_broadcasts, device_info, run_polling_briefly
from gree_ws import manager as manager_module
from gree_ws.manager import NO_RESPONSE, RECOVERED, REMOVED, GreeClimateManager

# The roster internals are what these tests are about.
# pylint: disable=protected-access


@pytest.mark.asyncio
async def test_a_unit_going_quiet_is_announced(manager, device, monkeypatch):
    """Clients are told once when a device stops being usable"""
    monkeypatch.setattr(manager_module, "UNRESPONSIVE_AFTER", 2)
    manager.settings = replace(manager.settings, response_timeout=0.2, polling_interval=0.1)
    manager.view_models[MAC] = await manager.device_view(MAC)
    device.alive = False

    broadcasts = await run_polling_briefly(manager, seconds=2.0)

    outages = [b for b in broadcasts if b["type"] == "availability"]
    assert outages, "the outage was never announced"
    assert outages[0]["mac"] == MAC
    assert outages[0]["data"] == {"available": False, "reason": NO_RESPONSE}
    assert len(outages) == 1, "the same outage was announced more than once"


@pytest.mark.asyncio
async def test_a_unit_that_comes_back_is_announced(manager, monkeypatch):
    """Recovery is announced too, so a client can trust the state again"""
    monkeypatch.setattr(manager_module, "UNRESPONSIVE_AFTER", 2)
    manager.view_models[MAC] = await manager.device_view(MAC)
    manager.unavailable[MAC] = NO_RESPONSE
    broadcasts = capture_broadcasts(manager)

    await manager.device_view(MAC)

    assert broadcasts == [{"type": "availability", "mac": MAC, "data": {"available": True, "reason": RECOVERED}}]
    assert MAC not in manager.unavailable


@pytest.mark.asyncio
async def test_a_healthy_unit_announces_nothing(manager):
    """No availability traffic while everything is working"""
    manager.view_models[MAC] = await manager.device_view(MAC)

    broadcasts = await run_polling_briefly(manager, seconds=1.0)

    assert not [b for b in broadcasts if b["type"] == "availability"]


@pytest.mark.asyncio
async def test_dropping_a_unit_is_announced(fast_args, discovery):
    """A device leaving the roster is announced as removed"""
    infos = [device_info()]
    created = discovery(infos)
    climate_manager = GreeClimateManager(fast_args)
    await climate_manager.discover_devices()

    broadcasts = capture_broadcasts(climate_manager)
    created[0].alive = False
    climate_manager.missed_responses[MAC] = manager_module.UNRESPONSIVE_AFTER
    infos.clear()

    await climate_manager.discover_devices()
    await climate_manager.stop_polling()

    removals = [b for b in broadcasts if b["type"] == "availability"]
    assert removals
    assert removals[-1]["data"] == {"available": False, "reason": REMOVED}


@pytest.mark.asyncio
async def test_availability_state_is_cleared_with_the_device(fast_args, discovery):
    """Forgetting a device forgets that it was unavailable"""
    discovery([device_info()])
    climate_manager = GreeClimateManager(fast_args)
    await climate_manager.discover_devices()
    await climate_manager.stop_polling()
    climate_manager.unavailable[MAC] = NO_RESPONSE

    climate_manager._forget_device(MAC)

    assert MAC not in climate_manager.unavailable


def test_the_availability_message_reads_both_ways(manager):
    """The message says available when nothing is wrong"""
    assert manager.availability_message(MAC)["data"] == {"available": True, "reason": RECOVERED}

    manager.unavailable[MAC] = NO_RESPONSE

    assert manager.availability_message(MAC)["data"] == {"available": False, "reason": NO_RESPONSE}
