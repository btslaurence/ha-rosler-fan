"""Start a real Home Assistant with the integration and drive it end to end.

Run with the Python of an environment that has Home Assistant installed:

    python scripts/e2e/run_e2e.py

The first run takes a while because Home Assistant installs the requirements
of its own core integrations. Nothing here talks to a real bridge.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import shutil
import sys
import tempfile

# Home Assistant has to be imported before anything pulls in httpx.
import homeassistant  # noqa: F401, I001
from aiohttp import web

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_bond import TOKEN, FakeBond
from tests.common import CONSTANT, REMOTE_ID, press

from custom_components.rosler_fan.protocol import Command, identify
from homeassistant import bootstrap
from homeassistant.const import __version__
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.runner import RuntimeConfig

DOMAIN = "rosler_fan"


def check(condition: bool, label: str) -> None:
    print(("ok   " if condition else "FAIL ") + label)
    if not condition:
        raise SystemExit(1)


async def main() -> None:
    bond = FakeBond()
    runner = web.AppRunner(bond.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    host = f"127.0.0.1:{port}"

    config_dir = Path(tempfile.mkdtemp(prefix="ha-e2e-"))
    shutil.copytree(REPO / "custom_components", config_dir / "custom_components")
    (config_dir / "configuration.yaml").write_text("logger:\n  default: warning\n")

    hass = await bootstrap.async_setup_hass(RuntimeConfig(config_dir=str(config_dir)))
    check(hass is not None, f"Home Assistant {__version__} started")
    check(
        not hass.config.recovery_mode and not hass.config.safe_mode,
        "normal mode, custom integrations allowed",
    )
    await hass.async_start()
    flow = hass.config_entries.flow

    # --- config flow: bridge, learn twice, name ---
    result = await flow.async_init(DOMAIN, context={"source": "user"})
    check(result["step_id"] == "user", "flow opens on the bridge step")
    result = await flow.async_configure(
        result["flow_id"], {"host": host, "access_token": "wrong"}
    )
    check(
        result["errors"] == {"access_token": "invalid_auth"}, "wrong token is refused"
    )
    result = await flow.async_configure(
        result["flow_id"], {"host": host, "access_token": TOKEN}
    )
    check(result["type"].value == "menu", "bridge accepted, method menu shown")
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "learn"})
    check(result["step_id"] == "learn", "learn step shown")
    flow_id = result["flow_id"]

    async def capture(user_input: dict) -> dict:
        result = await flow.async_configure(flow_id, user_input)
        check(result["type"].value == "progress", "scan runs as a progress step")
        for _ in range(100):
            await asyncio.sleep(0.1)
            current = flow.async_progress_by_handler(DOMAIN)
            if current and current[0]["step_id"] != "capture":
                break
        return await flow.async_configure(flow_id)

    bond.scan_queue = [None, press(Command.FAN_OFF, 3), press(Command.FAN_OFF, 4)]
    result = await capture({"frequency_khz": 447885})
    check(
        result["errors"] == {"base": "no_signal"},
        "scan without a press reports no_signal",
    )
    result = await capture({"frequency_khz": 447885})
    check(result["step_id"] == "learn_again", "first press captured")
    result = await capture({})
    check(result["step_id"] == "finish", "second press captured and verified")
    check(
        result["description_placeholders"]["remote_id"] == f"{REMOTE_ID:05X}",
        "remote id read from the signal",
    )
    check(
        bond.scan_requests[-1] == {"freq": 447885, "modulation": "OOK"},
        "scan requested on 447885 kHz OOK",
    )
    result = await flow.async_configure(flow_id, {"name": "Ceiling Fan"})
    check(result["type"].value == "create_entry", "config entry created")
    entry = result["result"]
    await hass.async_block_till_done()
    check(entry.state.value == "loaded", "config entry loaded")
    check(entry.data["checksum_constant"] == CONSTANT, "checksum constant stored")

    # --- device and entities ---
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    check(
        len(devices) == 1 and devices[0].name == "Ceiling Fan", "one device registered"
    )
    entities = er.async_entries_for_device(
        er.async_get(hass), devices[0].id, include_disabled_entities=True
    )
    ids = sorted(entity.entity_id for entity in entities)
    print("     entities:", ", ".join(ids))
    check(
        len(ids) == 6 and "fan.ceiling_fan" in ids, "fan and five buttons on the device"
    )

    # --- commands over real HTTP ---
    async def fan(service: str, **data) -> None:
        await hass.services.async_call(
            "fan", service, {"entity_id": "fan.ceiling_fan", **data}, blocking=True
        )

    await fan("turn_on", percentage=50)
    await fan("set_direction", direction="reverse")
    await fan("set_preset_mode", preset_mode="breeze")
    await fan("turn_off")
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.ceiling_fan_timer_2_hours"},
        blocking=True,
    )
    frames = [identify(signal).frame for signal in bond.transmitted]
    check(
        [frame.command for frame in frames]
        == [
            Command.SPEED_3,
            Command.REVERSE,
            Command.BREEZE,
            Command.FAN_OFF,
            Command.TIMER_2H,
        ],
        "five commands arrived at the bridge as the right buttons",
    )
    check(
        all(frame.remote_id == REMOTE_ID for frame in frames),
        "all frames carry the learned id",
    )
    counters = [frame.counter for frame in frames]
    check(counters == [1, 2, 3, 4, 5], f"counter advances on every command {counters}")
    check(
        all(
            s["freq"] == 447885 and s["reps"] == 7 and s["bps"] == 4000
            for s in bond.transmitted
        ),
        "frequency, repeats and bit rate as verified on the real fan",
    )
    state = hass.states.get("fan.ceiling_fan")
    check(
        state.state == "off" and state.attributes["direction"] == "reverse",
        "assumed state follows",
    )

    # --- options, reconfigure, reload, remove ---
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"frequency_khz": 447875, "repeats": 5}
    )
    await hass.async_block_till_done()
    await fan("turn_on")
    check(
        bond.transmitted[-1]["freq"] == 447875 and bond.transmitted[-1]["reps"] == 5,
        "options take effect after the automatic reload",
    )
    check(
        identify(bond.transmitted[-1]).frame.command is Command.SPEED_3,
        "turn_on returns to the last speed",
    )
    check(
        identify(bond.transmitted[-1]).frame.counter == 7,
        "counter kept across the reload, 6 skipped",
    )

    result = await flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    check(result["step_id"] == "reconfigure", "reconfigure step shown")
    result = await flow.async_configure(result["flow_id"], {"host": f"http://{host}/"})
    await hass.async_block_till_done()
    check(result["reason"] == "reconfigure_successful", "reconfigure keeps the token")

    check(
        await hass.config_entries.async_remove(entry.entry_id) is not None,
        "entry removed",
    )
    await hass.async_block_till_done()
    check(
        not dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id),
        "device removed with the entry",
    )

    await hass.async_stop(force=True)
    await runner.cleanup()
    shutil.rmtree(config_dir, ignore_errors=True)
    print("ALL OK on Home Assistant", __version__)


logging.basicConfig(level=logging.ERROR)
asyncio.run(main())
