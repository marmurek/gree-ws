"""Tests for looking up a unit on demand when a request names an unknown MAC."""

import asyncio
from dataclasses import replace

import pytest

from conftest import MAC, FakeDevice, device_info
from gree_ws import manager as manager_module
from gree_ws.manager import GreeClimateManager

OTHER_MAC = "aabbcc334455"


def count_scans(monkeypatch, infos, delay: float = 0.0) -> list:
    """Make discovery answer with `infos` and record every scan it performs"""
    scans: list = []

    async def scan(*_args, **_kwargs):
        scans.append(None)
        await asyncio.sleep(delay)
        return list(infos)

    monkeypatch.setattr("greeclimate.discovery.Discovery.scan", scan)
    return scans


@pytest.mark.asyncio
async def test_a_known_unit_is_found_without_scanning(manager, monkeypatch):
    """A unit already in the roster never costs a broadcast"""
    manager.view_models[MAC] = await manager.device_view(MAC, update_state=False)
    scans = count_scans(monkeypatch, [])

    assert await manager.find_device(MAC) is True
    assert not scans


@pytest.mark.asyncio
async def test_a_unit_that_came_online_later_is_bound_and_polled(cli_args, discovery):
    """A unit missing at startup is picked up by the first request for it"""
    infos: list = []
    created = discovery(infos)
    climate_manager = GreeClimateManager(cli_args)
    await climate_manager.discover_devices()
    assert not climate_manager.view_models

    infos.append(device_info())
    found = await climate_manager.find_device(MAC)
    await climate_manager.stop_polling()

    assert found is True
    assert climate_manager.devices[MAC] is created[0]
    assert climate_manager.view_models[MAC].mac == MAC
    assert climate_manager.view_models[MAC].available is True


@pytest.mark.asyncio
async def test_the_lookup_starts_polling_the_found_unit(cli_args, discovery):
    """The found unit reports changes like any discovered one"""
    discovery([device_info()])
    climate_manager = GreeClimateManager(cli_args)

    await climate_manager.find_device(MAC)

    assert MAC in climate_manager.polling_tasks
    assert not climate_manager.polling_tasks[MAC].done()
    await climate_manager.stop_polling()


@pytest.mark.asyncio
async def test_the_lookup_leaves_other_units_polling(cli_args, discovery):
    """Looking up one unit does not restart the others, unlike a full rediscovery"""
    infos = [device_info()]
    discovery(infos)
    climate_manager = GreeClimateManager(cli_args)
    await climate_manager.discover_devices()
    running = climate_manager.polling_tasks[MAC]

    infos.append(device_info(mac=OTHER_MAC, ip="1.1.1.1"))
    assert await climate_manager.find_device(OTHER_MAC) is True

    assert climate_manager.polling_tasks[MAC] is running
    assert not running.done()
    await climate_manager.stop_polling()


@pytest.mark.asyncio
async def test_a_unit_that_does_not_answer_is_not_found(cli_args, discovery, monkeypatch):
    """A unit absent from the scan stays unknown"""
    discovery([])
    count_scans(monkeypatch, [device_info(mac=OTHER_MAC)])
    climate_manager = GreeClimateManager(cli_args)

    assert await climate_manager.find_device(MAC) is False
    assert MAC not in climate_manager.devices
    assert MAC not in climate_manager.view_models


@pytest.mark.asyncio
async def test_a_unit_that_fails_to_bind_is_not_found(cli_args, discovery):
    """Answering the broadcast is not enough, the unit has to bind as well"""
    created = discovery([device_info()], factory=_silent_device)
    climate_manager = GreeClimateManager(replace(cli_args, response_timeout=0.1))

    assert await climate_manager.find_device(MAC) is False
    assert created, "the lookup tried to bind the unit"
    assert MAC not in climate_manager.devices
    assert MAC not in climate_manager.view_models


def _silent_device(*args, **kwargs) -> FakeDevice:
    """A fake unit that answers the broadcast but not the bind handshake"""
    unit = FakeDevice(*args, **kwargs)
    unit.alive = False
    return unit


@pytest.mark.asyncio
async def test_a_failed_lookup_is_not_repeated_within_the_cooldown(cli_args, discovery, monkeypatch):
    """A client retrying an unknown address does not flood the network with scans"""
    discovery([])
    scans = count_scans(monkeypatch, [])
    climate_manager = GreeClimateManager(replace(cli_args, lookup_cooldown=60))

    assert await climate_manager.find_device(MAC) is False
    assert await climate_manager.find_device(MAC) is False

    assert len(scans) == 1


@pytest.mark.asyncio
async def test_the_cooldown_is_kept_per_address(cli_args, discovery, monkeypatch):
    """A miss for one address does not hold back the lookup of another"""
    discovery([])
    scans = count_scans(monkeypatch, [])
    climate_manager = GreeClimateManager(replace(cli_args, lookup_cooldown=60))

    await climate_manager.find_device(MAC)
    await climate_manager.find_device(OTHER_MAC)

    assert len(scans) == 2


@pytest.mark.asyncio
async def test_a_zero_cooldown_scans_on_every_request(cli_args, discovery, monkeypatch):
    """With the cooldown disabled every request for an unknown unit scans"""
    discovery([])
    scans = count_scans(monkeypatch, [])
    climate_manager = GreeClimateManager(replace(cli_args, lookup_cooldown=0))

    await climate_manager.find_device(MAC)
    await climate_manager.find_device(MAC)

    assert len(scans) == 2


@pytest.mark.asyncio
async def test_a_unit_is_looked_up_again_after_the_cooldown(cli_args, discovery, monkeypatch):
    """Once the cooldown has passed a unit that has since come online is found"""
    infos: list = []
    discovery(infos)
    scans = count_scans(monkeypatch, infos)
    climate_manager = GreeClimateManager(replace(cli_args, lookup_cooldown=60))

    assert await climate_manager.find_device(MAC) is False
    infos.append(device_info())
    climate_manager.lookup_timestamps[MAC] -= 61

    assert await climate_manager.find_device(MAC) is True
    assert len(scans) == 2
    await climate_manager.stop_polling()


@pytest.mark.asyncio
async def test_concurrent_requests_share_one_scan(cli_args, discovery, monkeypatch):
    """Requests arriving together for the same unit wait for one scan"""
    infos = [device_info()]
    discovery(infos)
    scans = count_scans(monkeypatch, infos, delay=0.05)
    climate_manager = GreeClimateManager(cli_args)

    results = await asyncio.gather(*(climate_manager.find_device(MAC) for _ in range(3)))
    await climate_manager.stop_polling()

    assert results == [True, True, True]
    assert len(scans) == 1


@pytest.mark.asyncio
async def test_a_unit_that_stopped_answering_is_not_bound_again(cli_args, monkeypatch):
    """Only units that answer the current broadcast count as found.

    Regression test: one Discovery instance served every scan, and its result
    lists every unit it has ever seen, so a switched off unit was "found" and
    went through a full bind attempt on every lookup.
    """
    answering = [device_info()]
    created: list = []

    async def search_devices(self, *_args, **_kwargs):
        for info in answering:
            await self.device_found(info)

    def build(*args, **kwargs):
        unit = _silent_device(*args, **kwargs)
        created.append(unit)
        return unit

    monkeypatch.setattr("greeclimate.discovery.Discovery.search_devices", search_devices)
    monkeypatch.setattr(manager_module, "GreeDevice", build)
    climate_manager = GreeClimateManager(
        replace(cli_args, discovery_timeout=0, response_timeout=0.1, lookup_cooldown=0)
    )

    assert await climate_manager.find_device(MAC) is False
    assert len(created) == 1, "the unit answered the first scan, so binding it was attempted"

    answering.clear()
    assert await climate_manager.find_device(MAC) is False
    assert len(created) == 1, "the unit did not answer the second scan"


@pytest.mark.asyncio
async def test_a_failed_bind_is_not_mistaken_for_an_enrolled_unit(manager, discovery):
    """A unit left in the roster without a view stays unknown when rebinding it fails"""
    stale = manager.devices[MAC]
    created = discovery([device_info()], factory=_silent_device)
    manager.settings = replace(manager.settings, response_timeout=0.1)

    assert await manager.find_device(MAC) is False
    assert created, "the lookup tried to bind the unit"
    assert manager.devices[MAC] is stale
    assert MAC not in manager.view_models
