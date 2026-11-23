"""A stand-in for a Bond Bridge, for end-to-end tests on a real Home Assistant."""

from __future__ import annotations

from aiohttp import web

TOKEN = "e2e-token"


class FakeBond:
    def __init__(self) -> None:
        self.transmitted: list[dict] = []
        self.scan_queue: list[dict | None] = []  # None means: time out
        self.scan: dict = {"running": False, "success": False}
        self.signal: dict | None = None
        self.polls = 0
        self.scan_requests: list[dict] = []
        self.cancelled = 0

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/v2/sys/version", self.version)
        app.router.add_get("/v2/token", self.token)
        app.router.add_get("/v2/signal/scan", self.auth(self.scan_get))
        app.router.add_put("/v2/signal/scan", self.auth(self.scan_put))
        app.router.add_delete("/v2/signal/scan", self.auth(self.scan_delete))
        app.router.add_get("/v2/signal/scan/signal", self.auth(self.scan_signal))
        app.router.add_put("/v2/signal/tx", self.auth(self.tx))
        return app

    def auth(self, handler):
        async def wrapped(request: web.Request) -> web.Response:
            if request.headers.get("BOND-Token") != TOKEN:
                return web.Response(status=401)
            return await handler(request)

        return wrapped

    async def version(self, request):
        return web.json_response(
            {"target": "zermatt-s3", "fw_ver": "v4.40.1", "bondid": "ZZE2E00001"}
        )

    async def token(self, request):
        return web.json_response({"locked": 1, "pin_attempts_left": 5})

    async def scan_put(self, request):
        self.scan_requests.append(await request.json())
        self.scan = {"running": True, "success": False}
        self.polls = 0
        self.signal = None
        return web.Response(status=201)

    async def scan_get(self, request):
        if self.scan["running"]:
            self.polls += 1
            if self.polls >= 2:  # the button gets pressed after a moment
                result = self.scan_queue.pop(0) if self.scan_queue else None
                self.signal = result
                self.scan = {"running": False, "success": result is not None}
        return web.json_response({**self.scan, "progress": 0, "freq": 447885})

    async def scan_delete(self, request):
        self.cancelled += 1
        self.scan = {"running": False, "success": False}
        return web.Response(status=204)

    async def scan_signal(self, request):
        if self.signal is None:
            return web.Response(status=204)
        return web.json_response(self.signal)

    async def tx(self, request):
        self.transmitted.append(await request.json())
        return web.Response(status=204)
