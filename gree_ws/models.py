"""The API's data contract: the models served and accepted over REST and WebSocket."""

from enum import Enum
from typing import Annotated, List, Optional

from greeclimate.device import HUMIDITY_MAX, HUMIDITY_MIN
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# The device stores the target humidity as (value - 15) / 5, so only multiples of 5
# survive a write/read round trip. HUMIDITY_MIN/HUMIDITY_MAX come from greeclimate.
HUMIDITY_STEP = 5

type MacAddress = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{12}$"),
]
type IpAddress = Annotated[
    str,
    StringConstraints(pattern=r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$"),
]


class DeviceMode(Enum):
    """Device operating modes"""

    auto = "auto"
    cool = "cool"
    dry = "dry"
    fan = "fan"
    heat = "heat"


class DeviceFanSpeed(Enum):
    """Device fan speed settings"""

    auto = "auto"
    low = "low"
    medium_low = "medium_low"
    medium = "medium"
    medium_high = "medium_high"
    high = "high"


class DeviceHorizontalSwing(Enum):
    """Device horizontal swing settings"""

    default = "default"
    full_swing = "full_swing"
    left = "left"
    left_center = "left_center"
    center = "center"
    right_center = "right_center"
    right = "right"


class DeviceVerticalSwing(Enum):
    """Device vertical swing settings"""

    default = "default"
    full_swing = "full_swing"
    fixed_upper = "fixed_upper"
    fixed_upper_middle = "fixed_upper_middle"
    fixed_middle = "fixed_middle"
    fixed_lower_middle = "fixed_lower_middle"
    fixed_lower = "fixed_lower"
    swing_upper = "swing_upper"
    swing_upper_middle = "swing_upper_middle"
    swing_middle = "swing_middle"
    swing_lower_middle = "swing_lower_middle"
    swing_lower = "swing_lower"


class DeviceViewModel(BaseModel):
    """Device view model for API responses"""

    # The model is built field by field from the device, so without this the
    # declared types and patterns would never actually be checked.
    model_config = ConfigDict(validate_assignment=True)

    mac: MacAddress = Field("000000000000", description="MAC address of the device")
    ip: IpAddress = Field("0.0.0.0", description="IP address of the device")
    power: bool = Field(False, description="Power state of the device")
    mode: DeviceMode = Field(DeviceMode.auto, description="Operating mode of the device")
    current_temperature: Optional[int] = Field(None, description="Current temperature reported by the device")
    target_temperature: int = Field(16, description="Target temperature set on the device")
    current_humidity: Optional[int] = Field(None, description="Current humidity reported by the device")
    target_humidity: Optional[int] = Field(
        None,
        description=f"Target humidity set on the device ({HUMIDITY_MIN}-{HUMIDITY_MAX}, step {HUMIDITY_STEP}), null when the device does not report a settable value",
    )
    fan_speed: DeviceFanSpeed = Field(DeviceFanSpeed.auto, description="Fan speed setting of the device")
    horizontal_swing: DeviceHorizontalSwing = Field(
        DeviceHorizontalSwing.default, description="Horizontal swing setting of the device"
    )
    vertical_swing: DeviceVerticalSwing = Field(
        DeviceVerticalSwing.default, description="Vertical swing setting of the device"
    )
    turbo: Optional[bool] = Field(None, description="Turbo mode state")
    quiet: Optional[bool] = Field(None, description="Quiet mode state")
    light: Optional[bool] = Field(None, description="Light or backlight state")
    fresh_air: Optional[bool] = Field(None, description="Fresh air mode state")
    xfan: Optional[bool] = Field(None, description="XFan mode state")
    anion: Optional[bool] = Field(None, description="Anion mode state")
    sleep: Optional[bool] = Field(None, description="Sleep mode state")
    power_save: Optional[bool] = Field(None, description="Power save mode state")
    buzzer: Optional[bool] = Field(
        None, description="Buzzer state, when disabled the unit does not beep on each command"
    )
    clean_filter: Optional[bool] = Field(None, description="Clean filter indicator state")
    water_full: Optional[bool] = Field(None, description="Water full indicator state")
    steady_heat: Optional[bool] = Field(None, description="Steady heat mode state")


class DeviceUpdateModel(BaseModel):
    """Device update model for API requests"""

    power: Optional[bool] = Field(None, description="Power state of the device to set")
    mode: Optional[DeviceMode] = Field(None, description="Operating mode of the device to set")
    target_temperature: Optional[int] = Field(
        None, ge=16, le=30, description="Target temperature (16-30, step 1) to set", json_schema_extra={"step": 1}
    )
    target_humidity: Optional[int] = Field(
        None,
        ge=HUMIDITY_MIN,
        le=HUMIDITY_MAX,
        multiple_of=HUMIDITY_STEP,
        description=f"Target humidity ({HUMIDITY_MIN}-{HUMIDITY_MAX}, step {HUMIDITY_STEP}) to set",
        json_schema_extra={"step": HUMIDITY_STEP},
    )
    fan_speed: Optional[DeviceFanSpeed] = Field(None, description="Fan speed setting of the device to set")
    horizontal_swing: Optional[DeviceHorizontalSwing] = Field(
        None, description="Horizontal swing setting of the device to set"
    )
    vertical_swing: Optional[DeviceVerticalSwing] = None
    turbo: Optional[bool] = None
    quiet: Optional[bool] = None
    light: Optional[bool] = None
    fresh_air: Optional[bool] = None
    xfan: Optional[bool] = None
    anion: Optional[bool] = None
    sleep: Optional[bool] = None
    power_save: Optional[bool] = None
    buzzer: Optional[bool] = Field(
        None, description="Buzzer state to set, set to false to silence the beep on each command"
    )
    steady_heat: Optional[bool] = None


class RootResponse(BaseModel):
    """Root endpoint response model"""

    app: str
    version: str
    devices: List[MacAddress]


def create_view_model() -> DeviceViewModel:
    """Create a default DeviceViewModel instance.

    Every placeholder is already declared as the field's default, so there is
    nothing to repeat here - and nothing that can drift out of step with it.
    """
    return DeviceViewModel()
