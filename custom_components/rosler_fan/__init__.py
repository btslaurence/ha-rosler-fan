"""Rosler ceiling fans, controlled through a Bond Bridge."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import BondAuthError, BondBridge, BondError
from .const import (
    CONF_CHECKSUM_CONSTANT,
    CONF_FREQUENCY,
    CONF_REMOTE_ID,
    CONF_REPEATS,
    DEFAULT_FREQUENCY_KHZ,
    DEFAULT_REPEATS,
)
from .controller import STORAGE_VERSION, RoslerController, storage_key

PLATFORMS = [Platform.BUTTON, Platform.FAN]

type RoslerConfigEntry = ConfigEntry[RoslerController]


async def async_setup_entry(hass: HomeAssistant, entry: RoslerConfigEntry) -> bool:
    """Set up a fan from a config entry."""
    bridge = BondBridge(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_ACCESS_TOKEN],
    )
    try:
        await bridge.check_token()
    except BondAuthError as err:
        raise ConfigEntryError(
            "The Bond Bridge refused the token. Reconfigure the integration."
        ) from err
    except BondError as err:
        raise ConfigEntryNotReady(str(err)) from err

    store: Store[dict[str, int]] = Store(
        hass, STORAGE_VERSION, storage_key(entry.entry_id)
    )
    stored = await store.async_load() or {}

    entry.runtime_data = RoslerController(
        hass,
        bridge,
        store,
        remote_id=entry.data[CONF_REMOTE_ID],
        constant=entry.data[CONF_CHECKSUM_CONSTANT],
        frequency_khz=int(entry.options.get(CONF_FREQUENCY, DEFAULT_FREQUENCY_KHZ)),
        repeats=int(entry.options.get(CONF_REPEATS, DEFAULT_REPEATS)),
        counter_index=int(stored.get("counter_index", 0)),
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RoslerConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_save()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: RoslerConfigEntry) -> None:
    """Delete the stored counter of a removed entry."""
    await Store(hass, STORAGE_VERSION, storage_key(entry.entry_id)).async_remove()
