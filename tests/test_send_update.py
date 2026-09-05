"""Tests for turning an API request into a command packet."""

import pydantic
import pytest

from conftest import MAC
from main import DeviceFanSpeed, DeviceMode, DeviceUpdateModel, DeviceVerticalSwing, HUMIDITY_MAX, HUMIDITY_MIN


@pytest.mark.asyncio
async def test_update_is_encoded_into_a_command_packet(manager, device):
    """Requested fields reach the device under their protocol names"""
    modified = await manager.send_update(
        MAC,
        DeviceUpdateModel(
            power=False,
            mode=DeviceMode.cool,
            target_temperature=22,
            fan_speed=DeviceFanSpeed.medium,
            vertical_swing=DeviceVerticalSwing.fixed_middle,
            target_humidity=45,
            turbo=True,
            light=False,
        ),
    )

    assert modified is True
    command = device.last_command()
    assert command["Pow"] == 0
    assert command["Mod"] == 1
    assert command["SetTem"] == 22
    assert command["WdSpd"] == 3
    assert command["SwUpDn"] == 4
    assert command["Dwet"] == 6
    assert command["Tur"] == 1
    assert command["Lig"] == 0


@pytest.mark.asyncio
async def test_unchanged_values_report_no_modification(manager):
    """Sending the value the device already holds is not a change"""
    modified = await manager.send_update(MAC, DeviceUpdateModel(target_temperature=21))

    assert modified is False


@pytest.mark.asyncio
async def test_only_requested_fields_are_sent(manager, device):
    """Fields left out of the request are not part of the command"""
    await manager.send_update(MAC, DeviceUpdateModel(power=False))

    command = device.last_command()
    assert set(command) == {"Pow"}


@pytest.mark.asyncio
async def test_buzzer_disabled_adds_the_suppression_flag(manager, device):
    """buzzer=false silences the unit on every command"""
    await manager.send_update(MAC, DeviceUpdateModel(buzzer=False, power=False))

    assert device.last_command().get("Buzzer_ON_OFF") == 1
    assert device.buzzer is False


@pytest.mark.asyncio
async def test_buzzer_enabled_is_the_default_and_adds_nothing(manager, device):
    """The library default leaves the beep on and sends no extra flag"""
    await manager.send_update(MAC, DeviceUpdateModel(power=False))

    assert "Buzzer_ON_OFF" not in device.last_command()
    assert device.buzzer is True


@pytest.mark.parametrize("value", [29, 31, 42, 81, 90, 0])
def test_target_humidity_out_of_range_is_rejected(value):
    """Only multiples of 5 within the range the device accepts are valid"""
    with pytest.raises(pydantic.ValidationError):
        DeviceUpdateModel(target_humidity=value)


@pytest.mark.parametrize("value", [HUMIDITY_MIN, 45, 60, HUMIDITY_MAX])
def test_target_humidity_in_range_is_accepted(value):
    """Values the device accepts pass validation"""
    assert DeviceUpdateModel(target_humidity=value).target_humidity == value


@pytest.mark.parametrize("value", [15, 31])
def test_target_temperature_out_of_range_is_rejected(value):
    """The temperature range is enforced by the model"""
    with pytest.raises(pydantic.ValidationError):
        DeviceUpdateModel(target_temperature=value)
