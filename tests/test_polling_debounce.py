"""Tests for the suppression of one-degree sensor jitter in the polling loop."""

import asyncio

import pytest

from conftest import MAC, mock_state, run_polling_briefly

# The polling loop and the view builder are internal; these tests drive them directly.
# pylint: disable=protected-access


@pytest.mark.asyncio
async def test_a_sensor_that_starts_reporting_is_not_treated_as_jitter(manager):
    """A null to value transition is a real change and must be reported.

    Regression test: the jitter filter computed abs(new - old) on values that
    are Optional, so the first reading after a silent sensor raised TypeError.
    The polling loop swallowed it and simply stopped reporting anything.
    """
    seed = await manager.device_view(MAC, update_state=False)
    seed.current_humidity = None  # as if the unit had not answered yet
    manager.view_models[MAC] = seed
    # Inside the one minute window, so the jitter filter is the code under test
    manager.measurement_timestamps[MAC] = asyncio.get_running_loop().time()

    broadcasts = await run_polling_briefly(manager)

    reports = [b for b in broadcasts if b["type"] == "report"]
    assert reports, "the sensor coming back was never reported"
    assert reports[0]["data"]["current_humidity"] == {"old": None, "new": 47}


@pytest.mark.asyncio
async def test_a_sensor_that_goes_quiet_is_reported(manager, device):
    """A value to null transition is reported too"""
    seed = await manager.device_view(MAC, update_state=False)
    manager.view_models[MAC] = seed
    manager.measurement_timestamps[MAC] = asyncio.get_running_loop().time()
    # The unit drops the humidity field from its answers. greeclimate only ever
    # merges into its property cache, so the cached value has to go as well.
    silent_state = mock_state()
    del silent_state["DwatSen"]
    device.state = silent_state
    device._properties.pop("DwatSen")

    broadcasts = await run_polling_briefly(manager)

    reports = [b for b in broadcasts if b["type"] == "report"]
    assert reports
    assert reports[0]["data"]["current_humidity"]["old"] == 47


@pytest.mark.asyncio
async def test_one_degree_jitter_is_suppressed(manager):
    """A single degree wobble soon after a reading is not worth reporting"""
    seed = await manager.device_view(MAC, update_state=False)
    seed.current_temperature = seed.current_temperature + 1
    manager.view_models[MAC] = seed
    manager.measurement_timestamps[MAC] = asyncio.get_running_loop().time()

    broadcasts = await run_polling_briefly(manager)

    assert not broadcasts, f"a one degree wobble was reported: {broadcasts}"


@pytest.mark.asyncio
async def test_a_real_temperature_move_is_reported(manager):
    """A change of more than one degree is always reported"""
    seed = await manager.device_view(MAC, update_state=False)
    seed.current_temperature = seed.current_temperature + 4
    manager.view_models[MAC] = seed
    manager.measurement_timestamps[MAC] = asyncio.get_running_loop().time()

    broadcasts = await run_polling_briefly(manager)

    reports = [b for b in broadcasts if b["type"] == "report"]
    assert reports
    assert "current_temperature" in reports[0]["data"]


@pytest.mark.asyncio
async def test_jitter_is_reported_once_the_window_has_passed(manager):
    """After a minute of quiet even a one degree change is worth sending"""
    seed = await manager.device_view(MAC, update_state=False)
    seed.current_temperature = seed.current_temperature + 1
    manager.view_models[MAC] = seed
    manager.measurement_timestamps[MAC] = asyncio.get_running_loop().time() - 120

    broadcasts = await run_polling_briefly(manager)

    reports = [b for b in broadcasts if b["type"] == "report"]
    assert reports
    assert "current_temperature" in reports[0]["data"]
