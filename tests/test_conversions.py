"""Tests for the helpers that translate between library and API vocabularies."""

import pytest
from greeclimate.device import FanSpeed, HorizontalSwing, Mode, VerticalSwing

from gree_ws.conversions import (
    from_device_enum,
    normalize_mac,
    pascal_to_snake,
    snake_to_pascal,
    to_device_enum,
)
from gree_ws.models import DeviceFanSpeed, DeviceHorizontalSwing, DeviceMode, DeviceVerticalSwing


class UnknownValue:
    """Stands in for a library enum member the API has never heard of"""

    name = "SomethingNew"


def test_case_conversion_round_trips():
    """The two case helpers are inverses for the names in use"""
    assert pascal_to_snake("FixedUpperMiddle") == "fixed_upper_middle"
    assert snake_to_pascal("fixed_upper_middle") == "FixedUpperMiddle"


@pytest.mark.parametrize(
    "lib_enum, api_enum",
    [
        (Mode, DeviceMode),
        (FanSpeed, DeviceFanSpeed),
        (HorizontalSwing, DeviceHorizontalSwing),
        (VerticalSwing, DeviceVerticalSwing),
    ],
)
def test_every_library_value_has_an_api_counterpart(lib_enum, api_enum):
    """The two vocabularies must stay in step across library upgrades"""
    for member in lib_enum:
        assert to_device_enum(member, api_enum).name == pascal_to_snake(member.name)
    for name in api_enum.__members__:
        assert from_device_enum(api_enum[name], lib_enum) is not None


def test_unknown_device_value_falls_back_instead_of_raising():
    """A value the API does not know must not kill the polling loop.

    Regression test: this used to raise KeyError, because the `if val is None`
    fallback below the lookup could never run.
    """
    assert to_device_enum(UnknownValue(), DeviceMode) is DeviceMode.auto


def test_unknown_api_value_is_reported_as_unconvertible():
    """An unconvertible request is rejected rather than silently altered"""
    assert from_device_enum(UnknownValue(), Mode) is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("aabbcc001122", "aabbcc001122"),
        ("AA:BB:CC:00:11:22", "aabbcc001122"),
        ("aa-bb-cc-00-11-22", "aabbcc001122"),
        ("AABBCC001122", "aabbcc001122"),
    ],
)
def test_mac_normalisation(raw, expected):
    """MACs reach the API as bare lower case hex whatever the device sends"""
    assert normalize_mac(raw) == expected
