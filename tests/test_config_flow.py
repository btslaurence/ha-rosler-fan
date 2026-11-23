"""Tests for the config flow."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.rosler_fan.const import (
    CONF_BOND_ID,
    CONF_CHECKSUM_CONSTANT,
    CONF_FREQUENCY,
    CONF_REMOTE_ID,
    CONF_REPEATS,
    DOMAIN,
)
from custom_components.rosler_fan.protocol import Command, frame_bits, identify

from .common import CONSTANT, REMOTE_ID, press, render_scan
from .conftest import BASE, HOST, TOKEN, VERSION

SCAN = f"{BASE}/v2/signal/scan"


def _mock_bridge(mock: AiohttpClientMocker, signal: dict[str, Any] | None) -> None:
    """Mock a bridge whose next scan returns signal, or times out if None."""
    mock.clear_requests()
    mock.get(f"{BASE}/v2/sys/version", json=VERSION)
    mock.put(SCAN, status=201)
    mock.delete(SCAN, status=204)
    mock.put(f"{BASE}/v2/signal/tx", status=204)
    if signal is None:
        mock.get(SCAN, json={"running": False, "success": False})
        mock.get(f"{SCAN}/signal", status=204)
    else:
        mock.get(SCAN, json={"running": False, "success": True})
        mock.get(f"{SCAN}/signal", json=signal)


async def _to_learn(hass: HomeAssistant, mock: AiohttpClientMocker) -> str:
    """Walk the flow up to the first learning step and return its id."""
    _mock_bridge(mock, None)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST, CONF_ACCESS_TOKEN: TOKEN}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["learn", "manual"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "learn"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "learn"
    return result["flow_id"]


async def _capture(
    hass: HomeAssistant,
    mock: AiohttpClientMocker,
    flow_id: str,
    signal: dict[str, Any] | None,
    user_input: dict[str, Any],
) -> dict[str, Any]:
    """Submit a learning step, let the scan finish, and return what follows."""
    _mock_bridge(mock, signal)
    result = await hass.config_entries.flow.async_configure(flow_id, user_input)
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "capture"
    await hass.async_block_till_done()
    return await hass.config_entries.flow.async_configure(flow_id)


async def _learn(
    hass: HomeAssistant,
    mock: AiohttpClientMocker,
    first: dict[str, Any] | None,
    second: dict[str, Any] | None,
) -> dict[str, Any]:
    """Run both captures and return the step after the second."""
    flow_id = await _to_learn(hass, mock)
    result = await _capture(hass, mock, flow_id, first, {CONF_FREQUENCY: 447885})
    assert result["step_id"] == "learn_again", result
    assert not result["errors"]
    return await _capture(hass, mock, flow_id, second, {})


async def test_learn_flow(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Two presses of the remote are enough to set up a fan."""
    result = await _learn(
        hass,
        aioclient_mock,
        press(Command.FAN_OFF, 3),
        press(Command.FAN_OFF, 4),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "finish"
    assert result["description_placeholders"] == {
        "remote_id": f"{REMOTE_ID:05X}",
        "constant": str(CONSTANT),
    }

    # Both scans were asked for on the chosen frequency.
    scans = [
        data
        for method, url, data, _ in aioclient_mock.mock_calls
        if method == "PUT" and url.path == "/v2/signal/scan"
    ]
    assert scans == [{"freq": 447885, "modulation": "OOK"}]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Living room fan"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living room fan"
    assert result["data"] == {
        CONF_HOST: HOST,
        CONF_ACCESS_TOKEN: TOKEN,
        CONF_BOND_ID: VERSION["bondid"],
        CONF_REMOTE_ID: REMOTE_ID,
        CONF_CHECKSUM_CONSTANT: CONSTANT,
    }
    assert result["options"] == {CONF_FREQUENCY: 447885, CONF_REPEATS: 7}
    assert result["result"].unique_id == f"{REMOTE_ID:05X}"

    # The new fan transmits with what was learned.
    assert hass.states.get("fan.living_room_fan") is not None
    await hass.services.async_call(
        "fan", "turn_on", {"entity_id": "fan.living_room_fan"}, blocking=True
    )
    sent = identify(aioclient_mock.mock_calls[-1][2])
    assert sent.frame.remote_id == REMOTE_ID
    assert sent.frame.command is Command.SPEED_1


async def test_learn_with_other_constant_and_buttons(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Different buttons are fine, and the remote's own constant is kept."""
    result = await _learn(
        hass,
        aioclient_mock,
        press(Command.SPEED_2, 7, constant=0x5),
        press(Command.TIMER_4H, 0, constant=0x5),
    )
    assert result["step_id"] == "finish"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Fan"}
    )
    assert result["data"][CONF_CHECKSUM_CONSTANT] == 0x5


@pytest.mark.parametrize(
    ("signal", "error"),
    [
        (None, "no_signal"),
        (render_scan([]), "no_frame"),
        (press(Command.FAN_OFF, 3, repeats=2), "no_frame"),
        ({"encoding": "mystery", "bps": 40000, "data": "00"}, "capture_failed"),
    ],
)
async def test_first_capture_fails_then_succeeds(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    signal: dict[str, Any] | None,
    error: str,
) -> None:
    """A failed scan shows why and can be repeated."""
    flow_id = await _to_learn(hass, aioclient_mock)
    result = await _capture(
        hass, aioclient_mock, flow_id, signal, {CONF_FREQUENCY: 447885}
    )
    assert result["step_id"] == "learn"
    assert result["errors"] == {"base": error}

    result = await _capture(
        hass,
        aioclient_mock,
        flow_id,
        press(Command.FAN_OFF, 1),
        {CONF_FREQUENCY: 447900},
    )
    assert result["step_id"] == "learn_again"
    assert not result["errors"]


async def test_second_capture_fails_then_succeeds(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A failed second scan keeps the first capture."""
    result = await _learn(hass, aioclient_mock, press(Command.FAN_OFF, 1), None)
    assert result["step_id"] == "learn_again"
    assert result["errors"] == {"base": "no_signal"}
    result = await _capture(
        hass, aioclient_mock, result["flow_id"], press(Command.FAN_OFF, 2), {}
    )
    assert result["step_id"] == "finish"


@pytest.mark.parametrize(
    ("second", "error"),
    [
        (press(Command.FAN_OFF, 4, remote_id=0x12345), "id_mismatch"),
        (press(Command.FAN_OFF, 3), "same_counter"),
    ],
)
async def test_captures_that_do_not_belong_together(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    second: dict[str, Any],
    error: str,
) -> None:
    """Learning starts over when the two presses cannot be compared."""
    result = await _learn(hass, aioclient_mock, press(Command.FAN_OFF, 3), second)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "learn"
    assert result["errors"] == {"base": error}

    # Starting over needs two new presses.
    flow_id = result["flow_id"]
    result = await _capture(
        hass,
        aioclient_mock,
        flow_id,
        press(Command.FAN_OFF, 5),
        {CONF_FREQUENCY: 447885},
    )
    assert result["step_id"] == "learn_again"
    result = await _capture(
        hass, aioclient_mock, flow_id, press(Command.FAN_OFF, 6), {}
    )
    assert result["step_id"] == "finish"


async def test_unsupported_checksum(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A remote whose checksum is not button^counter^constant is refused."""
    result = await _learn(
        hass,
        aioclient_mock,
        press(Command.FAN_OFF, 3, constant=0x8),
        press(Command.FAN_OFF, 4, constant=0x9),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "checksum_mismatch"
    details = result["description_placeholders"]
    assert details["frame_1"] == "button B, flag 0, counter 3, checksum 0"
    assert details["frame_2"] == "button B, flag 0, counter 4, checksum 6"
    # Nothing that identifies the remote is shown.
    assert f"{REMOTE_ID:05X}" not in str(details)


async def test_unknown_button(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A button code outside the known table means a different remote."""

    def unknown(counter: int) -> dict[str, Any]:
        # Button F with flag 0 is not on the remote.
        check = 0xF ^ counter ^ CONSTANT
        bits = f"{REMOTE_ID:020b}1111{counter:04b}{check:04b}0"
        return render_scan([bits] * 7)

    result = await _learn(hass, aioclient_mock, unknown(1), unknown(2))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown_button"
    assert "button F" in result["description_placeholders"]["frame_1"]


@pytest.mark.parametrize(
    ("mock_kwargs", "url", "errors"),
    [
        (
            {"exc": aiohttp.ClientError()},
            "/v2/sys/version",
            {CONF_HOST: "cannot_connect"},
        ),
        ({"exc": TimeoutError()}, "/v2/sys/version", {CONF_HOST: "cannot_connect"}),
        ({"json": {"hello": "world"}}, "/v2/sys/version", {CONF_HOST: "not_bond"}),
        ({"status": 404}, "/v2/sys/version", {CONF_HOST: "not_bond"}),
        ({"status": 401}, "/v2/signal/scan", {CONF_ACCESS_TOKEN: "invalid_auth"}),
    ],
)
async def test_bridge_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_kwargs: dict[str, Any],
    url: str,
    errors: dict[str, str],
) -> None:
    """Problems with the bridge are shown on the field they concern."""
    if url != "/v2/sys/version":
        aioclient_mock.get(f"{BASE}/v2/sys/version", json=VERSION)
    aioclient_mock.get(f"{BASE}{url}", **mock_kwargs)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST, CONF_ACCESS_TOKEN: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors

    # Correcting the input lets the flow go on.
    _mock_bridge(aioclient_mock, None)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: f"http://{HOST}/", CONF_ACCESS_TOKEN: TOKEN}
    )
    assert result["type"] is FlowResultType.MENU


async def test_token_is_fetched_when_left_empty(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An unlocked bridge hands out its token."""
    _mock_bridge(aioclient_mock, None)
    aioclient_mock.get(f"{BASE}/v2/token", json={"token": "fetched", "locked": 0})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST}
    )
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "manual"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Fan",
            CONF_REMOTE_ID: f"{REMOTE_ID:05x}",
            CONF_CHECKSUM_CONSTANT: 8,
            CONF_FREQUENCY: 447885,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ACCESS_TOKEN] == "fetched"


async def test_locked_bridge_needs_a_token(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A locked bridge does not reveal its token."""
    _mock_bridge(aioclient_mock, None)
    aioclient_mock.get(f"{BASE}/v2/token", json={"locked": 1, "pin_attempts_left": 5})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ACCESS_TOKEN: "token_locked"}


async def test_manual_flow(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The remote values can be typed in, as hex or as binary."""
    flow_id = await _to_learn(hass, aioclient_mock)
    hass.config_entries.flow.async_abort(flow_id)

    _mock_bridge(aioclient_mock, None)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST, CONF_ACCESS_TOKEN: TOKEN}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "manual"}
    )
    assert result["step_id"] == "manual"

    user_input = {
        CONF_NAME: "Bedroom fan",
        CONF_REMOTE_ID: "not hex",
        CONF_CHECKSUM_CONSTANT: 8,
        CONF_FREQUENCY: 447900,
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["errors"] == {CONF_REMOTE_ID: "invalid_remote_id"}

    user_input[CONF_REMOTE_ID] = f"{REMOTE_ID:020b}"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_REMOTE_ID] == REMOTE_ID
    assert result["options"][CONF_FREQUENCY] == 447900


async def test_same_remote_twice(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """A remote can only be set up once."""
    result = await _learn(
        hass, aioclient_mock, press(Command.FAN_OFF, 3), press(Command.FAN_OFF, 4)
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Again"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_second_fan_is_offered_the_known_bridge(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Adding another fan suggests the bridge address that is already in use."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    host_key = next(key for key in result["data_schema"].schema if key == CONF_HOST)
    assert host_key.description == {"suggested_value": HOST}


async def test_second_fan_reuses_the_token(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """A second fan on a known bridge does not need the token again."""
    _mock_bridge(aioclient_mock, None)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: f"http://{HOST}"}
    )
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "manual"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Second fan",
            CONF_REMOTE_ID: "0BEEF",
            CONF_CHECKSUM_CONSTANT: 8,
            CONF_FREQUENCY: 447885,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ACCESS_TOKEN] == TOKEN
    assert result["data"][CONF_REMOTE_ID] == 0x0BEEF


async def test_reconfigure(
    hass: HomeAssistant, setup_entry: MockConfigEntry, bridge: AiohttpClientMocker
) -> None:
    """The bridge address can be changed, keeping the token."""
    new_base = "http://192.0.2.99"
    bridge.get(f"{new_base}/v2/sys/version", json={**VERSION, "bondid": "ZZNEW"})
    bridge.get(f"{new_base}/v2/signal/scan", json={"running": False, "success": False})
    bridge.put(f"{new_base}/v2/signal/tx", status=204)

    result = await setup_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.99"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert setup_entry.data[CONF_HOST] == "192.0.2.99"
    assert setup_entry.data[CONF_ACCESS_TOKEN] == TOKEN
    assert setup_entry.data[CONF_BOND_ID] == "ZZNEW"
    assert setup_entry.data[CONF_REMOTE_ID] == REMOTE_ID

    await hass.services.async_call(
        "fan", "turn_on", {"entity_id": "fan.ceiling_fan"}, blocking=True
    )
    assert bridge.mock_calls[-1][1].host == "192.0.2.99"


async def test_reconfigure_with_bad_token(
    hass: HomeAssistant, setup_entry: MockConfigEntry, bridge: AiohttpClientMocker
) -> None:
    """A wrong new token is refused and the entry keeps the old one."""
    bridge.clear_requests()
    bridge.get(f"{BASE}/v2/sys/version", json=VERSION)
    bridge.get(SCAN, status=401)
    result = await setup_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST, CONF_ACCESS_TOKEN: "wrong"}
    )
    assert result["errors"] == {CONF_ACCESS_TOKEN: "invalid_auth"}
    assert setup_entry.data[CONF_ACCESS_TOKEN] == TOKEN


async def test_options(
    hass: HomeAssistant, setup_entry: MockConfigEntry, bridge: AiohttpClientMocker
) -> None:
    """Frequency and repeats can be changed and take effect at once."""
    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FREQUENCY: 447875, CONF_REPEATS: 5}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_entry.options == {CONF_FREQUENCY: 447875, CONF_REPEATS: 5}

    await hass.services.async_call(
        "fan", "turn_on", {"entity_id": "fan.ceiling_fan"}, blocking=True
    )
    payload = bridge.mock_calls[-1][2]
    assert payload["freq"] == 447875
    assert payload["reps"] == 5


async def test_closing_the_flow_cancels_the_scan(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Closing the dialog while the bridge is listening stops the scan."""
    flow_id = await _to_learn(hass, aioclient_mock)
    aioclient_mock.clear_requests()
    aioclient_mock.put(SCAN, status=201)
    aioclient_mock.delete(SCAN, status=204)
    aioclient_mock.get(SCAN, json={"running": True, "success": False})
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_FREQUENCY: 447885}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS

    hass.config_entries.flow.async_abort(flow_id)

    def scan_cancelled() -> bool:
        return any(
            method == "DELETE" and url.path == "/v2/signal/scan"
            for method, url, _, _ in aioclient_mock.mock_calls
        )

    # The cancelled task tidies up on its own; give it a moment.
    async with asyncio.timeout(1):
        while not scan_cancelled():
            await asyncio.sleep(0)
    assert scan_cancelled()


def test_frame_helper_matches_protocol() -> None:
    """The hand-built frame in test_unknown_button follows the frame layout."""
    counter = 2
    check = 0xB ^ counter ^ CONSTANT
    bits = f"{REMOTE_ID:020b}1011{counter:04b}{check:04b}0"
    assert bits == frame_bits(REMOTE_ID, Command.FAN_OFF, counter, CONSTANT)
