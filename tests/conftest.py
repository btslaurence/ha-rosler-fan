"""Fixtures for the Rosler fan tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_HOST
from homeassistant.core import HomeAssistant
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
from custom_components.rosler_fan.protocol import Capture, identify

from .common import CONSTANT, REMOTE_ID

HOST = "192.0.2.10"
TOKEN = "test-token"
BASE = f"http://{HOST}"
VERSION = {"target": "zermatt-s3", "fw_ver": "v4.40.1", "bondid": "ZZTEST00001"}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load the integration under test."""


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not wait for scans or transmissions that never happen."""
    monkeypatch.setattr("custom_components.rosler_fan.api.SCAN_POLL_INTERVAL", 0)
    monkeypatch.setattr("custom_components.rosler_fan.controller._TX_MARGIN", 0)
    monkeypatch.setattr(
        "custom_components.rosler_fan.controller.tx_duration", lambda repeats: 0
    )


@pytest.fixture
def bridge(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """A Bond Bridge that accepts the token and every transmission."""
    aioclient_mock.get(f"{BASE}/v2/sys/version", json=VERSION)
    aioclient_mock.get(
        f"{BASE}/v2/signal/scan", json={"running": False, "success": False}
    )
    aioclient_mock.put(f"{BASE}/v2/signal/tx", status=204)
    return aioclient_mock


@pytest.fixture
def sent(bridge: AiohttpClientMocker) -> Callable[[], list[Capture]]:
    """Return the frames transmitted so far, decoded from what was sent."""

    def _sent() -> list[Capture]:
        return [
            identify(data)
            for method, url, data, _ in bridge.mock_calls
            if method == "PUT" and url.path == "/v2/signal/tx"
        ]

    return _sent


@pytest.fixture
def entry_data() -> dict[str, Any]:
    """Config entry data of a fan."""
    return {
        CONF_HOST: HOST,
        CONF_ACCESS_TOKEN: TOKEN,
        CONF_BOND_ID: VERSION["bondid"],
        CONF_REMOTE_ID: REMOTE_ID,
        CONF_CHECKSUM_CONSTANT: CONSTANT,
    }


@pytest.fixture
def config_entry(hass: HomeAssistant, entry_data: dict[str, Any]) -> MockConfigEntry:
    """A config entry that is not set up yet."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ceiling Fan",
        unique_id=f"{REMOTE_ID:05X}",
        data=entry_data,
        options={CONF_FREQUENCY: 447885, CONF_REPEATS: 7},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def setup_entry(
    hass: HomeAssistant, config_entry: MockConfigEntry, bridge: AiohttpClientMocker
) -> MockConfigEntry:
    """A config entry that is set up."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
