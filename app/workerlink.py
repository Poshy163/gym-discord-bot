"""RPC bridge between the supervisor (which serves the dashboard) and the bot.

Only calls that genuinely need a live Discord gateway cross this boundary --
listing channels, sending an invite, editing roles, reading moderation state.
Everything the dashboard can compute from SQLite stays in the supervisor, which
is why the member page keeps working while the bot is down.

Deliberately NOT proxied: ``today_window``, ``calorie_streak`` and
``protein_streak``. ``app/webui.py`` calls all three synchronously (:211,
:430-431), so replacing them with coroutine functions would put coroutine
objects into ``web.json_response`` and 500 the member endpoint. They live in
``app/dayspans.py`` and the supervisor calls them directly.

Transport is aiohttp over a unix socket -- no new dependency, no TCP port, and
filesystem permissions do the access control. ``app/webui.py`` already types
every injected Discord handler as optional and returns 503 when one is missing,
so "the bot is offline" reuses a path that has existed since day one.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import sys
from typing import Any, Callable

import aiohttp
from aiohttp import web

LOG = logging.getLogger("gymbot.workerlink")

#: Set in the worker's environment by the supervisor. Its presence is what
#: tells app/bot.py to serve RPC at all, so `python -m app.bot` standalone
#: (dev, tests) behaves exactly as it always has.
ROLE_ENV = "GYMBOT_ROLE"
SOCKET_ENV = "GYMBOT_CTL_SOCK"

#: Per-method budgets. A resync of a large guild is not a 10-second call, and a
#: fixed timeout there produces spurious 503s that read as "the bot is down"
#: when it is merely busy.
TIMEOUTS: dict[str, float] = {
    "resync": 120.0,
    "list_channels": 30.0,
    "invite_user": 30.0,
    "_default": 10.0,
}


class WorkerDown(RuntimeError):
    """The bot process is not reachable right now."""


def unix_sockets_supported() -> bool:
    """False on Windows, where aiohttp has no UnixSite.

    Production is Linux; this exists so the repo stays runnable on the Windows
    machine it is developed on, where the supervisor falls back to serving the
    dashboard without a worker link.
    """
    return sys.platform != "win32" and hasattr(web, "UnixSite")


# ---------------------------------------------------------------------------
# Worker side
# ---------------------------------------------------------------------------

async def serve(sock_path: str, methods: dict[str, Callable[..., Any]]):
    """Expose ``methods`` over a unix socket. Returns the AppRunner."""
    directory = os.path.dirname(sock_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    # A SIGKILLed predecessor (OOM, `docker kill`, host reboot) leaves the
    # socket file behind and the next bind fails with EADDRINUSE -- which the
    # supervisor would then report as a crash loop for a reason unrelated to
    # whatever config change triggered the restart.
    with contextlib.suppress(FileNotFoundError, OSError):
        os.unlink(sock_path)

    async def call(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "bad request"},
                                     status=400)
        name = body.get("m")
        fn = methods.get(name)
        if fn is None:
            return web.json_response({"ok": False, "error": f"unknown method {name!r}"},
                                     status=404)
        try:
            result = fn(*body.get("a", []))
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 - report, never kill the bot
            LOG.exception("RPC %s failed", name)
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500,
            )
        return web.json_response({"ok": True, "r": result})

    app = web.Application()
    app.add_routes([web.post("/rpc", call)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.UnixSite(runner, sock_path).start()
    with contextlib.suppress(OSError):
        os.chmod(sock_path, 0o600)
    LOG.info("Worker control socket listening at %s", sock_path)
    return runner


# ---------------------------------------------------------------------------
# Supervisor side
# ---------------------------------------------------------------------------

class WorkerLink:
    """Client half. Every call raises :class:`WorkerDown` when the bot is not up."""

    def __init__(self, sock_path: str) -> None:
        self.sock_path = sock_path
        self._session: aiohttp.ClientSession | None = None
        self.up = False

    async def start(self) -> None:
        """Open the client session.

        No-op where unix sockets are unavailable (Windows, i.e. local
        development). The link then simply never comes up, every live-Discord
        dashboard action returns the same 503 it already returns when a handler
        was never injected, and everything computed from SQLite keeps working.
        Production is Linux, so this only costs the invite/role/timeout buttons
        on a dev machine.
        """
        if not unix_sockets_supported():
            LOG.info(
                "Unix sockets are unavailable on this platform — the "
                "dashboard's live Discord actions will report the bot as "
                "offline. Everything read from the database still works."
            )
            return
        if self._session is None:
            connector = aiohttp.UnixConnector(path=self.sock_path)
            self._session = aiohttp.ClientSession(connector=connector)

    async def close(self) -> None:
        # Drop the reference FIRST. If this coroutine is cancelled inside
        # close(), leaving a closed-but-still-referenced session behind would
        # make start() decline to rebuild it ("is not None") and every later
        # RPC would raise "Session is closed" — surfacing as a 500 rather than
        # the intended 503, because only WorkerDown is translated.
        session, self._session = self._session, None
        self.up = False
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()

    async def reset(self) -> None:
        """Rebuild the session after a worker restart (the old socket is gone)."""
        await self.close()
        await self.start()

    async def call(self, method: str, *args: Any) -> Any:
        if not self.up:
            raise WorkerDown(method)
        return await self._call(method, *args)

    async def _call(self, method: str, *args: Any) -> Any:
        """Dial without consulting :attr:`up`, so :meth:`ping` can set it."""
        if self._session is None:
            raise WorkerDown(method)
        timeout = TIMEOUTS.get(method, TIMEOUTS["_default"])
        try:
            async with self._session.post(
                "http://worker/rpc",
                json={"m": method, "a": list(args)},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise WorkerDown(method) from exc
        if not data.get("ok"):
            raise RuntimeError(data.get("error", "rpc failed"))
        return data["r"]

    def proxy(self, method: str) -> Callable[..., Any]:
        """A drop-in for one of ``webui.build_app``'s injected async handlers."""
        async def _call(*args: Any) -> Any:
            return await self.call(method, *args)
        return _call

    async def ping(self) -> dict[str, Any] | None:
        """Cheap liveness probe. Returns the worker's status, or None if down.

        Bypasses the :attr:`up` gate deliberately -- this is what SETS it, so
        checking it here would make the flag a one-way door.
        """
        try:
            status = await self._call("status")
        except Exception:  # noqa: BLE001
            self.up = False
            return None
        self.up = True
        return status if isinstance(status, dict) else {}
