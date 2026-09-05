"""Tests for waiting on device answers instead of firing and forgetting.

greeclimate's `update_state()` only puts a request on the wire; the answer
arrives later as its own datagram. These tests cover the consequences: readings
must not lag a cycle behind, and a unit that stops answering must be noticed
rather than mistaken for one with nothing new to say.
"""

import time

import pytest

import main
from conftest import MAC, FakeDevice, device_info, mock_state, run_polling_briefly

# The view builder and the polling loop are internal; these tests drive them directly.
# pylint: disable=protected-access


@pytest.mark.asyncio
async def test_refresh_state_reports_that_the_unit_answered(device):
    """A responsive unit reports success and records when it answered"""
    assert await device.refresh_state(1.0) is True
    assert device.silent_for is not None
    assert device.silent_for < 1.0


@pytest.mark.asyncio
async def test_refresh_state_reports_silence(device):
    """A unit that never answers is reported as silent instead of hanging"""
    device.alive = False

    assert await device.refresh_state(0.2) is False


@pytest.mark.asyncio
async def test_the_view_shows_the_answer_to_the_request_it_just_made(manager, device):
    """The reading must be the state the unit just sent, not the previous one.

    Regression test for the one cycle lag: reading the properties straight after
    update_state() returned the answer to the previous request, so every value
    the API served was a polling interval out of date.
    """
    device.state = mock_state(SetTem=28)

    view = await manager._get_device_view_model(MAC)

    assert view.target_temperature == 28


@pytest.mark.asyncio
async def test_a_silent_device_keeps_its_last_known_state(manager, device):
    """Nothing new arriving must not be published as a fresh reading"""
    manager.view_models[MAC] = await manager._get_device_view_model(MAC)
    device.alive = False
    device.state = mock_state(SetTem=28)

    view = await manager._get_device_view_model(MAC)

    assert view.target_temperature == 21
    assert manager.missed_responses[MAC] == 1


@pytest.mark.asyncio
async def test_missed_responses_are_counted_and_cleared(manager, device):
    """Silence accumulates, and a unit coming back resets the count"""
    device.alive = False
    await manager._get_device_view_model(MAC)
    await manager._get_device_view_model(MAC)
    assert manager.missed_responses[MAC] == 2

    device.alive = True
    await manager._get_device_view_model(MAC)

    assert manager.missed_responses[MAC] == 0


@pytest.mark.asyncio
async def test_a_silent_device_is_not_reported_as_a_state_change(manager, device):
    """A unit going quiet must not broadcast a spurious report"""
    manager.view_models[MAC] = await manager._get_device_view_model(MAC)
    device.alive = False

    broadcasts = await run_polling_briefly(manager, seconds=2.5)

    assert not broadcasts
    assert manager.missed_responses[MAC] > 0


@pytest.mark.asyncio
async def test_a_unit_that_never_reports_its_state_is_discarded(cli_args, discovery):
    """Answering the handshake is not enough to be considered usable"""
    closed: list = []

    def goes_quiet_after_bind(info, *args, **kwargs):
        unit = FakeDevice(info, *args, **kwargs)
        original_close = unit.close

        def close():
            closed.append(unit)
            original_close()

        def answer(packet_type: str) -> None:
            FakeDevice._answer(unit, packet_type)  # pylint: disable=protected-access
            unit.alive = False  # answers the handshake, then goes quiet

        unit.close = close  # type: ignore[method-assign]
        unit._answer = answer  # type: ignore[method-assign]  # pylint: disable=protected-access
        return unit

    discovery([device_info()], factory=goes_quiet_after_bind)

    climate_manager = main.GreeClimateManager(cli_args)
    macs = await climate_manager.discover_devices()
    await climate_manager.stop_polling()

    assert macs == []
    assert closed, "the discarded unit's socket was left open"


@pytest.mark.asyncio
async def test_units_are_bound_concurrently(cli_args, discovery):
    """Three slow units must bind in parallel, not one after another"""

    def slow(info, *args, **kwargs):
        unit = FakeDevice(info, *args, **kwargs)
        unit.response_delay = 0.3
        return unit

    discovery([device_info(mac=f"aabbcc00112{n}") for n in range(3)], factory=slow)

    climate_manager = main.GreeClimateManager(cli_args)
    started = time.monotonic()
    macs = await climate_manager.discover_devices()
    elapsed = time.monotonic() - started
    await climate_manager.stop_polling()

    assert len(macs) == 3
    # One handshake plus one state exchange, all three units at once. Serially
    # this would be at least 3 * (0.3 + 0.3) = 1.8s.
    assert elapsed < 1.0, f"binding appears to be sequential: {elapsed:.2f}s"
