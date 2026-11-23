"""Diagnostics for the Rosler ceiling fan integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_HOST
from homeassistant.core import HomeAssistant

from . import RoslerConfigEntry
from .const import CONF_BOND_ID, CONF_REMOTE_ID

# The remote id is all it takes to control the fan by radio, so it is treated
# like a password. The entry's unique id is derived from it.
TO_REDACT = {CONF_ACCESS_TOKEN, CONF_HOST, CONF_REMOTE_ID, CONF_BOND_ID, "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RoslerConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "counter_index": entry.runtime_data.counter_index,
    }
