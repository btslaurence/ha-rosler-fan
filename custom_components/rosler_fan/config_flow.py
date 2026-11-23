"""Config flow for the Rosler ceiling fan integration.

Setup connects to a Bond Bridge and then needs two things from the remote:
its 20-bit id and the constant in its checksum. The flow reads both off the
air by scanning two button presses, and refuses remotes whose frames do not
follow the protocol this integration knows.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_HOST, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import RoslerConfigEntry
from .api import (
    BondAuthError,
    BondBridge,
    BondConnectionError,
    BondError,
    BondScanTimeoutError,
    BondTokenLockedError,
    normalize_host,
)
from .const import (
    CONF_BOND_ID,
    CONF_CHECKSUM_CONSTANT,
    CONF_FREQUENCY,
    CONF_REMOTE_ID,
    CONF_REPEATS,
    DEFAULT_CHECKSUM_CONSTANT,
    DEFAULT_FREQUENCY_KHZ,
    DEFAULT_REPEATS,
    DOMAIN,
    MAX_FREQUENCY_KHZ,
    MAX_REPEATS,
    MIN_FREQUENCY_KHZ,
    MIN_REPEATS,
)
from .protocol import (
    Capture,
    NoFrameError,
    ProtocolError,
    format_remote_id,
    identify,
    parse_remote_id,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "Ceiling Fan"

_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_FREQUENCY = NumberSelector(
    NumberSelectorConfig(
        min=MIN_FREQUENCY_KHZ,
        max=MAX_FREQUENCY_KHZ,
        step=1,
        mode=NumberSelectorMode.BOX,
        unit_of_measurement="kHz",
    )
)
_REPEATS = NumberSelector(
    NumberSelectorConfig(
        min=MIN_REPEATS, max=MAX_REPEATS, step=1, mode=NumberSelectorMode.BOX
    )
)
_CONSTANT = NumberSelector(
    NumberSelectorConfig(min=0, max=15, step=1, mode=NumberSelectorMode.BOX)
)


def _bridge_schema(host: str = "") -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, description={"suggested_value": host}): str,
            vol.Optional(CONF_ACCESS_TOKEN): _PASSWORD,
        }
    )


def _learn_schema(frequency_khz: int) -> vol.Schema:
    return vol.Schema({vol.Required(CONF_FREQUENCY, default=frequency_khz): _FREQUENCY})


class RoslerFanConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for a Rosler ceiling fan."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._bridge: BondBridge | None = None
        self._bond_id: str | None = None
        self._frequency_khz = DEFAULT_FREQUENCY_KHZ
        self._captures: list[Capture] = []
        self._capture_task: asyncio.Task[Capture] | None = None
        self._capture_error: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: RoslerConfigEntry,
    ) -> RoslerFanOptionsFlow:
        """Return the options flow."""
        return RoslerFanOptionsFlow()

    async def _async_connect(
        self, user_input: dict[str, Any], errors: dict[str, str]
    ) -> BondBridge | None:
        """Check the bridge and its token. Fill errors and return None on failure."""
        bridge = BondBridge(
            async_get_clientsession(self.hass),
            user_input[CONF_HOST],
            (user_input.get(CONF_ACCESS_TOKEN) or "").strip() or None,
        )
        try:
            version = await bridge.version()
            if not bridge.token:
                bridge.token = await bridge.fetch_token()
            await bridge.check_token()
        except BondTokenLockedError:
            errors[CONF_ACCESS_TOKEN] = "token_locked"
        except BondAuthError:
            errors[CONF_ACCESS_TOKEN] = "invalid_auth"
        except BondConnectionError:
            errors[CONF_HOST] = "cannot_connect"
        except BondError:
            errors[CONF_HOST] = "not_bond"
        else:
            self._bond_id = str(version.get("bondid") or "") or None
            return bridge
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the Bond Bridge."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not (user_input.get(CONF_ACCESS_TOKEN) or "").strip():
                # Another fan on the same bridge already has the token.
                host = normalize_host(user_input[CONF_HOST])
                for entry in self._async_current_entries(include_ignore=False):
                    if normalize_host(entry.data[CONF_HOST]) == host:
                        user_input = {
                            **user_input,
                            CONF_ACCESS_TOKEN: entry.data[CONF_ACCESS_TOKEN],
                        }
                        break
            if (bridge := await self._async_connect(user_input, errors)) is not None:
                self._bridge = bridge
                return await self.async_step_method()

        # Offer the bridge of a fan that is already set up.
        host = (user_input or {}).get(CONF_HOST) or next(
            (
                entry.data[CONF_HOST]
                for entry in self._async_current_entries(include_ignore=False)
            ),
            "",
        )
        return self.async_show_form(
            step_id="user", data_schema=_bridge_schema(host), errors=errors
        )

    async def async_step_method(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between learning the remote and typing its values."""
        return self.async_show_menu(step_id="method", menu_options=["learn", "manual"])

    # --- Learning from the remote -------------------------------------------

    async def async_step_learn(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explain the first press and start scanning when submitted."""
        if user_input is not None:
            self._frequency_khz = int(user_input[CONF_FREQUENCY])
            self._captures = []
            return await self.async_step_capture()

        errors = {"base": self._capture_error} if self._capture_error else {}
        self._capture_error = None
        return self.async_show_form(
            step_id="learn",
            data_schema=_learn_schema(self._frequency_khz),
            errors=errors,
        )

    async def async_step_learn_again(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explain the second press and start scanning when submitted."""
        if user_input is not None:
            return await self.async_step_capture()

        errors = {"base": self._capture_error} if self._capture_error else {}
        self._capture_error = None
        return self.async_show_form(
            step_id="learn_again", data_schema=vol.Schema({}), errors=errors
        )

    async def _async_capture(self) -> Capture:
        """Scan once and return the frame that was received."""
        assert self._bridge is not None
        signal = await self._bridge.capture(self._frequency_khz)
        return identify(signal)

    async def async_step_capture(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait for a button press while the bridge scans."""
        if self._capture_task is None:
            self._capture_task = self.hass.async_create_task(self._async_capture())

        if not self._capture_task.done():
            return self.async_show_progress(
                step_id="capture",
                progress_action="capture",
                progress_task=self._capture_task,
            )

        try:
            capture = await self._capture_task
        except BondScanTimeoutError:
            self._capture_error = "no_signal"
        except BondAuthError:
            self._capture_error = "invalid_auth"
        except BondConnectionError:
            self._capture_error = "cannot_connect"
        except NoFrameError as err:
            _LOGGER.debug("Capture not usable: %s", err)
            self._capture_error = "no_frame"
        except (BondError, ProtocolError) as err:
            _LOGGER.warning("Capture failed: %s", err)
            self._capture_error = "capture_failed"
        else:
            self._captures.append(capture)
        finally:
            self._capture_task = None

        return self.async_show_progress_done(next_step_id="captured")

    async def async_step_captured(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Decide what follows a capture."""
        if self._capture_error:
            if self._captures:
                return await self.async_step_learn_again()
            return await self.async_step_learn()

        if len(self._captures) < 2:
            return await self.async_step_learn_again()

        first, second = (capture.frame for capture in self._captures[:2])

        if first.remote_id != second.remote_id:
            # Two different remotes, most likely one of them a neighbour's.
            return await self._async_restart_learning("id_mismatch")
        if first.counter == second.counter:
            return await self._async_restart_learning("same_counter")
        if first.constant != second.constant:
            return self._unsupported("checksum_mismatch")
        if first.command is None or second.command is None:
            return self._unsupported("unknown_button")

        return await self.async_step_finish()

    async def _async_restart_learning(self, error: str) -> ConfigFlowResult:
        """Throw both captures away and ask for the first press again."""
        self._captures = []
        self._capture_error = error
        return await self.async_step_learn()

    def _unsupported(self, reason: str) -> ConfigFlowResult:
        """Stop: this remote does not follow the protocol this integration knows.

        The frame fields are shown and logged so that they can be reported.
        The remote id is left out because it is all it takes to control the fan.
        """
        first, second = (capture.frame for capture in self._captures[:2])
        details = {
            "frame_1": (
                f"button {first.button:X}, flag {first.flag}, "
                f"counter {first.counter}, checksum {first.checksum:X}"
            ),
            "frame_2": (
                f"button {second.button:X}, flag {second.flag}, "
                f"counter {second.counter}, checksum {second.checksum:X}"
            ),
        }
        _LOGGER.warning("Remote not supported (%s): %s", reason, details)
        return self.async_abort(reason=reason, description_placeholders=details)

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name the fan and create the entry."""
        frame = self._captures[0].frame
        if user_input is not None:
            return await self._async_create(
                user_input[CONF_NAME], frame.remote_id, frame.constant
            )
        return self.async_show_form(
            step_id="finish",
            data_schema=vol.Schema(
                {vol.Required(CONF_NAME, default=DEFAULT_NAME): str}
            ),
            description_placeholders={
                "remote_id": format_remote_id(frame.remote_id),
                "constant": str(frame.constant),
            },
        )

    # --- Typing the values ---------------------------------------------------

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take the remote id and checksum constant as typed."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                remote_id = parse_remote_id(user_input[CONF_REMOTE_ID])
            except ValueError:
                errors[CONF_REMOTE_ID] = "invalid_remote_id"
            else:
                self._frequency_khz = int(user_input[CONF_FREQUENCY])
                return await self._async_create(
                    user_input[CONF_NAME],
                    remote_id,
                    int(user_input[CONF_CHECKSUM_CONSTANT]),
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_REMOTE_ID): str,
                vol.Required(
                    CONF_CHECKSUM_CONSTANT, default=DEFAULT_CHECKSUM_CONSTANT
                ): _CONSTANT,
                vol.Required(CONF_FREQUENCY, default=self._frequency_khz): _FREQUENCY,
            }
        )
        return self.async_show_form(
            step_id="manual",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def _async_create(
        self, name: str, remote_id: int, constant: int
    ) -> ConfigFlowResult:
        assert self._bridge is not None
        await self.async_set_unique_id(format_remote_id(remote_id))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=name.strip() or DEFAULT_NAME,
            data={
                CONF_HOST: self._bridge.host,
                CONF_ACCESS_TOKEN: self._bridge.token,
                CONF_BOND_ID: self._bond_id,
                CONF_REMOTE_ID: remote_id,
                CONF_CHECKSUM_CONSTANT: constant,
            },
            options={
                CONF_FREQUENCY: self._frequency_khz,
                CONF_REPEATS: DEFAULT_REPEATS,
            },
        )

    # --- Changing the bridge address or token --------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Point an existing fan at a new bridge address or token."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            if not (user_input.get(CONF_ACCESS_TOKEN) or "").strip():
                user_input = {
                    **user_input,
                    CONF_ACCESS_TOKEN: entry.data[CONF_ACCESS_TOKEN],
                }
            if (bridge := await self._async_connect(user_input, errors)) is not None:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_HOST: bridge.host,
                        CONF_ACCESS_TOKEN: bridge.token,
                        CONF_BOND_ID: self._bond_id,
                    },
                )

        host = (user_input or {}).get(CONF_HOST) or entry.data[CONF_HOST]
        return self.async_show_form(
            step_id="reconfigure", data_schema=_bridge_schema(host), errors=errors
        )


class RoslerFanOptionsFlow(OptionsFlowWithReload):
    """Transmission settings."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the frequency and the number of repeats."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_FREQUENCY: int(user_input[CONF_FREQUENCY]),
                    CONF_REPEATS: int(user_input[CONF_REPEATS]),
                }
            )

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_FREQUENCY,
                    default=options.get(CONF_FREQUENCY, DEFAULT_FREQUENCY_KHZ),
                ): _FREQUENCY,
                vol.Required(
                    CONF_REPEATS, default=options.get(CONF_REPEATS, DEFAULT_REPEATS)
                ): _REPEATS,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
