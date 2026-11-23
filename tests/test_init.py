"""Tests for setting up and removing a config entry."""

from __future__ import annotations

from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.rosler_fan.controller import storage_key
from custom_components.rosler_fan.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.rosler_fan.protocol import COUNTER_SEQUENCE, identify

from .common import REMOTE_ID
from .conftest import BASE, HOST, TOKEN


async def test_setup_and_unload(
    hass: HomeAssistant, setup_entry: MockConfigEntry, bridge: AiohttpClientMocker
) -> None:
    """The entry loads after checking the token, and unloads."""
    assert setup_entry.state is ConfigEntryState.LOADED
    method, url, _, headers = bridge.mock_calls[0]
    assert (method, url.path) == ("GET", "/v2/signal/scan")
    assert headers["BOND-Token"] == TOKEN

    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert setup_entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_retries_when_bridge_is_offline(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An unreachable bridge is retried later."""
    aioclient_mock.get(f"{BASE}/v2/signal/scan", exc=aiohttp.ClientError())
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_fails_on_bad_token(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A refused token is a setup error, not a retry."""
    aioclient_mock.get(f"{BASE}/v2/signal/scan", status=401)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR


async def test_counter_survives_restart(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    bridge: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """The counter position is stored and picked up again."""
    hass_storage[storage_key(config_entry.entry_id)] = {
        "version": 1,
        "minor_version": 1,
        "key": storage_key(config_entry.entry_id),
        "data": {"counter_index": 4},
    }
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        "fan", "turn_on", {"entity_id": "fan.ceiling_fan"}, blocking=True
    )
    sent = identify(bridge.mock_calls[-1][2])
    assert sent.frame.counter == COUNTER_SEQUENCE[5]

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage[storage_key(config_entry.entry_id)]["data"] == {
        "counter_index": 5
    }


async def test_remove_deletes_the_counter(
    hass: HomeAssistant, setup_entry: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    """Removing the entry removes its stored counter."""
    await hass.services.async_call(
        "fan", "turn_on", {"entity_id": "fan.ceiling_fan"}, blocking=True
    )
    assert await hass.config_entries.async_remove(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert storage_key(setup_entry.entry_id) not in hass_storage


async def test_diagnostics_hide_secrets(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Neither the token, the address nor the remote id leaves in diagnostics."""
    diagnostics = await async_get_config_entry_diagnostics(hass, setup_entry)
    text = str(diagnostics)
    assert TOKEN not in text
    assert HOST not in text
    assert str(REMOTE_ID) not in text
    assert f"{REMOTE_ID:05X}" not in text
    assert diagnostics["entry"]["data"]["remote_id"] == "**REDACTED**"
    assert diagnostics["counter_index"] == 0
