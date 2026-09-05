"""Tests for turning a device's reported state into the API view model."""

# The view model builder is internal on purpose; these tests exercise it directly.
# pylint: disable=protected-access

import pydantic
import pytest

from conftest import MAC, device_info, mock_state
from main import DeviceFanSpeed, DeviceMode, DeviceVerticalSwing


@pytest.mark.asyncio
async def test_reported_state_maps_onto_the_view_model(manager):
    """Every field the unit reports reaches the view model"""
    view = await manager._get_device_view_model(MAC, update_state=False)

    assert view.mac == MAC
    assert view.ip == "1.1.1.0"
    assert view.power is True
    assert view.mode is DeviceMode.heat
    assert view.target_temperature == 21
    assert view.current_temperature == 20  # v4 firmware reports Fahrenheit-ish offsets
    assert view.current_humidity == 47
    assert view.fan_speed is DeviceFanSpeed.medium_low
    assert view.light is True
    assert view.buzzer is True
    assert view.clean_filter is False
    assert view.water_full is False


@pytest.mark.asyncio
async def test_unknown_mac_returns_the_default_view(manager):
    """Asking for a device that is not registered yields the placeholder view"""
    view = await manager._get_device_view_model("ffffffffffff", update_state=False)

    assert view.mac == "000000000000"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dwet, expected",
    [
        (0, None),  # no dehumidifier: the library decodes this as an unsettable 15
        (1, None),  # 20, still below what the device accepts
        (2, None),  # 25
        (3, 30),  # the lowest value the device accepts
        (5, 40),
        (13, 80),  # the highest value the device accepts
    ],
)
async def test_target_humidity_only_reports_settable_values(manager, device, dwet, expected):
    """Values the update model would refuse are reported as null instead"""
    device.handle_state_update(**mock_state(Dwet=dwet))

    view = await manager._get_device_view_model(MAC, update_state=False)

    assert view.target_humidity == expected


@pytest.mark.asyncio
async def test_every_reported_humidity_can_be_sent_back(manager, device):
    """Whatever the view model reports must be accepted by the update model"""
    from main import DeviceUpdateModel  # pylint: disable=import-outside-toplevel

    for dwet in range(0, 16):
        device.handle_state_update(**mock_state(Dwet=dwet))
        reported = (await manager._get_device_view_model(MAC, update_state=False)).target_humidity
        if reported is not None:
            DeviceUpdateModel(target_humidity=reported)  # must not raise


@pytest.mark.asyncio
async def test_buzzer_reflects_the_device_object(manager, device):
    """The buzzer flag is application state, mirrored into the view"""
    device.buzzer = False

    view = await manager._get_device_view_model(MAC, update_state=False)

    assert view.buzzer is False


@pytest.mark.asyncio
async def test_swing_and_mode_enums_round_trip(manager, device):
    """Library enum values are translated to the API enum names"""
    device.handle_state_update(**mock_state(Mod=1, SwUpDn=4, WdSpd=5))

    view = await manager._get_device_view_model(MAC, update_state=False)

    assert view.mode is DeviceMode.cool
    assert view.vertical_swing is DeviceVerticalSwing.fixed_middle
    assert view.fan_speed is DeviceFanSpeed.high


@pytest.mark.asyncio
async def test_a_mac_with_separators_is_normalised(manager, device):
    """However the unit spells its MAC, the API reports bare lower case hex"""
    device.device_info = device_info(mac="AA:BB:CC:00:11:22")

    view = await manager._get_device_view_model(MAC, update_state=False)

    assert view.mac == "aabbcc001122"


def test_the_view_model_validates_what_is_assigned_to_it():
    """Building the view field by field must not bypass the declared types.

    Regression test: without validate_assignment the model accepted anything,
    which is how an unsettable target_humidity used to reach the API.
    """
    from main import create_view_model  # pylint: disable=import-outside-toplevel

    view = create_view_model()

    with pytest.raises(pydantic.ValidationError):
        view.mac = "NOT-A-MAC"
    with pytest.raises(pydantic.ValidationError):
        view.ip = "definitely.not.an.ip"
    with pytest.raises(pydantic.ValidationError):
        view.current_temperature = "hot"  # type: ignore[assignment]  # deliberately wrong
