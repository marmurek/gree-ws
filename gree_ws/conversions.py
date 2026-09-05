"""Translations between greeclimate's vocabulary and the API's."""

import logging
import re
from enum import Enum
from typing import Any, Optional, Type

logger = logging.getLogger(__name__)


def pascal_to_snake(name: str) -> str:
    """Convert PascalCase to snake_case, e.g. PascalCase -> pascal_case"""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def snake_to_pascal(name: str) -> str:
    """Convert snake_case to PascalCase, e.g. snake_case -> SnakeCase"""
    return "".join(word.capitalize() for word in name.split("_"))


def to_device_enum(enum_value: Any, device_enum_cls: Type[Enum]) -> Enum:
    """Convert a library enum member to the API member of the same name.

    A value the API does not know falls back to the first member: reporting an
    approximate state is better than letting the polling loop die on it.
    """
    try:
        return device_enum_cls[pascal_to_snake(enum_value.name)]
    except KeyError:
        fallback = list(device_enum_cls)[0]
        logger.warning(
            "Device reported unknown %s value %s, reporting %s", device_enum_cls.__name__, enum_value, fallback
        )
        return fallback


def from_device_enum(device_enum_value: Any, target_enum_cls: Type[Enum]) -> Optional[Enum]:
    """Convert an API enum member to the library member of the same name.

    Returns None when the value has no counterpart, so the caller can reject the
    request instead of quietly sending the device something else.
    """
    try:
        return target_enum_cls[snake_to_pascal(device_enum_value.name)]
    except KeyError:
        return None


def normalize_mac(mac: str) -> str:
    """Reduce a device MAC to the bare lower case hex the API uses as its key"""
    return mac.replace(":", "").replace("-", "").lower()
