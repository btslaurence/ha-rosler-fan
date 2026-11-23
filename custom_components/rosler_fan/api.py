"""Minimal client for the Bond Bridge local API.

Only the raw signal endpoints are used: this integration treats the bridge as
a transmitter and receiver, not as a device registry.
https://docs-local.appbond.com
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Mapping
from typing import Any

import aiohttp

REQUEST_TIMEOUT = 10
SCAN_POLL_INTERVAL = 0.5
# The bridge gives up on a scan by itself after about 30 s.
SCAN_TIMEOUT = 40


class BondError(Exception):
    """The bridge answered with an error."""


class BondConnectionError(BondError):
    """The bridge could not be reached."""


class BondAuthError(BondError):
    """The bridge refused the token."""


class BondTokenLockedError(BondError):
    """The bridge will not hand out its token right now."""


class BondScanTimeoutError(BondError):
    """A scan ended without capturing a signal."""


def normalize_host(host: str) -> str:
    """Strip a scheme, a path and blanks from what the user typed."""
    host = host.strip()
    for prefix in ("http://", "https://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix) :]
    return host.split("/", 1)[0]


class BondBridge:
    """A Bond Bridge reached over its local HTTP API."""

    def __init__(
        self, session: aiohttp.ClientSession, host: str, token: str | None = None
    ) -> None:
        """Initialize the client."""
        self._session = session
        self.host = normalize_host(host)
        self.token = token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        auth: bool = True,
    ) -> Any:
        """Send a request and return the decoded body, or None if it is empty."""
        headers = {"BOND-Token": self.token} if auth and self.token else {}
        try:
            async with self._session.request(
                method,
                f"http://{self.host}{path}",
                json=json,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as response:
                if response.status == 401:
                    raise BondAuthError("the bridge refused the token")
                if response.status >= 400:
                    raise BondError(
                        f"{method} {path} failed with HTTP {response.status}: "
                        f"{(await response.text())[:200]}"
                    )
                if response.status == 204:
                    return None
                try:
                    return await response.json(content_type=None)
                except ValueError:
                    return None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BondConnectionError(
                f"cannot reach the bridge at {self.host}: {err!r}"
            ) from err

    async def version(self) -> dict[str, Any]:
        """Return the bridge's version information. Needs no token."""
        data = await self._request("GET", "/v2/sys/version", auth=False)
        if not isinstance(data, dict) or "fw_ver" not in data:
            raise BondError("the device did not answer like a Bond Bridge")
        return data

    async def fetch_token(self) -> str:
        """Return the bridge's token, which it only reveals while unlocked.

        The bridge unlocks for a few minutes after a power cycle.
        """
        data = await self._request("GET", "/v2/token", auth=False)
        if isinstance(data, dict) and data.get("token"):
            return str(data["token"])
        raise BondTokenLockedError("the bridge is locked")

    async def check_token(self) -> None:
        """Raise BondAuthError unless the bridge accepts the token.

        Reading the scan state is used because, unlike the device list, the
        bridge does protect it, and it changes nothing.
        """
        if not self.token:
            raise BondAuthError("no token")
        await self._request("GET", "/v2/signal/scan")

    async def transmit(self, signal: Mapping[str, Any]) -> None:
        """Transmit a signal. Returns once the bridge has started sending."""
        await self._request("PUT", "/v2/signal/tx", json=signal)

    async def cancel_scan(self) -> None:
        """Stop a scan, ignoring any error."""
        with contextlib.suppress(BondError):
            await self._request("DELETE", "/v2/signal/scan")

    async def capture(self, frequency_khz: int) -> dict[str, Any]:
        """Scan one frequency and return the recorded signal.

        Raises BondScanTimeoutError if nothing was received before the bridge
        gave up.
        """
        deadline = time.monotonic() + SCAN_TIMEOUT
        finished = False
        try:
            await self._request(
                "PUT",
                "/v2/signal/scan",
                json={"freq": frequency_khz, "modulation": "OOK"},
            )
            while True:
                await asyncio.sleep(SCAN_POLL_INTERVAL)
                status = await self._request("GET", "/v2/signal/scan")
                if not isinstance(status, dict):
                    raise BondError("unexpected scan status")
                if not status.get("running"):
                    break
                if time.monotonic() > deadline:
                    raise BondScanTimeoutError("the scan did not finish")
            finished = True
        finally:
            if not finished:
                # Also runs when the flow is closed while a scan is pending.
                await asyncio.shield(self.cancel_scan())
        if not status.get("success"):
            raise BondScanTimeoutError("no signal was received")
        signal = await self._request("GET", "/v2/signal/scan/signal")
        if not isinstance(signal, dict) or "data" not in signal:
            raise BondScanTimeoutError("the scan returned no signal")
        return signal
