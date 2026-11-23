"""Fan entity of a Rosler ceiling fan."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from homeassistant.components.fan import (
    DIRECTION_FORWARD,
    DIRECTION_REVERSE,
    FanEntity,
    FanEntityFeature,
)
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)

from . import RoslerConfigEntry
from .entity import RoslerEntity
from .protocol import SPEED_COMMANDS, Command

PARALLEL_UPDATES = 0

SPEED_RANGE = (1, len(SPEED_COMMANDS))
PRESET_MODE_BREEZE = "breeze"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoslerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the fan."""
    async_add_entities([RoslerFan(entry)])


@dataclass
class RoslerFanExtraData(ExtraStoredData):
    """The speed to return to when the fan is switched on without one."""

    last_speed: int

    def as_dict(self) -> dict[str, Any]:
        """Return the data as a dict."""
        return {"last_speed": self.last_speed}


class RoslerFan(RoslerEntity, FanEntity, RestoreEntity):
    """A ceiling fan that cannot report its state.

    The state shown is what was last sent from Home Assistant. Anything done
    with the handheld remote is not seen.
    """

    _attr_name = None
    _attr_assumed_state = True
    _attr_translation_key = "fan"
    _attr_speed_count = len(SPEED_COMMANDS)
    _attr_preset_modes = [PRESET_MODE_BREEZE]
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.DIRECTION
        | FanEntityFeature.PRESET_MODE
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )

    def __init__(self, entry: RoslerConfigEntry) -> None:
        """Initialize the fan."""
        super().__init__(entry)
        self._attr_unique_id = entry.entry_id
        self._attr_percentage = 0
        self._last_speed = 1

    async def async_added_to_hass(self) -> None:
        """Restore the last known state and follow the commands that are sent."""
        await super().async_added_to_hass()

        if (extra := await self.async_get_last_extra_data()) is not None:
            last_speed = extra.as_dict().get("last_speed")
            if isinstance(last_speed, int) and last_speed in range(1, 7):
                self._last_speed = last_speed

        if (state := await self.async_get_last_state()) is not None:
            direction = state.attributes.get("direction")
            if direction in (DIRECTION_FORWARD, DIRECTION_REVERSE):
                self._attr_current_direction = direction
            if state.state == STATE_ON:
                if state.attributes.get("preset_mode") == PRESET_MODE_BREEZE:
                    self._attr_preset_mode = PRESET_MODE_BREEZE
                else:
                    self._attr_percentage = ranged_value_to_percentage(
                        SPEED_RANGE, self._last_speed
                    )

        self.async_on_remove(self._controller.async_add_listener(self._handle_command))

    @property
    def extra_restore_state_data(self) -> RoslerFanExtraData:
        """Return the data to keep across restarts."""
        return RoslerFanExtraData(self._last_speed)

    @callback
    def _handle_command(self, command: Command) -> None:
        """Update the assumed state after a command went out."""
        if command in SPEED_COMMANDS:
            self._last_speed = SPEED_COMMANDS.index(command) + 1
            self._attr_percentage = ranged_value_to_percentage(
                SPEED_RANGE, self._last_speed
            )
            self._attr_preset_mode = None
        elif command in (Command.FAN_OFF, Command.ALL_OFF):
            self._attr_percentage = 0
            self._attr_preset_mode = None
        elif command is Command.BREEZE:
            self._attr_percentage = 0
            self._attr_preset_mode = PRESET_MODE_BREEZE
        elif command is Command.FORWARD:
            self._attr_current_direction = DIRECTION_FORWARD
        elif command is Command.REVERSE:
            self._attr_current_direction = DIRECTION_REVERSE
        else:
            return
        self.async_write_ha_state()

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn the fan on, at the last speed unless one is given."""
        if preset_mode is not None:
            await self.async_set_preset_mode(preset_mode)
        elif percentage is not None:
            await self.async_set_percentage(percentage)
        else:
            await self._controller.async_send(SPEED_COMMANDS[self._last_speed - 1])

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the fan."""
        await self._controller.async_send(Command.FAN_OFF)

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the speed, 0 meaning off."""
        if percentage <= 0:
            await self.async_turn_off()
            return
        speed = math.ceil(percentage_to_ranged_value(SPEED_RANGE, percentage))
        speed = max(SPEED_RANGE[0], min(SPEED_RANGE[1], speed))
        await self._controller.async_send(SPEED_COMMANDS[speed - 1])

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Switch to natural wind."""
        await self._controller.async_send(Command.BREEZE)

    async def async_set_direction(self, direction: str) -> None:
        """Set the direction of rotation."""
        await self._controller.async_send(
            Command.REVERSE if direction == DIRECTION_REVERSE else Command.FORWARD
        )
