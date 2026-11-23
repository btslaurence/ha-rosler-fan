"""Tests for the fan entity and the buttons."""

from __future__ import annotations

import itertools
from collections.abc import Callable

import aiohttp
import pytest
from homeassistant.components.fan import DOMAIN as FAN_DOMAIN
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.rosler_fan.const import DOMAIN
from custom_components.rosler_fan.protocol import COUNTER_SEQUENCE, Capture, Command

from .common import CONSTANT, REMOTE_ID
from .conftest import BASE

FAN = "fan.ceiling_fan"


async def _call(hass: HomeAssistant, service: str, **data: object) -> None:
    await hass.services.async_call(
        FAN_DOMAIN, service, {"entity_id": FAN, **data}, blocking=True
    )


async def test_device_and_entities(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """One device is created, holding the fan and the buttons."""
    devices = dr.async_entries_for_config_entry(
        dr.async_get(hass), setup_entry.entry_id
    )
    assert len(devices) == 1
    assert devices[0].name == "Ceiling Fan"
    assert devices[0].manufacturer == "Rosler"

    registry = er.async_get(hass)
    entities = er.async_entries_for_device(
        registry, devices[0].id, include_disabled_entities=True
    )
    assert {entity.entity_id for entity in entities} == {
        FAN,
        "button.ceiling_fan_all_off",
        "button.ceiling_fan_timer_1_hour",
        "button.ceiling_fan_timer_2_hours",
        "button.ceiling_fan_timer_4_hours",
        "button.ceiling_fan_light_toggle",
    }
    # The light button is off until enabled: not every fan has a light.
    assert registry.async_get("button.ceiling_fan_light_toggle").disabled
    assert hass.states.get("button.ceiling_fan_light_toggle") is None

    state = hass.states.get(FAN)
    assert state.state == STATE_OFF
    assert state.attributes["assumed_state"] is True
    assert state.attributes["preset_modes"] == ["breeze"]
    assert state.attributes["percentage_step"] == pytest.approx(100 / 6)


@pytest.mark.parametrize(
    ("percentage", "command"),
    [
        (1, Command.SPEED_1),
        (16, Command.SPEED_1),
        (17, Command.SPEED_2),
        (33, Command.SPEED_2),
        (50, Command.SPEED_3),
        (66, Command.SPEED_4),
        (83, Command.SPEED_5),
        (84, Command.SPEED_6),
        (100, Command.SPEED_6),
    ],
)
async def test_set_percentage(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    sent: Callable[[], list[Capture]],
    percentage: int,
    command: Command,
) -> None:
    """A percentage maps onto one of the six speeds."""
    await _call(hass, "set_percentage", percentage=percentage)
    (capture,) = sent()
    assert capture.frame.command is command
    assert capture.frame.remote_id == REMOTE_ID
    assert capture.frame.constant == CONSTANT
    assert capture.matching == 7
    assert hass.states.get(FAN).state == STATE_ON


async def test_commands_and_assumed_state(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    sent: Callable[[], list[Capture]],
) -> None:
    """Each service sends one frame and updates the assumed state."""
    await _call(hass, "turn_on")
    assert hass.states.get(FAN).attributes["percentage"] == 16

    await _call(hass, "turn_on", percentage=50)
    assert hass.states.get(FAN).attributes["percentage"] == 50

    await _call(hass, "set_direction", direction="reverse")
    assert hass.states.get(FAN).attributes["direction"] == "reverse"
    await _call(hass, "set_direction", direction="forward")
    assert hass.states.get(FAN).attributes["direction"] == "forward"

    await _call(hass, "set_preset_mode", preset_mode="breeze")
    state = hass.states.get(FAN)
    assert state.state == STATE_ON
    assert state.attributes["preset_mode"] == "breeze"
    assert state.attributes["percentage"] == 0

    await _call(hass, "turn_off")
    state = hass.states.get(FAN)
    assert state.state == STATE_OFF
    assert state.attributes["preset_mode"] is None

    # Without a speed, the fan comes back at the last one.
    await _call(hass, "turn_on")
    assert hass.states.get(FAN).attributes["percentage"] == 50

    await _call(hass, "turn_on", preset_mode="breeze")
    await _call(hass, "set_percentage", percentage=100)
    state = hass.states.get(FAN)
    assert state.attributes["preset_mode"] is None
    assert state.attributes["percentage"] == 100

    await _call(hass, "set_percentage", percentage=0)
    assert hass.states.get(FAN).state == STATE_OFF

    assert [capture.frame.command for capture in sent()] == [
        Command.SPEED_1,
        Command.SPEED_3,
        Command.REVERSE,
        Command.FORWARD,
        Command.BREEZE,
        Command.FAN_OFF,
        Command.SPEED_3,
        Command.BREEZE,
        Command.SPEED_6,
        Command.FAN_OFF,
    ]


async def test_counter_changes_on_every_command(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    sent: Callable[[], list[Capture]],
) -> None:
    """No two commands in a row share a counter, whatever the button."""
    for _ in range(8):
        await _call(hass, "set_percentage", percentage=50)
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.ceiling_fan_timer_1_hour"},
            blocking=True,
        )
    counters = [capture.frame.counter for capture in sent()]
    assert len(counters) == 16
    assert all(a != b for a, b in itertools.pairwise(counters))
    assert 6 not in counters
    # Starts one step into the sequence and wraps around.
    expected = [COUNTER_SEQUENCE[(i + 1) % 7] for i in range(16)]
    assert counters == expected


async def test_increase_and_decrease_speed(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    sent: Callable[[], list[Capture]],
) -> None:
    """Speed steps move one speed at a time."""
    await _call(hass, "set_percentage", percentage=50)
    await _call(hass, "increase_speed")
    await _call(hass, "decrease_speed")
    await _call(hass, "decrease_speed")
    assert [capture.frame.command for capture in sent()] == [
        Command.SPEED_3,
        Command.SPEED_4,
        Command.SPEED_3,
        Command.SPEED_2,
    ]


@pytest.mark.parametrize(
    ("entity_id", "command"),
    [
        ("button.ceiling_fan_all_off", Command.ALL_OFF),
        ("button.ceiling_fan_timer_1_hour", Command.TIMER_1H),
        ("button.ceiling_fan_timer_2_hours", Command.TIMER_2H),
        ("button.ceiling_fan_timer_4_hours", Command.TIMER_4H),
    ],
)
async def test_buttons(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    sent: Callable[[], list[Capture]],
    entity_id: str,
    command: Command,
) -> None:
    """Each button sends its command."""
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id}, blocking=True
    )
    (capture,) = sent()
    assert capture.frame.command is command


async def test_all_off_turns_the_fan_entity_off(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The all-off button also updates the fan's assumed state."""
    await _call(hass, "set_percentage", percentage=100)
    assert hass.states.get(FAN).state == STATE_ON
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.ceiling_fan_all_off"}, blocking=True
    )
    assert hass.states.get(FAN).state == STATE_OFF


async def test_timer_does_not_change_the_state(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """A timer button leaves the fan state alone."""
    await _call(hass, "set_percentage", percentage=100)
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.ceiling_fan_timer_2_hours"},
        blocking=True,
    )
    assert hass.states.get(FAN).attributes["percentage"] == 100


async def test_light_button_when_enabled(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    sent: Callable[[], list[Capture]],
) -> None:
    """The light button works once it is enabled."""
    er.async_get(hass).async_update_entity(
        "button.ceiling_fan_light_toggle", disabled_by=None
    )
    await hass.config_entries.async_reload(setup_entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.ceiling_fan_light_toggle"},
        blocking=True,
    )
    assert sent()[-1].frame.command is Command.LIGHT


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"status": 401}, "refused the token"),
        ({"status": 500, "text": "failed to decode sigbuf"}, "failed to decode sigbuf"),
        ({"exc": aiohttp.ClientError("boom")}, "cannot reach the bridge"),
        ({"exc": TimeoutError()}, "cannot reach the bridge"),
    ],
)
async def test_transmit_failure(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    bridge: AiohttpClientMocker,
    kwargs: dict,
    message: str,
) -> None:
    """A failed transmission raises and leaves the state as it was."""
    bridge.clear_requests()
    bridge.put(f"{BASE}/v2/signal/tx", **kwargs)
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, "set_percentage", percentage=50)
    assert message in str(err.value)
    assert hass.states.get(FAN).state == STATE_OFF


async def test_counter_moves_on_after_a_failure(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    bridge: AiohttpClientMocker,
    sent: Callable[[], list[Capture]],
) -> None:
    """A failed request still uses up its counter value."""
    await _call(hass, "set_percentage", percentage=50)
    bridge.clear_requests()
    bridge.put(f"{BASE}/v2/signal/tx", exc=TimeoutError())
    with pytest.raises(HomeAssistantError):
        await _call(hass, "set_percentage", percentage=50)
    bridge.clear_requests()
    bridge.put(f"{BASE}/v2/signal/tx", status=204)
    await _call(hass, "set_percentage", percentage=50)
    assert sent()[-1].frame.counter == COUNTER_SEQUENCE[3]


async def test_restore_state(
    hass: HomeAssistant, config_entry: MockConfigEntry, bridge: AiohttpClientMocker
) -> None:
    """State, direction and the last speed survive a restart."""
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(FAN, STATE_OFF, {"percentage": 0, "direction": "reverse"}),
                {"last_speed": 5},
            )
        ],
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(FAN)
    assert state.state == STATE_OFF
    assert state.attributes["direction"] == "reverse"

    await _call(hass, "turn_on")
    assert hass.states.get(FAN).attributes["percentage"] == 83


@pytest.mark.parametrize(
    ("attributes", "extra", "percentage", "preset"),
    [
        ({"percentage": 66}, {"last_speed": 4}, 66, None),
        ({"percentage": 0, "preset_mode": "breeze"}, {"last_speed": 2}, 0, "breeze"),
        ({"percentage": 66}, {"last_speed": 99}, 16, None),
        ({"percentage": 66}, {}, 16, None),
    ],
)
async def test_restore_on_state(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    bridge: AiohttpClientMocker,
    attributes: dict,
    extra: dict,
    percentage: int,
    preset: str | None,
) -> None:
    """A fan that was on comes back on; bad stored data falls back to speed 1."""
    mock_restore_cache_with_extra_data(
        hass, [(State(FAN, STATE_ON, attributes), extra)]
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(FAN)
    assert state.state == STATE_ON
    assert state.attributes["percentage"] == percentage
    assert state.attributes["preset_mode"] == preset


async def test_domain_constant() -> None:
    """The domain matches the folder name."""
    assert DOMAIN == "rosler_fan"
