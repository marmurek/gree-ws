"""Errors the device layer raises, free of any transport vocabulary.

The API layer decides what these mean over HTTP or WebSocket; nothing below it
needs to know about status codes.
"""


class GreeError(Exception):
    """Base class for everything this application raises deliberately"""


class DeviceNotFound(GreeError):
    """No device is registered under the requested address"""


class DeviceUnavailable(GreeError):
    """The device did not answer, or did not acknowledge the command"""


class InvalidCommand(GreeError):
    """The request cannot be expressed as something the device understands"""
