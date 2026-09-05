"""A greeclimate device whose state requests can be awaited."""

import asyncio
import time
from contextlib import contextmanager
from typing import Optional

from greeclimate.device import Device


class GreeDevice(Device):
    """A greeclimate device whose state requests can be awaited.

    greeclimate speaks UDP and never waits: `update_state()` puts a request on
    the wire and returns, while the answer arrives later through
    `handle_state_update`. Reading the properties straight after the call
    therefore returns the *previous* answer, and a unit that has stopped
    answering is indistinguishable from one that has nothing new to say.

    This subclass signals the arrival of an answer, so a caller can wait for the
    state it just asked for and find out whether it ever came.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._state_received = asyncio.Event()
        self.last_response: Optional[float] = None

    def handle_state_update(self, **kwargs) -> None:
        super().handle_state_update(**kwargs)
        self.last_response = time.monotonic()
        self._state_received.set()

    @property
    def silent_for(self) -> Optional[float]:
        """Seconds since the unit last answered, or None if it never has"""
        return None if self.last_response is None else time.monotonic() - self.last_response

    @contextmanager
    def rollback_on_failure(self):
        """Undo pending property changes when the command fails to land.

        Setting a property writes it straight into the local property cache, so
        a command that never reaches the unit would otherwise be reported as the
        device's state until the next successful read contradicted it.
        """
        properties = dict(self._properties)
        dirty = list(self._dirty)
        try:
            yield
        except Exception:
            self._properties = properties
            self._dirty = dirty
            raise

    async def refresh_state(self, timeout: float) -> bool:
        """Ask for the current state and wait for it.

        Returns True when an answer arrived, False when the unit stayed silent.
        """
        self._state_received.clear()
        await self.update_state()

        try:
            await asyncio.wait_for(self._state_received.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
