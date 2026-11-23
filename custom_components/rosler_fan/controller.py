"""Builds and sends the commands of one fan."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .api import BondAuthError, BondBridge, BondError
from .const import DOMAIN
from .protocol import COUNTER_SEQUENCE, Command, tx_duration, tx_signal

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
_SAVE_DELAY = 10
# Pause after a transmission so the next one cannot run into it.
_TX_MARGIN = 0.07


def storage_key(entry_id: str) -> str:
    """Return the storage key that holds an entry's counter."""
    return f"{DOMAIN}.{entry_id}"


class RoslerController:
    """Sends commands through a Bond Bridge, one at a time.

    The fan ignores a frame whose counter equals the last one it accepted, so
    every command moves on to the next counter value. The position is saved
    so that it survives a restart.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        bridge: BondBridge,
        store: Store[dict[str, int]],
        *,
        remote_id: int,
        constant: int,
        frequency_khz: int,
        repeats: int,
        counter_index: int = 0,
    ) -> None:
        """Initialize the controller."""
        self.hass = hass
        self.bridge = bridge
        self._store = store
        self._remote_id = remote_id
        self._constant = constant
        self._frequency_khz = frequency_khz
        self._repeats = repeats
        self._index = counter_index % len(COUNTER_SEQUENCE)
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[Command], None]] = []

    @property
    def counter_index(self) -> int:
        """Return the position in the counter sequence."""
        return self._index

    @callback
    def async_add_listener(self, listener: Callable[[Command], None]) -> CALLBACK_TYPE:
        """Call listener with every command that was sent."""
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            self._listeners.remove(listener)

        return remove

    @callback
    def _data_to_save(self) -> dict[str, int]:
        return {"counter_index": self._index}

    async def async_save(self) -> None:
        """Write the counter position now."""
        await self._store.async_save(self._data_to_save())

    async def async_send(self, command: Command) -> None:
        """Transmit a command and tell the listeners about it."""
        async with self._lock:
            # Move on even if the request fails: the bridge may have sent the
            # frame anyway, and reusing its counter would get the next one
            # ignored.
            self._index = (self._index + 1) % len(COUNTER_SEQUENCE)
            self._store.async_delay_save(self._data_to_save, _SAVE_DELAY)
            signal = tx_signal(
                self._remote_id,
                command,
                COUNTER_SEQUENCE[self._index],
                self._constant,
                frequency_khz=self._frequency_khz,
                repeats=self._repeats,
            )
            try:
                await self.bridge.transmit(signal)
            except BondAuthError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="auth_failed"
                ) from err
            except BondError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="transmit_failed",
                    translation_placeholders={"error": str(err)},
                ) from err
            _LOGGER.debug("Sent %s", command.name)
            await asyncio.sleep(tx_duration(self._repeats) + _TX_MARGIN)

        for listener in list(self._listeners):
            listener(command)
