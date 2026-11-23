"""Base entity for the Rosler ceiling fan integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from . import RoslerConfigEntry
from .const import DOMAIN, MANUFACTURER, MODEL


class RoslerEntity(Entity):
    """An entity of one fan."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: RoslerConfigEntry) -> None:
        """Initialize the entity."""
        self._controller = entry.runtime_data
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )
