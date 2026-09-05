"""The single description of how API fields map onto device properties.

Reading a device and writing to one used to be two hand-maintained lists of the
same twenty-odd fields, in different files' worth of if-statements. Keeping them
in step was manual, and when greeclimate renamed a property one side silently
stopped doing anything. One table drives both directions instead.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional, Type

from greeclimate.device import FanSpeed, HorizontalSwing, Mode, VerticalSwing

from gree_ws.conversions import from_device_enum, to_device_enum
from gree_ws.models import (
    HUMIDITY_MAX,
    HUMIDITY_MIN,
    DeviceFanSpeed,
    DeviceHorizontalSwing,
    DeviceMode,
    DeviceVerticalSwing,
)


@dataclass(frozen=True)
class DeviceField:
    """One API field and how it crosses to and from a device property.

    `to_api` turns a device property into the value the API reports; returning
    None means the device has nothing to say and the field keeps its default.
    `to_device` turns a requested value into what the device expects; returning
    None means the request cannot be expressed and must be rejected. A field
    with no `to_device` is read only.
    """

    name: str
    to_api: Callable[[Any], Any] = lambda value: value
    to_device: Optional[Callable[[Any], Any]] = None

    @property
    def writable(self) -> bool:
        """Can this field be set through the API"""
        return self.to_device is not None

    def read(self, device) -> Any:
        """The value this field reports for the given device"""
        return self.to_api(getattr(device, self.name))

    def write(self, device, value: Any) -> None:
        """Set this field on the device, assuming the value converts"""
        setattr(device, self.name, self.to_device(value))  # type: ignore[misc]


def _as_bool(value: Any) -> Optional[bool]:
    """Report a flag, distinguishing off from never reported.

    These fields are declared Optional because a unit need not support them all.
    Coercing an absent one to False would claim the feature exists and is off.
    """
    return None if value is None else bool(value)


def _settable_humidity(value: Any) -> Optional[int]:
    """Report the target humidity only when it is a value the device accepts.

    Units without a dehumidifier report Dwet=0, which greeclimate decodes as 15 -
    a value a request carrying it would be refused for.
    """
    if value is None or not HUMIDITY_MIN <= value <= HUMIDITY_MAX:
        return None
    return value


def _reader(library_enum: Type[Enum], api_enum: Type[Enum]) -> Callable[[Any], Any]:
    """Read a device's raw enum value as the matching API member"""

    def read(value: Any) -> Any:
        if value is None:
            return None
        try:
            member = library_enum(value)
        except ValueError:
            # A value this version of greeclimate has no name for. Reporting the
            # first member keeps the polling loop alive; raising would freeze it.
            member = list(library_enum)[0]
        return to_device_enum(member, api_enum)

    return read


def _writer(library_enum: Type[Enum]) -> Callable[[Any], Any]:
    """Turn an API enum member into the raw value the device expects"""

    def write(value: Any) -> Any:
        member = from_device_enum(value, library_enum)
        return None if member is None else member.value

    return write


def _enum_field(name: str, library_enum: Type[Enum], api_enum: Type[Enum]) -> DeviceField:
    """A field whose device value is an integer from a greeclimate enum"""
    return DeviceField(name, to_api=_reader(library_enum, api_enum), to_device=_writer(library_enum))


def _flag(name: str) -> DeviceField:
    """A boolean field the device stores as an integer"""
    return DeviceField(name, to_api=_as_bool, to_device=bool)


def _readonly_flag(name: str) -> DeviceField:
    """A boolean the device reports but does not accept"""
    return DeviceField(name, to_api=_as_bool)


DEVICE_FIELDS: tuple[DeviceField, ...] = (
    DeviceField("power", to_device=bool),
    _enum_field("mode", Mode, DeviceMode),
    DeviceField("current_temperature"),
    DeviceField("target_temperature", to_device=int),
    DeviceField("current_humidity"),
    DeviceField("target_humidity", to_api=_settable_humidity, to_device=int),
    _enum_field("fan_speed", FanSpeed, DeviceFanSpeed),
    _enum_field("horizontal_swing", HorizontalSwing, DeviceHorizontalSwing),
    _enum_field("vertical_swing", VerticalSwing, DeviceVerticalSwing),
    _flag("turbo"),
    # The device stores quiet as 2 or 0, so it has to be compared as a boolean
    _flag("quiet"),
    _flag("light"),
    _flag("fresh_air"),
    _flag("xfan"),
    _flag("anion"),
    _flag("sleep"),
    _flag("power_save"),
    _flag("buzzer"),
    _flag("steady_heat"),
    _readonly_flag("clean_filter"),
    _readonly_flag("water_full"),
)

WRITABLE_FIELDS: tuple[DeviceField, ...] = tuple(field for field in DEVICE_FIELDS if field.writable)
