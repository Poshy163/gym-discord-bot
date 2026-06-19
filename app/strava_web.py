"""Tiny aiohttp web surface for the Strava integration.

The bot is otherwise a pure gateway client with no HTTP server. Strava's OAuth
redirect and its webhook push both need a publicly reachable URL, so we run a
minimal aiohttp app on the bot's own event loop (started from ``setup_hook``).

This module is intentionally thin: it only does request routing/validation and
delegates the real work to async callbacks injected by ``bot.py`` (which owns
the DB and the Discord client). Keeping Discord/DB logic out of here makes the
routes trivial to reason about and test.

Routes
------
GET  /strava/callback   OAuth redirect target — exchanges ?code for tokens.
GET  /strava/webhook    Strava subscription validation handshake.
POST /strava/webhook    Strava event push (activity created/updated/deleted).
GET  /healthz           Liveness probe.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from aiohttp import web

LOG = logging.getLogger("gymbot.strava.web")

# Callback signatures injected by the bot:
#   on_callback(code, state, error) -> str   (HTML body shown to the user)
#   on_event(payload: dict)         -> None  (process a webhook event)
CallbackHandler = Callable[[str | None, str | None, str | None], Awaitable[str]]
EventHandler = Callable[[dict], Awaitable[None]]


def build_app(
    *,
    verify_token: str,
    on_callback: CallbackHandler,
    on_event: EventHandler,
    schedule: Callable[[Awaitable[None]], None],
) -> web.Application:
    """Construct the aiohttp application.

    ``schedule`` runs a coroutine in the background (typically
    ``bot.loop.create_task``) — used so the webhook POST can ack within Strava's
    2-second window while the activity fetch + Discord post happen afterwards.
    """

    async def callback(request: web.Request) -> web.Response:
        q = request.query
        body = await on_callback(
            q.get("code"), q.get("state"), q.get("error")
        )
        return web.Response(text=body, content_type="text/html")

    async def webhook_verify(request: web.Request) -> web.Response:
        # Strava validates the subscription by GETting the callback with
        # hub.mode=subscribe and echoing back hub.challenge — but only if our
        # verify_token matches the one supplied at subscription-create time.
        q = request.query
        if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == verify_token:
            LOG.info("Strava webhook subscription validated")
            return web.json_response({"hub.challenge": q.get("hub.challenge", "")})
        LOG.warning(
            "Strava webhook validation rejected (mode=%s token_ok=%s)",
            q.get("hub.mode"), q.get("hub.verify_token") == verify_token,
        )
        return web.Response(status=403, text="verify_token mismatch")

    async def webhook_event(request: web.Request) -> web.Response:
        # Ack fast: Strava retries (and eventually disables the subscription) if
        # we don't 200 within ~2s. Do the heavy lifting off the request path.
        try:
            payload = await request.json()
        except Exception:
            return web.Response(status=400, text="bad json")
        LOG.info(
            "Strava webhook event: object=%s aspect=%s id=%s owner=%s",
            payload.get("object_type"), payload.get("aspect_type"),
            payload.get("object_id"), payload.get("owner_id"),
        )
        schedule(on_event(payload))
        return web.Response(status=200, text="ok")

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.add_routes(
        [
            web.get("/strava/callback", callback),
            web.get("/strava/webhook", webhook_verify),
            web.post("/strava/webhook", webhook_event),
            web.get("/healthz", health),
        ]
    )
    return app


async def start_server(
    app: web.Application, host: str, port: int
) -> web.AppRunner:
    """Start the app and return the runner (so callers can clean it up)."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    LOG.info("Strava web server listening on %s:%d", host, port)
    return runner


# Small static HTML pages shown to the user after the OAuth redirect.
def success_page(athlete_name: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Strava linked</title>
<style>body{{font-family:system-ui,sans-serif;background:#111;color:#eee;
text-align:center;padding:4rem 1rem}}.c{{color:#fc4c02;font-weight:700}}</style>
</head><body>
<h1>✅ <span class="c">Strava</span> linked!</h1>
<p>Thanks, <strong>{athlete_name}</strong>. Your new workouts will now post to
the server feed automatically.</p>
<p>You can close this tab and head back to Discord.</p>
</body></html>"""


def error_page(message: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Strava link failed</title>
<style>body{{font-family:system-ui,sans-serif;background:#111;color:#eee;
text-align:center;padding:4rem 1rem}}</style>
</head><body>
<h1>⚠️ Couldn't link Strava</h1>
<p>{message}</p>
<p>Head back to Discord and run <code>/strava_link</code> again.</p>
</body></html>"""
