"""Buttons for the remote functions that are not part of the fan entity."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RoslerConfigEntry
from .entity import RoslerEntity
from .protocol import Command

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class RoslerButtonDescription(ButtonEntityDescription):
    """A remote button."""

    command: Command


BUTTONS: tuple[RoslerButtonDescription, ...] = (
    RoslerButtonDescription(
        key="all_off", translation_key="all_off", command=Command.ALL_OFF
    ),
    RoslerButtonDescription(
        key="timer_1h", translation_key="timer_1h", command=Command.TIMER_1H
    ),
    RoslerButtonDescription(
        key="timer_2h", translation_key="timer_2h", command=Command.TIMER_2H
    ),
    RoslerButtonDescription(
        key="timer_4h", translation_key="timer_4h", command=Command.TIMER_4H
    ),
    # The light button toggles, and fans without a light ignore it, so it is
    # off until someone enables it.
    RoslerButtonDescription(
        key="light",
        translation_key="light",
        command=Command.LIGHT,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoslerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the buttons."""
    async_add_entities(RoslerButton(entry, description) for description in BUTTONS)


class RoslerButton(RoslerEntity, ButtonEntity):
    """A button that sends one remote command."""

    entity_description: RoslerButtonDescription

    def __init__(
        self, entry: RoslerConfigEntry, description: RoslerButtonDescription
    ) -> None:
        """Initialize the button."""
        super().__init__(entry)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

    async def async_press(self) -> None:
        """Send the command."""
        await self._controller.async_send(self.entity_description.command)
