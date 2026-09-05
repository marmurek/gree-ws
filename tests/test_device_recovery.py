"""Tests for the device roster: rebuilding, dropping and releasing units.

A unit that is power cycled stops answering while its socket, its session key
and its address may all have changed underneath us. Rebinding the object we
already hold cannot fix that - greeclimate never clears its `ready` event, so a
second bind() returns at once without a handshake, having already replaced the
session key with the generic one. Recovery has to build a new object.
"""

import asyncio

import pytest
from fastapi import HTTPException

import main
from conftest import MAC, device_info, mock_state

# The roster internals are what these tests are about.
# pylint: disable=protected-access


@pytest.fixture(name="fast_args")
def fast_args_fixture(cli_args, monkeypatch):
    """Timings short enough to watch a unit be declared gone and rebuilt"""
    cli_args.response_timeout = 0.2
    cli_args.polling_interval = 0.1
    cli_args.discovery_timeout = 0
    monkeypatch.setattr(main, "UNRESPONSIVE_AFTER", 2)
    return cli_args


@pytest.mark.asyncio
async def test_a_silent_unit_is_rebuilt(fast_args, discovery):
    """The polling loop notices sustained silence and builds a new device"""
    created = discovery([device_info()])
    manager = main.GreeClimateManager(fast_args)
    await manager.discover_devices()
    original = created[0]

    original.alive = False  # the unit is power cycled
    await asyncio.sleep(2.0)
    await manager.stop_polling()

    assert len(created) > 1, "no replacement device was built"
    assert manager.devices[MAC] is not original
    assert manager.devices[MAC].device_key is not None
    assert manager.missed_responses[MAC] == 0


@pytest.mark.asyncio
async def test_recovery_follows_a_unit_that_changed_address(fast_args, discovery):
    """A unit that comes back on a new IP is rebuilt against that address"""
    infos = [device_info(ip="1.1.1.0")]
    created = discovery(infos)
    manager = main.GreeClimateManager(fast_args)
    await manager.discover_devices()

    created[0].alive = False
    infos[0] = device_info(ip="1.1.1.99")  # the unit came back on a new lease

    await asyncio.sleep(2.0)
    await manager.stop_polling()

    assert manager.devices[MAC].device_info.ip == "1.1.1.99"


@pytest.mark.asyncio
async def test_the_old_socket_is_released_when_a_unit_is_rebuilt(fast_args, discovery):
    """Rebuilding must not leak the socket of the device it replaces"""
    created = discovery([device_info()])
    manager = main.GreeClimateManager(fast_args)
    await manager.discover_devices()

    original = created[0]
    closed = []
    original.close = lambda: closed.append(original)
    original.alive = False

    await asyncio.sleep(2.0)
    await manager.stop_polling()

    assert closed, "the replaced device's socket was left open"


@pytest.mark.asyncio
async def test_a_working_unit_is_not_rebound_on_rediscovery(fast_args, discovery):
    """Rediscovery must leave a healthy session alone"""
    created = discovery([device_info()])
    manager = main.GreeClimateManager(fast_args)
    await manager.discover_devices()
    original = manager.devices[MAC]

    await manager.discover_devices()
    await manager.stop_polling()

    assert manager.devices[MAC] is original
    assert len(created) == 1


@pytest.mark.asyncio
async def test_a_unit_that_vanished_and_is_silent_is_dropped(fast_args, discovery):
    """A unit that neither answers nor reappears is forgotten entirely"""
    infos = [device_info()]
    created = discovery(infos)
    manager = main.GreeClimateManager(fast_args)
    await manager.discover_devices()

    created[0].alive = False
    manager.missed_responses[MAC] = main.UNRESPONSIVE_AFTER
    infos.clear()  # it does not answer the broadcast either

    macs = await manager.discover_devices()
    await manager.stop_polling()

    assert macs == []
    assert MAC not in manager.devices
    assert MAC not in manager.view_models
    assert MAC not in manager.missed_responses
    assert MAC not in manager.polling_tasks


@pytest.mark.asyncio
async def test_a_unit_that_missed_the_broadcast_but_still_answers_is_kept(fast_args, discovery):
    """Silence on the broadcast alone is not enough to drop a working unit"""
    infos = [device_info()]
    discovery(infos)
    manager = main.GreeClimateManager(fast_args)
    await manager.discover_devices()
    infos.clear()

    macs = await manager.discover_devices()
    await manager.stop_polling()

    assert macs == [MAC]


@pytest.mark.asyncio
async def test_concurrent_discoveries_do_not_interleave(fast_args, discovery):
    """Two rediscoveries at once are serialised rather than racing"""
    discovery([device_info()])
    manager = main.GreeClimateManager(fast_args)

    results = await asyncio.gather(manager.discover_devices(), manager.discover_devices())
    await manager.stop_polling()

    assert list(results) == [[MAC], [MAC]]
    assert len(manager.devices) == 1


@pytest.mark.asyncio
async def test_stop_polling_waits_for_the_tasks_to_finish(fast_args, discovery):
    """Cancelled loops must be finished before their replacements start"""
    discovery([device_info()])
    manager = main.GreeClimateManager(fast_args)
    await manager.discover_devices()
    tasks = list(manager.polling_tasks.values())

    await manager.stop_polling()

    assert all(task.done() for task in tasks)
    assert not manager.polling_tasks


@pytest.mark.asyncio
async def test_an_unacknowledged_command_leaves_no_phantom_state(manager, device):
    """A command the unit never confirms must not be reported as its state.

    Regression test: properties are written into the local cache as they are
    applied, so a command that never landed used to be served back as the
    device's state until a later read contradicted it.
    """
    before = device.target_temperature
    device.alive = False

    with pytest.raises(HTTPException) as raised:
        await manager.send_update(MAC, main.DeviceUpdateModel(target_temperature=before + 3))

    assert raised.value.status_code == 503
    assert device.target_temperature == before
    assert not device._dirty


@pytest.mark.asyncio
async def test_an_acknowledged_command_is_reported_as_applied(manager, device):
    """A confirmed command leaves the new value in place"""
    target = device.target_temperature + 3
    device.state = mock_state(SetTem=target)

    modified = await manager.send_update(MAC, main.DeviceUpdateModel(target_temperature=target))

    assert modified is True
    assert device.target_temperature == target


@pytest.mark.asyncio
async def test_a_command_is_retried_against_a_rebuilt_device(fast_args, discovery):
    """A command to a unit that has gone quiet rebuilds it and tries again"""
    created = discovery([device_info()])
    manager = main.GreeClimateManager(fast_args)
    await manager.discover_devices()
    await manager.stop_polling()

    original = created[0]
    original.alive = False

    modified = await manager.send_update(MAC, main.DeviceUpdateModel(target_temperature=27))

    assert modified is True
    assert manager.devices[MAC] is not original
    assert "cmd" in [packet["t"] for packet in created[-1].sent]
