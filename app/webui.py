"""Authenticated web dashboard for the gym bot.

A second aiohttp surface (separate port from the Strava callback server) that
lets an operator browse and edit everything the bot tracks — lifts, calories,
protein, bodyweight — alongside a mirror of the guild's members and roles and a
unified audit log.

Design mirrors ``app/strava_web.py``: this module owns only request
routing/auth/rendering. All data access goes through the injected ``Database``
instance; live Discord lookups (current guild list, role colours) are already
mirrored into SQLite by ``bot.py`` so the dashboard keeps working even while the
gateway is reconnecting.

Auth is a single shared password (``WEBUI_PASSWORD``). On success we mint an
opaque session token and set it as an HttpOnly cookie. Normal sessions remain
in-process; an explicitly remembered login stores only the token's SHA-256
digest in SQLite so it can survive supervisor restarts. There is no per-user
identity — dashboard edits are audited under the label ``web:<ip>``.

Routes
------
GET  /login            Password form.
POST /login            Verify password, set session cookie.
POST /logout           Clear session.
GET  /                 Single-page dashboard shell (HTML).
GET  /api/guilds       Guilds the dashboard knows about.
GET  /api/overview     Server totals + recent audit for a guild.
GET  /api/members      Member list with role counts.
GET  /api/member       One member: roles, nutrition, lift counters, audit.
GET  /api/roles        Roles with member counts.
GET  /api/role         Members holding a role.
GET  /api/audit        Filterable audit log slice.
GET  /api/lifts        Lift rows (optionally one user).
GET  /api/calories     Calorie rows.
GET  /api/protein      Protein rows.
GET  /api/activity     Per-tracked-user game/app activity (overlap-aware totals).
GET  /api/activity/log One member's play sessions (start/end/duration) + rollup.
GET  /api/sleep        Per-tracked-user nightly sleep sessions from presence.
POST /api/lifts/delete, /api/lifts/edit, /api/calories/delete,
     /api/protein/delete   Edit endpoints (audited).
GET  /healthz          Liveness probe (no auth).
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from aiohttp import web
from yarl import URL  # already a hard dependency of aiohttp

from . import game_icons, ha_client, presence, targets
from .voicetime import summarize_voice

LOG = logging.getLogger("gymbot.webui")

SESSION_COOKIE = "gymdash_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600  # a week
REMEMBER_SESSION_TTL_SECONDS = 30 * 24 * 3600

# Typo guards on the nutrition-target form, mirroring the bot's slash commands
# (app.bot._MAX_TARGET_KCAL / _MAX_PROTEIN_TARGET_G).
_MAX_TARGET_KCAL = 20_000
_MAX_TARGET_PROTEIN_G = 500

# A member can accumulate years of daily scale readings. The dashboard only
# needs a recent trend window, and bounding each registry metric here prevents
# one member response from growing with the age of the database.
_MEMBER_BODY_METRIC_HISTORY_LIMIT = 90

# A callable the bot can inject so the dashboard can resync members/roles on
# demand (the "Refresh" button). Optional — None disables the button.
ResyncHandler = Callable[[int], Awaitable[bool]]

# Live Discord actions the bot injects. All optional — when None the matching
# endpoint returns 503. They run on the bot's event loop and exchange plain
# dicts (no Discord objects cross this boundary).
ChannelsHandler = Callable[[int], Awaitable[list[dict]]]
# (guild_id, user_id, channel_id|None, actor_name) -> result dict
InviteHandler = Callable[[int, int, "int | None", str], Awaitable[dict]]
# (guild_id, user_id, role_id, add, actor_name) -> result dict
RoleHandler = Callable[[int, int, int, bool, str], Awaitable[dict]]
# (guild_id, user_id) -> moderation state dict
ModerationHandler = Callable[[int, int], Awaitable[dict]]
# (guild_id, user_id, actor_name) -> result dict
TimeoutHandler = Callable[[int, int, str], Awaitable[dict]]
# (guild_id, user_id, enable, actor_name) -> result dict — add/remove a member
# from the auto un-timeout protected list (the per-member Moderation toggle).
AutoUntimeoutHandler = Callable[[int, int, bool, str], Awaitable[dict]]
# (guild_id, user_id, reason|None, actor_name) -> result dict — posts a public
# chat message pinging the blacklisted user with the reason.
BlacklistAnnouncer = Callable[[int, int, "str | None", str], Awaitable[dict]]
# (guild_id) -> list of {channel_id, channel_name, members:[…]} currently in VC.
VoiceSnapshotHandler = Callable[[int], Awaitable[list[dict]]]
# (guild_id, user_id, start, actor_name) -> result dict — start/stop recording a
# member's presence + activity (the dashboard's "Presence tracking" control).
PresenceTrackHandler = Callable[[int, int, bool, str], Awaitable[dict]]
# (user_id) -> current consecutive-day logging streak. Injected so the dashboard
# reuses the bot's timezone-correct streak logic; None hides the streak chips.
StreakHandler = Callable[[int], int]

# Injected by app/supervisor.py. Typed loosely on purpose: app/webui.py must
# not import app.settings_service or app.supervisor, or the module could no
# longer be built in isolation by the tests.
AuthProvider = Any        # app.settings_service.DbAuth
SettingsProvider = Any    # app.settings_service.SettingsService
SupervisorProvider = Any  # app.supervisor.WorkerSupervisor


#: The dashboard's tabs, as ``(slug, icon)``. Single source of truth: the JS
#: nav is generated from this, and every slug gets its own GET route so
#: ``/overview``, ``/members`` etc. are real bookmarkable URLs rather than
#: client-only state. Without the routes, a refresh or a shared link 404s.
DASHBOARD_TABS: tuple[tuple[str, str], ...] = (
    ("overview", "📊"),
    ("members", "👥"),
    ("activity", "🎮"),
    ("sleep", "💤"),
    ("messages", "💬"),
    ("voice", "🔊"),
    ("roles", "🛡️"),
    ("leaderboard", "🏆"),
    ("audit", "📜"),
    ("lifts", "🏋️"),
    ("calories", "🔥"),
    ("protein", "🥩"),
    ("settings", "⚙️"),
)


def _esc(text: str) -> str:
    """Escape untrusted text before it goes into one of the HTML templates."""
    return html.escape(str(text), quote=True)


def _safe_next(raw: str | None) -> str:
    """Sanitise a post-login redirect target.

    ``next`` is attacker-controllable on an unauthenticated page, so anything
    that is not plainly a same-origin absolute path collapses to ``/``. The
    rejected cases, and why each matters:

    * ``https://evil.example`` — absolute URL, another origin.
    * ``//evil.example`` — protocol-relative; browsers treat it as another
      origin even though it starts with a slash.
    * ``/\\evil.example`` — browsers normalise backslashes to forward slashes,
      so this becomes protocol-relative too. This is the bypass that catches
      naive "starts with / and not //" checks.
    * anything containing CR, LF, TAB or NUL — those would be smuggled into the
      ``Location`` response header.
    """
    if not raw:
        return "/"
    if any(ch in raw for ch in "\r\n\t\x00"):
        return "/"
    if "\\" in raw:
        return "/"
    if not raw.startswith("/") or raw.startswith("//"):
        return "/"
    # Final catch-all: the value ends up in web.HTTPFound, which parses it with
    # yarl. Something like "/[" raises ValueError("Invalid IPv6 URL") there and
    # would surface as a 500 on the login POST -- losing the operator's sign-in
    # to a crafted link. Parse it here instead and fall back quietly.
    try:
        URL(raw)
    except Exception:  # noqa: BLE001 - any parse failure means "not usable"
        return "/"
    return raw


async def _json_body(request: "web.Request") -> dict:
    """Parse a JSON request body, 400ing on anything that isn't an object."""
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="invalid json")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="expected a JSON object")
    return body


def _set_session_cookie(
    request: "web.Request", resp, token: str, *, remember: bool = False,
) -> None:
    """Set the session cookie, adding Secure only when the request arrived
    over HTTPS.

    Setting Secure unconditionally would break every plain-HTTP LAN deployment
    (the cookie would be set and never sent back), so it keys off the proxy's
    X-Forwarded-Proto and the request scheme instead.
    """
    https = (
        request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
        == "https"
        or request.scheme == "https"
    )
    resp.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="Lax",
        max_age=REMEMBER_SESSION_TTL_SECONDS if remember else None,
        secure=https, path="/",
    )


class _Sessions:
    """Short in-memory sessions plus hashed 30-day remembered sessions."""

    def __init__(self, db) -> None:
        self._db = db
        self._store: dict[str, float] = {}
        self._db.web_sessions_prune()

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(self, *, remember: bool = False) -> str:
        token = secrets.token_urlsafe(32)
        if remember:
            expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=REMEMBER_SESSION_TTL_SECONDS,
            )
            self._db.web_session_add(self._digest(token), expires_at)
        else:
            self._store[token] = time.time() + SESSION_TTL_SECONDS
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        exp = self._store.get(token)
        if exp is not None and exp < time.time():
            self._store.pop(token, None)
            exp = None
        if exp is not None:
            return True
        return self._db.web_session_valid(self._digest(token))

    def drop(self, token: str | None) -> None:
        if token:
            self._store.pop(token, None)
            self._db.web_session_remove(self._digest(token))

    def clear(self) -> None:
        """Invalidate every live session (used when the password is rotated)."""
        self._store.clear()
        self._db.web_sessions_clear()


# Login brute-force guard: after this many wrong passwords from one IP within
# the window, that IP is locked out for the cooldown. Counts reset on success.
LOGIN_MAX_FAILS = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60


class _LoginThrottle:
    """Per-IP failed-login counter with a temporary lockout.

    In-process and best-effort (a determined attacker behind rotating IPs isn't
    stopped), but it turns the single shared password from "unlimited online
    guessing" into "5 tries per IP per 15 min", which is the point."""

    def __init__(self) -> None:
        self._fails: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def locked_for(self, ip: str) -> int:
        """Seconds remaining on this IP's lockout, or 0 if not locked."""
        until = self._locked_until.get(ip, 0.0)
        remaining = until - time.time()
        return int(remaining) if remaining > 0 else 0

    def record_failure(self, ip: str) -> None:
        now = time.time()
        recent = [t for t in self._fails.get(ip, []) if now - t < LOGIN_LOCKOUT_SECONDS]
        recent.append(now)
        self._fails[ip] = recent
        if len(recent) >= LOGIN_MAX_FAILS:
            self._locked_until[ip] = now + LOGIN_LOCKOUT_SECONDS
            self._fails[ip] = []

    def record_success(self, ip: str) -> None:
        self._fails.pop(ip, None)
        self._locked_until.pop(ip, None)


def build_app(
    *,
    db,
    password: str = "",
    auth: "AuthProvider | None" = None,
    settings: "SettingsProvider | None" = None,
    supervisor: "SupervisorProvider | None" = None,
    resync: ResyncHandler | None = None,
    today_window: Callable[[], tuple[str, str]] | None = None,
    list_channels: ChannelsHandler | None = None,
    invite_user: InviteHandler | None = None,
    set_member_role: RoleHandler | None = None,
    member_moderation: ModerationHandler | None = None,
    remove_timeout: TimeoutHandler | None = None,
    set_auto_untimeout: AutoUntimeoutHandler | None = None,
    announce_blacklist: BlacklistAnnouncer | None = None,
    voice_snapshot: VoiceSnapshotHandler | None = None,
    presence_track: PresenceTrackHandler | None = None,
    presence_enabled: "bool | Callable[[], bool]" = False,
    calorie_streak: StreakHandler | None = None,
    protein_streak: StreakHandler | None = None,
    media_dir: "str | Callable[[], str | None] | None" = None,
    display_tz=timezone.utc,  # tzinfo, or a zero-arg callable returning one
) -> web.Application:
    """Construct the dashboard aiohttp application.

    ``db`` is the shared :class:`app.db.Database`.

    ``auth`` (optional) is an :class:`app.settings_service.DbAuth` providing the
    hashed-password and first-boot claim flow. ``settings`` (optional) is an
    :class:`app.settings_service.SettingsService` powering the Settings tab, and
    ``supervisor`` (optional) is the process manager behind the bot-status card
    and the Apply/restart button. When all three are None this behaves exactly
    as it did before they existed: static ``password`` comparison, no setup
    mode, no Settings tab — which is the shape every existing test uses.

    ``password`` is the legacy shared login secret; ``auth`` supersedes it.
    ``resync`` (optional) re-pulls member/role state from Discord.
    ``today_window`` (optional) returns the ``(start_iso, end_iso)`` of "today"
    in the bot's display timezone — used for today's nutrition totals; falls
    back to a UTC calendar day. ``list_channels`` / ``invite_user`` /
    ``set_member_role`` / ``member_moderation`` / ``remove_timeout`` (optional)
    are live Discord actions powering the invite, role-grant and timeout
    controls; when not injected those endpoints return 503.
    ``announce_blacklist`` (optional) posts a public chat message pinging a
    just-blacklisted user with the reason; when not injected blacklisting still
    works silently (no announcement). ``voice_snapshot`` (optional) returns the
    live "who's in VC now" occupancy for the Voice tab; when not injected the
    tab shows only the logged join/leave history. ``presence_track`` (optional)
    starts/stops recording a member's presence + activity from the member panel;
    ``presence_enabled`` mirrors the bot's ENABLE_PRESENCE_TRACKING flag so the
    dashboard only offers the control when the feature is actually on.
    ``display_tz`` is the timezone the Sleep tab attributes nightly sessions to
    (defaults to UTC); pass the bot's display timezone so wake-up dates match
    the operator's local sense of time.
    """
    sessions = _Sessions(db)
    login_throttle = _LoginThrottle()

    def _today() -> tuple[str, str]:
        if today_window is not None:
            return today_window()
        from datetime import datetime, timedelta, timezone
        start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        return start.isoformat(), (start + timedelta(days=1)).isoformat()

    # ---- auth helpers ----------------------------------------------------

    def _claimed() -> bool:
        """True once a password exists. Always True in the legacy static mode."""
        return auth.is_claimed() if auth is not None else True

    # ``media_dir``, ``display_tz`` and ``presence_enabled`` may each be given
    # as a plain value (the legacy shape, still used by the tests) or as a
    # zero-arg callable. The supervisor passes callables so that changing them
    # in the Settings tab takes effect on the next request — otherwise they
    # would be frozen at supervisor boot, and a "hot" setting that reported
    # "Saved, no restart needed" would keep serving the old value until the
    # whole container was restarted.

    def _media_dir() -> "str | None":
        return media_dir() if callable(media_dir) else media_dir

    def _display_tz():
        return display_tz() if callable(display_tz) else display_tz

    def _presence_enabled() -> bool:
        return bool(presence_enabled() if callable(presence_enabled)
                    else presence_enabled)

    def _authed(request: web.Request) -> bool:
        return sessions.valid(request.cookies.get(SESSION_COOKIE))

    def _require(request: web.Request) -> None:
        # The unclaimed check comes FIRST and deliberately does not rely on
        # "no session can exist yet". Binding the port without a password must
        # not open a single API route, and leaning on session absence for that
        # would be one refactor away from a data leak.
        if not _claimed():
            raise web.HTTPUnauthorized(text="setup required")
        if not _authed(request):
            raise web.HTTPUnauthorized(text="login required")

    def _client_ip(request: web.Request) -> str:
        peer = request.headers.get("X-Forwarded-For") or (request.remote or "?")
        return peer.split(",")[0].strip()

    def _actor(request: web.Request) -> str:
        # No per-user identity; attribute edits to the requesting IP so the
        # audit trail at least distinguishes operators on a shared deployment.
        return f"web:{_client_ip(request)}"

    def _guild_id(request: web.Request) -> int:
        try:
            return int(request.query["guild"])
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text="missing or invalid ?guild")

    # ---- auth routes -----------------------------------------------------

    def _login_page(request: web.Request, error: str = "") -> str:
        """LOGIN_HTML with the post-login target and any error filled in."""
        nxt = _safe_next(request.query.get("next"))
        # safe="/" so ?, = and & inside the target are percent-encoded. Leaving
        # them literal split a multi-parameter target at the first & when the
        # form was submitted -- "/activity?guild=1&days=30" came back as
        # next="/activity?guild=1" plus a stray days=30, silently dropping part
        # of the view the operator was returning to.
        body = LOGIN_HTML.replace(
            'action="/login"',
            f'action="/login?next={_esc(quote(nxt, safe="/"))}"',
        )
        return body.replace("<!--ERR-->", error)

    async def login_get(request: web.Request) -> web.Response:
        if not _claimed():
            raise web.HTTPFound("/setup")
        if _authed(request):
            raise web.HTTPFound(_safe_next(request.query.get("next")))
        return web.Response(text=_login_page(request), content_type="text/html")

    # ---- first-boot claim -------------------------------------------------

    async def setup_get(request: web.Request) -> web.Response:
        """The claim page. Unauthenticated by necessity — nothing exists yet."""
        if _claimed():
            raise web.HTTPFound("/login")
        return web.Response(
            text=SETUP_HTML.replace("<!--ERR-->", ""), content_type="text/html",
        )

    async def setup_post(request: web.Request) -> web.Response:
        if _claimed():
            raise web.HTTPFound("/login")
        ip = _client_ip(request)
        data = await request.post()
        password = str(data.get("password", ""))
        confirm = str(data.get("password2", ""))
        remember = str(data.get("remember", "")).lower() in {
            "1", "true", "on", "yes",
        }

        error = None
        if password != confirm:
            error = "Those two passwords don't match."
        else:
            error = auth.claim(password, actor=_actor(request))

        if error:
            body = SETUP_HTML.replace(
                "<!--ERR-->", f'<p class="err" role="alert">{_esc(error)}</p>',
            )
            return web.Response(text=body, content_type="text/html", status=400)

        # Record who took the instance. The claim is unauthenticated by design,
        # so attribution is the only accountability available.
        try:
            db.add_audit(
                0, "settings", "dashboard_claimed",
                actor_name=_actor(request),
                detail=f"Dashboard claimed from {ip}",
            )
        except Exception:  # noqa: BLE001 - never block setup on the audit write
            LOG.warning("Could not audit the dashboard claim", exc_info=True)

        token = sessions.create(remember=remember)
        resp = web.HTTPFound("/")
        _set_session_cookie(request, resp, token, remember=remember)
        LOG.warning("Dashboard claimed from %s.", ip)
        return resp

    async def login_post(request: web.Request) -> web.Response:
        ip = _client_ip(request)
        locked = login_throttle.locked_for(ip)
        if locked:
            mins = max(1, locked // 60)
            LOG.warning("Dashboard login blocked (locked out) from %s", ip)
            body = _login_page(
                request,
                f'<p class="err" role="alert">Too many attempts. Try again in ~{mins} min.</p>',
            )
            return web.Response(
                text=body, content_type="text/html", status=429,
                headers={"Retry-After": str(locked)},
            )
        data = await request.post()
        supplied = str(data.get("password", ""))
        remember = str(data.get("remember", "")).lower() in {
            "1", "true", "on", "yes",
        }
        # Constant-time compare so the form can't be used as a timing oracle.
        # ``auth`` (when injected) checks the stored PBKDF2 hash and the legacy
        # environment pin; without it we fall back to the static comparison the
        # module has always used.
        if auth is not None:
            ok = auth.verify(supplied)
        else:
            ok = bool(password) and hmac.compare_digest(supplied, password)
        if ok:
            login_throttle.record_success(ip)
            token = sessions.create(remember=remember)
            resp = web.HTTPFound(_safe_next(request.query.get("next")))
            _set_session_cookie(request, resp, token, remember=remember)
            LOG.info("Dashboard login from %s", _actor(request))
            return resp
        login_throttle.record_failure(ip)
        LOG.warning("Failed dashboard login from %s", ip)
        body = _login_page(
            request, '<p class="err" role="alert">Wrong password.</p>',
        )
        return web.Response(text=body, content_type="text/html", status=401)

    async def logout_post(request: web.Request) -> web.Response:
        sessions.drop(request.cookies.get(SESSION_COOKIE))
        resp = web.HTTPFound("/login")
        resp.del_cookie(SESSION_COOKIE, path="/")
        return resp

    async def index(request: web.Request) -> web.Response:
        if not _claimed():
            raise web.HTTPFound("/setup")
        if not _authed(request):
            # Carry the requested view through the login round-trip, so a
            # bookmarked /members/123 lands there instead of dumping you on the
            # overview and making you navigate back.
            target = request.path_qs
            raise web.HTTPFound(
                "/login" if target == "/"
                else f"/login?next={quote(target, safe='')}"
            )
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    # ---- settings ---------------------------------------------------------

    def _need_settings() -> None:
        if settings is None:
            raise web.HTTPServiceUnavailable(
                text="Settings are not available in this deployment.",
            )

    async def api_settings(request: web.Request) -> web.Response:
        _require(request)
        _need_settings()
        payload = settings.describe()
        payload["worker"] = (
            supervisor.status() if supervisor is not None
            else {"state": "unknown", "headline": "", "log": [],
                  "can_retry": False}
        )
        payload["history"] = [
            {
                "key": r["key"], "at": r["at"], "actor": r["actor"],
                "redacted": bool(r["redacted"]),
                "old": r["old_value"], "new": r["new_value"],
            }
            for r in db.settings_history(50)
        ]
        return web.json_response(payload)

    async def api_settings_set(request: web.Request) -> web.Response:
        _require(request)
        _need_settings()
        body = await _json_body(request)
        key = str(body.get("key", ""))
        if "value" not in body:
            raise web.HTTPBadRequest(text="missing value")
        value = body["value"]
        result = settings.set(
            key, None if value is None else str(value), actor=_actor(request),
        )
        if not result.get("ok"):
            return web.json_response(result, status=400)
        # Hot settings are pushed to a running bot immediately; everything else
        # is staged so an operator editing four fields gets one restart.
        if result.get("apply") == "hot" and supervisor is not None:
            await supervisor.reload_hot()
        return web.json_response(result)

    async def api_settings_apply(request: web.Request) -> web.Response:
        _require(request)
        _need_settings()
        if supervisor is None:
            raise web.HTTPServiceUnavailable(text="No process manager.")
        await supervisor.restart("settings applied")
        return web.json_response({"ok": True})

    async def api_settings_revert(request: web.Request) -> web.Response:
        _require(request)
        _need_settings()
        result = settings.revert_to_last_good(actor=_actor(request))
        if result.get("ok") and supervisor is not None:
            await supervisor.restart("settings reverted")
        return web.json_response(result)

    async def api_worker(request: web.Request) -> web.Response:
        _require(request)
        if supervisor is None:
            raise web.HTTPServiceUnavailable(text="No process manager.")
        if request.method == "POST":
            body = await _json_body(request)
            action = str(body.get("action", ""))
            if action == "restart":
                await supervisor.restart("requested from the dashboard")
            elif action == "stop":
                await supervisor.stop()
            else:
                raise web.HTTPBadRequest(text="unknown action")
        return web.json_response(supervisor.status())

    async def api_settings_export(request: web.Request) -> web.Response:
        _require(request)
        _need_settings()
        LOG.warning(
            "Settings exported as .env by %s — this file contains every secret "
            "in plaintext.", _actor(request),
        )
        return web.Response(
            text=settings.export_env(),
            content_type="text/plain",
            headers={"Content-Disposition": 'attachment; filename="gym-bot.env"'},
        )

    async def api_timezones(request: web.Request) -> web.Response:
        _require(request)
        try:
            from zoneinfo import available_timezones
            zones = sorted(available_timezones())
        except Exception:  # noqa: BLE001
            zones = ["UTC"]
        return web.json_response({"timezones": zones})

    async def api_password(request: web.Request) -> web.Response:
        _require(request)
        if auth is None:
            raise web.HTTPServiceUnavailable(text="Password changes are not "
                                                  "available in this deployment.")
        body = await _json_body(request)
        error = auth.change(str(body.get("old", "")), str(body.get("new", "")))
        if error:
            return web.json_response({"ok": False, "error": error}, status=400)
        # Rotating the password must invalidate existing cookies — otherwise a
        # remembered session minted with the OLD password keeps working, which
        # is exactly what you are trying to stop when you rotate it.
        sessions.clear()
        LOG.warning("Dashboard password changed by %s.", _actor(request))
        return web.json_response({"ok": True})

    # ---- JSON API: reads -------------------------------------------------

    async def api_guilds(request: web.Request) -> web.Response:
        _require(request)
        out = [
            {
                "id": str(r["guild_id"]),
                "name": r["name"] or f"Guild {r['guild_id']}",
                "member_count": r["member_count"],
            }
            for r in db.list_guilds()
        ]
        return web.json_response({"guilds": out})

    def _overview_live(gid: int) -> dict:
        """Live snapshot for the Overview header: who's online/playing now, the
        day's top games, average recent sleep, and a 7-day message sparkline.
        Loops only the (small) tracked-user set plus one grouped message query."""
        now = datetime.now(timezone.utc)
        day_since = now - timedelta(hours=24)
        week_since = now - timedelta(days=7)
        online = playing = tracked = 0
        game_today: dict[str, float] = {}
        sleep_avgs: list[float] = []
        for row in db.presence_track_list(gid):
            tracked += 1
            uid = int(row["user_id"])
            pres = db.presence_current(gid, uid)
            is_online = bool(pres and presence.is_online(pres["status"]))
            if is_online:
                online += 1
            cur = db.activity_current_set(gid, uid)
            if (pres is None or is_online) and cur and cur[0]:
                playing += 1
            for nm, secs in presence.summarize_activity_sets(
                db.activity_sets_for(gid, uid, since=day_since), day_since, now,
            ).items():
                game_today[nm] = game_today.get(nm, 0.0) + secs
            events = [
                (r["status"], r["at"])
                for r in db.presence_events_for(
                    gid, uid, since=week_since, until=now,
                )
            ]
            st = presence.sleep_stats(presence.nightly_sleep_sessions(
                events, week_since, now, display_tz=_display_tz(),
            ))
            if st["avg_hours"] is not None:
                sleep_avgs.append(st["avg_hours"])
        top_today = [
            {"name": nm, "seconds": round(secs),
             "image": game_icons.icon_for(nm)}
            for nm, secs in sorted(
                game_today.items(), key=lambda kv: kv[1], reverse=True,
            )[:5]
        ]
        # 7-day message sparkline (oldest→newest, zero-filled).
        counts = db.message_daily_counts(gid, week_since)
        msg_series = []
        for i in range(7, -1, -1):
            day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            msg_series.append({"date": day, "count": counts.get(day, 0)})
        return {
            "online": online, "playing": playing, "tracked": tracked,
            "top_today": top_today,
            "avg_sleep": (
                round(sum(sleep_avgs) / len(sleep_avgs), 1)
                if sleep_avgs else None
            ),
            "messages_7d": msg_series,
        }

    async def api_overview(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        totals = db.server_totals(gid) or {}
        members = db.list_members(gid)
        roles = db.list_guild_roles(gid)
        recent = [_audit_dict(r) for r in db.list_audit(gid, limit=15)]
        return web.json_response({
            "totals": _stringify_ids(dict(totals)),
            "member_count": len([m for m in members if m["present"]]),
            "known_members": len(members),
            "role_count": len(roles),
            "recent_audit": recent,
            "live": _overview_live(gid),
        })

    async def api_members(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        rows = db.list_members(gid)
        return web.json_response({"members": [
            {
                "user_id": str(r["user_id"]),
                "username": r["username"],
                "display_name": r["display_name"],
                "avatar": r["avatar"],
                "is_bot": bool(r["is_bot"]),
                "present": bool(r["present"]),
                "joined_at": r["joined_at"],
                "role_count": r["role_count"],
            }
            for r in rows
        ]})

    async def api_member(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        try:
            uid = int(request.query["user"])
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text="missing or invalid ?user")
        member = db.get_member(gid, uid)
        roles = db.member_role_names(gid, uid)
        overview = db.web_member_overview(gid, uid)
        audit = [_audit_dict(r) for r in db.list_audit(gid, subject_id=uid, limit=50)]
        start, end = _today()
        cal_goal = db.calorie_goal_get(gid, uid)
        pro_goal = db.protein_goal_get(gid, uid)
        cal_today, _ = db.calorie_total_between(gid, uid, start, end)
        pro_today, _ = db.protein_total_between(gid, uid, start, end)
        bw = [
            # ``id`` is what makes a single bogus reading deletable -- a scale can
            # log a mis-assigned or half-finished measurement, and without the row
            # id the only recourse was editing the database by hand.
            {"id": r["id"], "weight_kg": r["weight_kg"], "at": r["recorded_at"]}
            for r in db.bodyweight_history(gid, uid, limit=400)
        ]
        metric_specs = tuple(
            metric for metric in ha_client.METRICS
            if metric.key != ha_client.WEIGHT_KEY
        )
        metric_histories = db.body_metric_histories(
            uid,
            (metric.key for metric in metric_specs),
            limit_per_metric=_MEMBER_BODY_METRIC_HISTORY_LIMIT,
        )
        body_metrics = []
        for metric in metric_specs:
            # Weight already has its richer, deletable timeline above. Keeping it
            # out of this collection also avoids presenting the same reading twice.
            rows = metric_histories.get(metric.key, [])
            if not rows:
                continue
            body_metrics.append({
                "key": metric.key,
                "label": metric.label,
                "unit": metric.unit,
                "emoji": metric.emoji,
                "precision": metric.precision,
                "points": [
                    {
                        "value": r["value"],
                        # Stored units describe the normalized value. Fall back
                        # to the registry for old rows that predate unit storage.
                        "unit": r["unit"] or metric.unit,
                        "at": r["recorded_at"],
                    }
                    for r in rows
                ],
            })
        return web.json_response({
            "member": _member_dict(member) if member else {"user_id": str(uid)},
            "roles": [_role_dict(r) for r in roles],
            "overview": _stringify_ids(overview),
            "audit": audit,
            "strava_linked": db.get_strava_account(uid) is not None,
            "revo_linked": db.get_revo_account(uid) is not None,
            # The member's own Home Assistant. Only ever the host they connected
            # to -- the stored token is encrypted and is never exposed here.
            "ha_server": (
                lambda r: r["base_url"] if r is not None else None
            )(db.ha_server_get(uid)),
            "ha_prefix": (
                lambda r: r["entity_prefix"] if r is not None else None
            )(db.ha_get(uid)),
            "presence_tracked": db.presence_is_tracked(gid, uid),
            "presence_tracking_available": bool(
                _presence_enabled() and presence_track is not None
            ),
            "calorie_streak": calorie_streak(uid) if calorie_streak else None,
            "protein_streak": protein_streak(uid) if protein_streak else None,
            "nutrition": {
                # ``calorie_goal``/``protein_goal`` are the targets in force
                # TODAY (weekday or weekend, whichever applies) — that's what
                # the progress bars are measured against. The weekday/weekend
                # pair below is what the settings form edits.
                "calorie_goal": (
                    cal_goal["daily_target_kcal"] if cal_goal else None
                ),
                "calorie_today": cal_today,
                "protein_goal": (
                    pro_goal["daily_target_g"] if pro_goal else None
                ),
                "protein_today": pro_today,
                "targets": _targets_dict(db, uid),
            },
            "foods": [
                _food_dict(r, _alias_map(db, uid))
                for r in db.calorie_food_list(gid, uid)
            ],
            "lift_goals": [
                {
                    "equipment": r["equipment"],
                    "target_kg": r["target_kg"],
                    "bw": bool(r["bw"]),
                    "current_best": r["current_best"],
                }
                for r in db.goal_list(gid, uid)
            ],
            "bodyweights": bw,
            "body_metrics": body_metrics,
        })

    async def api_roles(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        rows = db.list_guild_roles(gid)
        return web.json_response({"roles": [_role_dict(r) for r in rows]})

    async def api_role(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        try:
            rid = int(request.query["role"])
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text="missing or invalid ?role")
        rows = db.members_with_role(gid, rid)
        return web.json_response({"members": [
            {
                "user_id": str(r["user_id"]),
                "username": r["username"],
                "display_name": r["display_name"],
                "avatar": r["avatar"],
                "present": bool(r["present"]),
            }
            for r in rows
        ]})

    async def api_audit(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        category = request.query.get("category") or None
        subject = request.query.get("user")
        subject_id = int(subject) if subject and subject.isdigit() else None
        limit = _clamp_int(request.query.get("limit"), 100, 1, 500)
        offset = _clamp_int(request.query.get("offset"), 0, 0, 1_000_000)
        rows = db.list_audit(
            gid, category=category, subject_id=subject_id,
            limit=limit, offset=offset,
        )
        total = db.count_audit(gid, category=category, subject_id=subject_id)
        return web.json_response({
            "audit": [_audit_dict(r) for r in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
        })

    async def api_lifts(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        uid = _opt_user(request)
        limit = _clamp_int(request.query.get("limit"), 100, 1, 500)
        offset = _clamp_int(request.query.get("offset"), 0, 0, 1_000_000)
        rows = db.web_list_lifts(gid, uid, limit=limit, offset=offset)
        return web.json_response({"lifts": [
            {
                "id": r["id"],
                "user_id": str(r["user_id"]),
                "username": r["username"],
                "equipment": r["equipment"],
                "weight_kg": r["weight_kg"],
                "bw": bool(r["bw"]),
                "reps": r["reps"],
                "logged_at": r["logged_at"],
            }
            for r in rows
        ]})

    async def api_calories(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        uid = _opt_user(request)
        limit = _clamp_int(request.query.get("limit"), 100, 1, 500)
        offset = _clamp_int(request.query.get("offset"), 0, 0, 1_000_000)
        rows = db.web_list_calories(gid, uid, limit=limit, offset=offset)
        return web.json_response({"calories": [
            {
                "id": r["id"],
                "user_id": str(r["user_id"]),
                "username": r["username"],
                "kcal": r["kcal"],
                "note": r["note"],
                "logged_at": r["logged_at"],
            }
            for r in rows
        ]})

    async def api_protein(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        uid = _opt_user(request)
        limit = _clamp_int(request.query.get("limit"), 100, 1, 500)
        offset = _clamp_int(request.query.get("offset"), 0, 0, 1_000_000)
        rows = db.web_list_protein(gid, uid, limit=limit, offset=offset)
        return web.json_response({"protein": [
            {
                "id": r["id"],
                "user_id": str(r["user_id"]),
                "username": r["username"],
                "grams": r["grams"],
                "note": r["note"],
                "logged_at": r["logged_at"],
            }
            for r in rows
        ]})

    async def api_foods(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        uid = _opt_user(request)
        if uid is None:
            raise web.HTTPBadRequest(text="?user required")
        rows = db.calorie_food_list(gid, uid)
        amap = _alias_map(db, uid)
        return web.json_response(
            {"foods": [_food_dict(r, amap) for r in rows]}
        )

    async def api_equipment(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        return web.json_response({"equipment": db.known_equipment(gid)})

    async def api_leaderboard(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        equipment = request.query.get("equipment", "").strip()
        if not equipment:
            raise web.HTTPBadRequest(text="?equipment required")
        rows = db.leaderboard(gid, equipment)
        return web.json_response({"equipment": equipment, "rows": [
            {
                "user_id": str(r["user_id"]),
                "username": r["username"],
                "best": r["best"],
                "bw": bool(r["bw"]),
                "set_on": r["set_on"],
            }
            for r in rows
        ]})

    def _act_icon(name: str, stored: str | None) -> str | None:
        """Resolve an activity's art from data we already have: its own
        rich-presence image (or one captured in an earlier session) first, then
        the curated/dynamic game-icon map. Returns None when only an app-id
        lookup could help — :func:`game_icons.app_icon` fills those in after a
        batched resolve."""
        return stored or game_icons.icon_for(name)

    async def api_activity(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        days = _clamp_int(request.query.get("days"), 7, 1, 90)
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=days)
        users = []
        missing_app_ids: set[int] = set()
        # Server-wide totals for the "most played" leaderboard:
        # name -> {seconds, players, stored image, app id}.
        server: dict[str, dict] = {}

        def _game(name, stored, app_id, **extra):
            # Carry the app id alongside so a single batched RPC resolve can
            # backfill icons for apps that ship no image (CurseForge, …).
            image = _act_icon(name, stored)
            if image is None and app_id:
                missing_app_ids.add(int(app_id))
            return {"name": name, "image": image, "_app": app_id, **extra}

        for row in db.presence_track_list(gid):
            uid = int(row["user_id"])
            member = db.get_member(gid, uid)
            display = member["display_name"] if member else str(uid)
            pres = db.presence_current(gid, uid)
            is_online = bool(pres and presence.is_online(pres["status"]))
            cur = db.activity_current_set(gid, uid)
            imgmap, appidmap = db.activity_metadata_maps(gid, uid)
            presence_events = [
                (event["status"], event["at"])
                for event in db.presence_events_for(
                    gid, uid, since=since, until=now,
                )
            ]
            presence_summary = presence.summarize_presence(
                presence_events, since, now, display_tz=_display_tz(),
            )
            observed = presence_summary.observed_seconds
            window_seconds = max(1.0, (now - since).total_seconds())
            status_for = None
            if pres:
                status_at = datetime.fromisoformat(pres["at"])
                if status_at.tzinfo is None:
                    status_at = status_at.replace(tzinfo=timezone.utc)
                status_for = max(
                    0, round((now - status_at.astimezone(timezone.utc)).total_seconds())
                )
            totals = presence.summarize_activity_sets(
                db.activity_sets_for(gid, uid, since=since), since, now,
            )
            for nm, secs in totals.items():
                e = server.setdefault(
                    nm, {"seconds": 0.0, "players": [], "img": None, "app": None}
                )
                e["seconds"] += secs
                e["players"].append(display)
                e["img"] = e["img"] or imgmap.get(nm)
                e["app"] = e["app"] or appidmap.get(nm)
            top = [
                _game(nm, imgmap.get(nm), appidmap.get(nm), seconds=round(secs))
                for nm, secs in list(totals.items())[:6]
            ]
            # Everything the user is running right now (a game plus a launcher,
            # two games, …), all sharing the snapshot's timestamp as "since".
            current_games = []
            if (pres is None or is_online) and cur is not None:
                acts, at = cur
                current_games = [
                    _game(d["n"], d["i"] or imgmap.get(d["n"]),
                          d.get("a") or appidmap.get(d["n"]), since=at)
                    for d in acts
                ]
            users.append({
                "user_id": str(uid),
                "display_name": display,
                "avatar": member["avatar"] if member else None,
                "status": pres["status"] if pres else None,
                "status_at": pres["at"] if pres else None,
                "status_for_seconds": status_for,
                "tracking_since": row["started_at"],
                "presence": {
                    "online_seconds": round(presence_summary.online_seconds),
                    "offline_seconds": round(presence_summary.offline_seconds),
                    "observed_seconds": round(observed),
                    "online_percent": (
                        round(presence_summary.online_seconds / observed * 100, 1)
                        if observed else None
                    ),
                    "coverage_percent": round(
                        min(1.0, observed / window_seconds) * 100, 1,
                    ),
                    "transitions": presence_summary.transitions,
                    "last_online_at": (
                        presence_summary.last_online_at.isoformat()
                        if presence_summary.last_online_at else None
                    ),
                },
                "current_games": current_games,
                "top_games": top,
            })

        leaders = sorted(
            server.items(), key=lambda kv: kv[1]["seconds"], reverse=True,
        )[:8]
        for _nm, e in leaders:  # make sure leaderboard icons resolve too
            if e["app"] and not (e["img"] or game_icons.icon_for(_nm)):
                missing_app_ids.add(int(e["app"]))

        # One batched RPC resolve for every app that still lacks art, then fill
        # the icons in and drop the internal app-id field from the payload.
        if missing_app_ids:
            await game_icons.resolve_app_icons(missing_app_ids)
        for u in users:
            for g in (*u["current_games"], *u["top_games"]):
                app_id = g.pop("_app", None)
                if g["image"] is None and app_id:
                    g["image"] = game_icons.app_icon(app_id)
        leaderboard = [
            {
                "name": nm,
                "seconds": round(e["seconds"]),
                "players": e["players"],
                "image": e["img"] or game_icons.icon_for(nm)
                or game_icons.app_icon(e["app"]),
            }
            for nm, e in leaders
        ]
        return web.json_response(
            {"users": users, "window_days": days, "leaderboard": leaderboard}
        )

    async def api_activity_log(request: web.Request) -> web.Response:
        """One member's play sessions — every stretch of a title, newest first.

        The Activity tab's cards answer "what have they played"; this answers
        "when". ``sessions`` is the raw log, ``games`` the same window rolled up
        per title (the "most played" view) so the panel needs a single fetch.
        """
        _require(request)
        gid = _guild_id(request)
        uid = _opt_user(request)
        if uid is None:
            return web.json_response(
                {"ok": False, "error": "a numeric ?user= is required"},
                status=400,
            )
        days = _clamp_int(request.query.get("days"), 7, 1, 90)
        # Someone who leaves a launcher running racks up a session a day, so a
        # busy 90-day window can run long; cap the payload and say we did.
        limit = _clamp_int(request.query.get("limit"), 400, 1, 2000)
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=days)

        member = db.get_member(gid, uid)
        pres = db.presence_current(gid, uid)
        imgmap, appidmap = db.activity_metadata_maps(gid, uid)
        events = db.activity_sets_for(gid, uid, since=since)
        sessions = presence.activity_sessions(
            events, since, now, display_tz=_display_tz(),
        )
        # Same source, same window: the per-title totals are exactly the sums of
        # the sessions above, so the rollup can't disagree with the log.
        totals = presence.summarize_activity_sets(events, since, now)

        # Resolve art once per title rather than once per session, batching the
        # app-id lookups the icon map can't answer (same fallback chain as
        # api_activity: rich-presence image → curated map → app-id RPC).
        names = {s["name"] for s in sessions} | set(totals)
        missing_app_ids = {
            int(appidmap[nm]) for nm in names
            if appidmap.get(nm) and not _act_icon(nm, imgmap.get(nm))
        }
        if missing_app_ids:
            await game_icons.resolve_app_icons(missing_app_ids)
        art = {
            nm: _act_icon(nm, imgmap.get(nm)) or game_icons.app_icon(
                appidmap.get(nm)
            )
            for nm in names
        }

        playing_now = (
            {
                d["n"]
                for d in (db.activity_current_set(gid, uid) or ([], ""))[0]
            }
            if pres is None or presence.is_online(pres["status"])
            else set()
        )
        per_game: dict[str, dict] = {}
        for s in sessions:
            g = per_game.setdefault(
                s["name"], {"sessions": 0, "last": None, "last_local": None},
            )
            g["sessions"] += 1
            g["last"], g["last_local"] = s["end"], s["end_local"]
        games = [
            {
                "name": nm,
                "image": art.get(nm),
                "seconds": round(secs),
                "sessions": per_game.get(nm, {}).get("sessions", 0),
                "last_played": per_game.get(nm, {}).get("last"),
                "last_played_local": per_game.get(nm, {}).get("last_local"),
                "playing_now": nm in playing_now,
            }
            for nm, secs in totals.items()
        ]

        newest_first = sessions[::-1]
        return web.json_response({
            "user_id": str(uid),
            "display_name": member["display_name"] if member else str(uid),
            "avatar": member["avatar"] if member else None,
            "status": pres["status"] if pres else None,
            "status_at": pres["at"] if pres else None,
            "tracked": db.presence_is_tracked(gid, uid),
            "window_days": days,
            "since": since.isoformat(),
            "until": now.isoformat(),
            "sessions": [
                {**s, "image": art.get(s["name"])} for s in newest_first[:limit]
            ],
            "session_count": len(sessions),
            "truncated": len(sessions) > limit,
            "games": games,
        })

    async def api_sleep(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        days = _clamp_int(request.query.get("days"), 7, 1, 90)
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=days)
        users = []
        for row in db.presence_track_list(gid):
            uid = int(row["user_id"])
            member = db.get_member(gid, uid)
            pres = db.presence_current(gid, uid)
            events = [
                (r["status"], r["at"])
                for r in db.presence_events_for(gid, uid, since=since, until=now)
            ]
            sessions = presence.nightly_sleep_sessions(
                events, since, now, display_tz=_display_tz(),
            )
            stats = presence.sleep_stats(sessions)
            users.append({
                "user_id": str(uid),
                "display_name": (
                    member["display_name"] if member else str(uid)
                ),
                "avatar": member["avatar"] if member else None,
                "status": pres["status"] if pres else None,
                "sessions": sessions,
                "nights": stats["nights"],
                "avg_hours": stats["avg_hours"],
                "stats": stats,
            })
        return web.json_response({"users": users, "window_days": days})

    # ---- JSON API: message history (Discord-style "Messages" tab) --------

    async def api_messages_channels(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        channels = [
            {
                "channel_id": str(r["channel_id"]) if r["channel_id"] else "",
                "channel_name": r["channel_name"],
                "count": int(r["count"]),
                "last_at": r["last_at"],
            }
            for r in db.message_channels(gid)
        ]
        blacklist = [
            {
                "user_id": str(r["user_id"]),
                "display_name": (
                    (m["display_name"]
                     if (m := db.get_member(gid, int(r["user_id"]))) else None)
                    or str(r["user_id"])
                ),
                "reason": r["reason"],
                "added_by": r["added_by"],
                "added_at": r["added_at"],
            }
            for r in db.message_blacklist_list(gid)
        ]
        return web.json_response({"channels": channels, "blacklist": blacklist})

    async def api_messages_log(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        try:
            cid = int(request.query["channel"])
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text="missing or invalid ?channel")
        limit = _clamp_int(request.query.get("limit"), 300, 1, 1000)
        rows = db.message_channel_log(gid, cid, limit=limit)
        def _media(raw: str | None) -> list:
            if not raw:
                return []
            try:
                items = json.loads(raw)
                return items if isinstance(items, list) else []
            except (ValueError, TypeError):
                return []

        messages = [
            {
                "user_id": str(r["user_id"]),
                "display_name": r["display_name"] or str(r["user_id"]),
                "avatar": r["avatar"],
                "content": r["content"],
                "media": _media(r["attachments"]),
                "at": r["at"],
                "edited": bool(r["edited_at"]),
                "deleted": bool(r["deleted_at"]),
            }
            for r in rows
        ]
        return web.json_response({"messages": messages})

    async def api_voice(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        # "Who's in VC right now" needs the bot; the join/leave log and the
        # 7-day totals do not. Degrade to the history rather than failing the
        # whole tab — the bot being down is the normal state during setup and
        # reconfiguration, which is exactly when the dashboard has to stay
        # useful.
        occupancy = []
        if voice_snapshot is not None:
            try:
                occupancy = await voice_snapshot(gid)
            except Exception as exc:  # noqa: BLE001
                if type(exc).__name__ != "WorkerDown":
                    raise
                LOG.debug("Voice snapshot unavailable — bot offline.")
        events = [
            {
                "user_id": str(r["user_id"]),
                "display_name": r["display_name"] or str(r["user_id"]),
                "avatar": r["avatar"],
                "event": r["event"],
                "channel": r["channel_name"],
                "at": r["at"],
            }
            for r in db.voice_events_recent(gid, limit=100)
        ]

        # Historical per-user totals over the last 7 days. The live snapshot only
        # exposes *self* mute/deaf (server mute/deaf isn't in it) — acceptable
        # here: it's just the tail-close hint for someone still connected, so a
        # server-muted-only member counts as active until their next logged mute.
        # A member present in the snapshot is verifiably in-call now; one absent
        # gets all-False live flags, so any unterminated interval is dropped
        # rather than accruing phantom time — the same conservative closed-tail
        # behaviour as /voice stats on a cache miss.
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=7)
        live_by_uid: dict[str, dict] = {}
        for chan in occupancy:
            for mem in chan.get("members", []):
                live_by_uid[str(mem.get("user_id"))] = mem
        # One voice_events_for + summarize_voice per member active in the window.
        # Bounded by a guild's worth of members over 7 days of events; cheap at
        # dashboard cadence, so it rides the existing liveVoice poll. Revisit
        # only if a very large guild's voice log makes this loop noticeably slow.
        # Candidate set = anyone with a logged event in the window UNION anyone
        # currently in a channel. The live union matters for a member who joined
        # >7d ago and has sat in-call ever since with no new event: they have no
        # row at >= since, so voice_user_ids_since alone would drop them even
        # though their 7-day in-call tail (accrued via the carry-in join) would
        # top the table — matching what /voice stats reports for them.
        candidates = set(db.voice_user_ids_since(gid, since))
        candidates.update(int(u) for u in live_by_uid)
        totals: list[dict] = []
        for uid in sorted(candidates):
            rows = db.voice_events_for(gid, uid, since=since, until=now)
            live = live_by_uid.get(str(uid))
            summary = summarize_voice(
                [(r["event"], r["at"]) for r in rows],
                since, now, now=now,
                live_in_call=live is not None,
                live_muted=bool(live and live.get("self_mute")),
                live_deafened=bool(live and live.get("self_deaf")),
            )
            if summary.in_call_seconds == 0 and not summary.in_call_now:
                continue  # only carry-in / a dropped phantom tail — nothing to show
            member = db.get_member(gid, uid)
            totals.append({
                "user_id": str(uid),
                "display_name": (
                    member["display_name"] if member and member["display_name"]
                    else str(uid)
                ),
                "avatar": member["avatar"] if member else None,
                "in_call": summary.in_call_seconds,
                "active": summary.active_seconds,
                "muted": summary.muted_seconds,
                "deafened": summary.deafened_seconds,
            })
        totals.sort(key=lambda t: t["in_call"], reverse=True)

        return web.json_response(
            {"occupancy": occupancy, "events": events, "totals": totals}
        )

    async def api_blacklist_add(request: web.Request) -> web.Response:
        _require(request)
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="invalid json")
        try:
            gid = int(body["guild"])
            uid = int(body["user_id"])
        except (KeyError, ValueError, TypeError):
            raise web.HTTPBadRequest(text="guild and user_id required")
        reason = str(body.get("reason", "")).strip() or None
        actor = _actor(request)
        db.message_blacklist_add(gid, uid, reason, actor)
        member = db.get_member(gid, uid)
        db.add_audit(
            gid, "member", "message_blacklist_add", actor_name=actor,
            subject_id=uid,
            subject_name=member["display_name"] if member else None,
            detail=reason or "no reason given",
        )
        # Announce publicly (ping + reason) when wired to a live bot.
        announced, announce_error = False, None
        if announce_blacklist is not None:
            result = await announce_blacklist(gid, uid, reason, actor)
            announced = bool(result.get("ok"))
            announce_error = result.get("error")
        return web.json_response(
            {"ok": True, "announced": announced, "error": announce_error}
        )

    async def api_blacklist_remove(request: web.Request) -> web.Response:
        _require(request)
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="invalid json")
        try:
            gid = int(body["guild"])
            uid = int(body["user_id"])
        except (KeyError, ValueError, TypeError):
            raise web.HTTPBadRequest(text="guild and user_id required")
        actor = _actor(request)
        removed = db.message_blacklist_remove(gid, uid)
        if removed:
            member = db.get_member(gid, uid)
            db.add_audit(
                gid, "member", "message_blacklist_remove", actor_name=actor,
                subject_id=uid,
                subject_name=member["display_name"] if member else None,
            )
        return web.json_response({"ok": removed})

    # ---- JSON API: edits (audited) --------------------------------------

    async def api_lift_delete(request: web.Request) -> web.Response:
        _require(request)
        gid, body = await _edit_ctx(request)
        ok = db.web_delete_lift(gid, int(body["id"]), _actor(request))
        return web.json_response({"ok": ok})

    async def api_lift_edit(request: web.Request) -> web.Response:
        _require(request)
        gid, body = await _edit_ctx(request)
        reps = body.get("reps")
        ok = db.web_update_lift(
            gid, int(body["id"]),
            weight_kg=float(body["weight_kg"]),
            reps=int(reps) if reps not in (None, "", "null") else None,
            equipment=str(body["equipment"]).strip(),
            actor_name=_actor(request),
        )
        return web.json_response({"ok": ok})

    async def api_calorie_delete(request: web.Request) -> web.Response:
        _require(request)
        gid, body = await _edit_ctx(request)
        ok = db.web_delete_calorie(gid, int(body["id"]), _actor(request))
        return web.json_response({"ok": ok})

    async def api_protein_delete(request: web.Request) -> web.Response:
        _require(request)
        gid, body = await _edit_ctx(request)
        ok = db.web_delete_protein(gid, int(body["id"]), _actor(request))
        return web.json_response({"ok": ok})

    async def api_bodyweight_delete(request: web.Request) -> web.Response:
        """Remove one weigh-in, and the body metrics measured with it.

        Needed because a smart scale can log a reading nobody wants kept: a
        half-finished measurement, or one it assigned to the wrong profile. A
        stray weight is not cosmetic -- it moves TDEE, the bodyweight-linked
        protein target and every true-load line on the leaderboard."""
        _require(request)
        gid, body = await _edit_ctx(request)
        ok = db.web_delete_bodyweight(gid, int(body["id"]), _actor(request))
        return web.json_response({"ok": ok})

    async def api_food_set(request: web.Request) -> web.Response:
        _require(request)
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="invalid json")
        try:
            gid = int(body["guild"])
            uid = int(body["user"])
            display = str(body["display"]).strip()
            kcal = float(body["kcal"])
        except (KeyError, ValueError, TypeError):
            raise web.HTTPBadRequest(text="guild, user, display, kcal required")
        if not display:
            raise web.HTTPBadRequest(text="display name required")
        # Normalize the lookup key the same way the bot does for chat shortcuts.
        from . import calories as _cal
        name = _cal.normalize_food(display)
        if not name:
            raise web.HTTPBadRequest(text="invalid food name")
        protein_raw = body.get("protein_g")
        protein_g = None
        if protein_raw not in (None, "", "null"):
            try:
                protein_g = float(protein_raw)
            except (ValueError, TypeError):
                raise web.HTTPBadRequest(text="invalid protein")
        member = db.get_member(gid, uid)
        username = member["display_name"] if member else str(uid)
        db.web_food_set(
            gid, uid, username, name=name, display=display,
            kcal=kcal, protein_g=protein_g, actor_name=_actor(request),
        )
        return web.json_response({"ok": True})

    async def api_food_alias_set(request: web.Request) -> web.Response:
        """Point an alternate name at an existing saved food."""
        _require(request)
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="invalid json")
        try:
            gid = int(body["guild"])
            uid = int(body["user"])
            name = str(body["name"]).strip()
            alias_raw = str(body["alias"]).strip()
        except (KeyError, ValueError, TypeError):
            raise web.HTTPBadRequest(text="guild, user, name, alias required")
        from . import calories as _cal
        alias = _cal.normalize_food(alias_raw)
        name = _cal.normalize_food(name)
        if not alias:
            raise web.HTTPBadRequest(text="invalid alias")
        if db.calorie_food_get(gid, uid, name) is None:
            raise web.HTTPBadRequest(text="no such saved food")
        if alias == name:
            raise web.HTTPBadRequest(text="alias matches the food's own name")
        # A shortcut can only mean one thing, and a saved food outranks an
        # alias in lookup order — so an alias that shadows another food would
        # simply never fire.
        clash = db.calorie_food_get(gid, uid, alias)
        if clash is not None and clash["name"] == alias:
            raise web.HTTPBadRequest(
                text=f"'{alias}' is already a saved food"
            )
        db.calorie_food_alias_set(uid, alias, name)
        return web.json_response({"ok": True, "alias": alias, "name": name})

    async def api_food_alias_delete(request: web.Request) -> web.Response:
        _require(request)
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="invalid json")
        try:
            uid = int(body["user"])
            alias = str(body["alias"]).strip()
        except (KeyError, ValueError, TypeError):
            raise web.HTTPBadRequest(text="user, alias required")
        from . import calories as _cal
        removed = db.calorie_food_alias_remove(uid, _cal.normalize_food(alias))
        return web.json_response({"ok": True, "removed": removed})

    async def api_nutrition_targets(request: web.Request) -> web.Response:
        _require(request)
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="invalid json")
        try:
            gid = int(body["guild"])
            uid = int(body["user"])
        except (KeyError, ValueError, TypeError):
            raise web.HTTPBadRequest(text="guild and user required")

        def _optional(key: str, limit: float) -> float | None:
            raw = body.get(key)
            if raw in (None, "", "null"):
                return None
            try:
                value = float(raw)
            except (ValueError, TypeError):
                raise web.HTTPBadRequest(text=f"invalid {key}")
            if not 0 < value <= limit:
                raise web.HTTPBadRequest(text=f"{key} out of range")
            return value

        kcal = _optional("calorie_weekday", _MAX_TARGET_KCAL)
        weekend_kcal = _optional("calorie_weekend", _MAX_TARGET_KCAL)
        protein_g = _optional("protein_weekday", _MAX_TARGET_PROTEIN_G)
        weekend_protein_g = _optional("protein_weekend", _MAX_TARGET_PROTEIN_G)
        # A weekend override with no weekday target underneath it would leave
        # Mon-Fri untracked while Sat/Sun kept a goal — almost certainly a
        # half-filled form rather than what anyone meant.
        if weekend_kcal is not None and kcal is None:
            raise web.HTTPBadRequest(text="weekday calorie target required")
        if weekend_protein_g is not None and protein_g is None:
            raise web.HTTPBadRequest(text="weekday protein target required")

        member = db.get_member(gid, uid)
        username = member["display_name"] if member else str(uid)
        db.web_nutrition_targets_set(
            gid, uid, username, kcal=kcal, weekend_kcal=weekend_kcal,
            protein_g=protein_g, weekend_protein_g=weekend_protein_g,
            actor_name=_actor(request),
        )
        return web.json_response({"ok": True})

    async def api_food_delete(request: web.Request) -> web.Response:
        _require(request)
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="invalid json")
        try:
            gid = int(body["guild"])
            uid = int(body["user"])
            name = str(body["name"])
        except (KeyError, ValueError, TypeError):
            raise web.HTTPBadRequest(text="guild, user, name required")
        member = db.get_member(gid, uid)
        username = member["display_name"] if member else str(uid)
        ok = db.web_food_delete(gid, uid, username, name, _actor(request))
        return web.json_response({"ok": ok})

    async def api_resync(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        if resync is None:
            return web.json_response(
                {"ok": False, "error": "resync unavailable"}, status=503,
            )
        ok = await resync(gid)
        return web.json_response({"ok": bool(ok)})

    async def api_channels(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        if list_channels is None:
            return web.json_response(
                {"channels": [], "error": "unavailable"}, status=503,
            )
        chans = await list_channels(gid)
        return web.json_response({"channels": chans})

    async def api_invite(request: web.Request) -> web.Response:
        _require(request)
        if invite_user is None:
            return web.json_response(
                {"ok": False, "error": "invites unavailable"}, status=503,
            )
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="invalid json")
        try:
            gid = int(body["guild"])
            uid = int(body["user_id"])
        except (KeyError, ValueError, TypeError):
            raise web.HTTPBadRequest(text="guild and user_id required")
        channel_id = None
        raw_ch = body.get("channel_id")
        if raw_ch not in (None, "", "null"):
            try:
                channel_id = int(raw_ch)
            except (ValueError, TypeError):
                raise web.HTTPBadRequest(text="invalid channel_id")
        # Note: invites deliberately accept user IDs that are NOT members of the
        # guild (that's the point) — and we never query their stored info here,
        # which keeps the cross-server privacy rule intact.
        result = await invite_user(gid, uid, channel_id, _actor(request))
        status = 200 if result.get("ok") else 400
        return web.json_response(result, status=status)

    async def api_member_role(request: web.Request) -> web.Response:
        _require(request)
        if set_member_role is None:
            return web.json_response(
                {"ok": False, "error": "role editing unavailable"}, status=503,
            )
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="invalid json")
        try:
            gid = int(body["guild"])
            uid = int(body["user"])
            rid = int(body["role_id"])
        except (KeyError, ValueError, TypeError):
            raise web.HTTPBadRequest(text="guild, user, role_id required")
        action = str(body.get("action", "add")).lower()
        if action not in ("add", "remove"):
            raise web.HTTPBadRequest(text="action must be add or remove")
        result = await set_member_role(
            gid, uid, rid, action == "add", _actor(request),
        )
        status = 200 if result.get("ok") else 400
        return web.json_response(result, status=status)

    async def api_member_moderation(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        if member_moderation is None:
            return web.json_response(
                {"ok": False, "error": "moderation unavailable"}, status=503,
            )
        try:
            uid = int(request.query["user"])
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text="missing or invalid ?user")
        result = await member_moderation(gid, uid)
        status = 200 if result.get("ok") else 400
        return web.json_response(result, status=status)

    async def api_member_untimeout(request: web.Request) -> web.Response:
        _require(request)
        if remove_timeout is None:
            return web.json_response(
                {"ok": False, "error": "timeout removal unavailable"}, status=503,
            )
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="invalid json")
        try:
            gid = int(body["guild"])
            uid = int(body["user"])
        except (KeyError, ValueError, TypeError):
            raise web.HTTPBadRequest(text="guild and user required")
        result = await remove_timeout(gid, uid, _actor(request))
        status = 200 if result.get("ok") else 400
        return web.json_response(result, status=status)

    async def api_member_autountimeout(request: web.Request) -> web.Response:
        _require(request)
        if set_auto_untimeout is None:
            return web.json_response(
                {"ok": False, "error": "auto un-timeout unavailable"},
                status=503,
            )
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="invalid json")
        try:
            gid = int(body["guild"])
            uid = int(body["user"])
        except (KeyError, ValueError, TypeError):
            raise web.HTTPBadRequest(text="guild and user required")
        enable = bool(body.get("enable", True))
        result = await set_auto_untimeout(gid, uid, enable, _actor(request))
        status = 200 if result.get("ok") else 400
        return web.json_response(result, status=status)

    async def api_member_track(request: web.Request) -> web.Response:
        _require(request)
        if presence_track is None:
            return web.json_response(
                {"ok": False, "error": "presence tracking unavailable"},
                status=503,
            )
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="invalid json")
        try:
            gid = int(body["guild"])
            uid = int(body["user"])
        except (KeyError, ValueError, TypeError):
            raise web.HTTPBadRequest(text="guild and user required")
        action = str(body.get("action", "start")).lower()
        if action not in ("start", "stop"):
            raise web.HTTPBadRequest(text="action must be start or stop")
        result = await presence_track(
            gid, uid, action == "start", _actor(request),
        )
        status = 200 if result.get("ok") else 400
        return web.json_response(result, status=status)

    async def _edit_ctx(request: web.Request) -> tuple[int, dict]:
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="invalid json")
        try:
            gid = int(body["guild"])
        except (KeyError, ValueError, TypeError):
            raise web.HTTPBadRequest(text="missing guild")
        if "id" not in body:
            raise web.HTTPBadRequest(text="missing id")
        return gid, body

    async def logo(_request: web.Request) -> web.Response:
        # Unauthenticated so it works as the favicon on the login page too.
        return web.Response(
            text=LOGO_SVG, content_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    async def health(request: web.Request) -> web.Response:
        """Liveness, deliberately NOT "is the Discord bot connected".

        The supervisor staying up while the bot is down is the normal, intended
        state during setup and reconfiguration, and it is exactly when the
        container must keep running so the operator can fix it. Alerting that
        wants the stricter test can ask for ``?require_worker=1``.
        """
        if request.query.get("require_worker") in ("1", "true", "yes"):
            state = supervisor.status()["state"] if supervisor is not None else None
            if state != "running":
                return web.Response(
                    text=f"bot not running (state={state})", status=503,
                )
        return web.Response(text="ok")

    async def media(request: web.Request) -> web.StreamResponse:
        """Serve a downloaded message attachment from ``media_dir``.

        Authenticated (logged-in operators only) and confined to ``media_dir``
        — the requested path is resolved and rejected if it escapes the root, so
        a crafted ``../`` can't read arbitrary files. Long-cached because each
        file is content-addressed by its immutable Discord attachment id."""
        _require(request)
        root = _media_dir()
        if not root:
            raise web.HTTPNotFound(text="media storage disabled")
        rel = request.match_info.get("path", "")
        base = os.path.abspath(root)
        full = os.path.abspath(os.path.join(base, rel))
        if full != base and not full.startswith(base + os.sep):
            raise web.HTTPForbidden(text="bad path")
        if not os.path.isfile(full):
            raise web.HTTPNotFound(text="not found")
        return web.FileResponse(
            full, headers={"Cache-Control": "private, max-age=31536000"},
        )

    @web.middleware
    async def _security_headers(request: web.Request, handler):
        resp = await handler(request)
        # Defensive headers: block clickjacking + MIME sniffing, trim referrer
        # leakage. The dashboard is same-origin and uses no external embeds.
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.path != "/logo.svg" and not request.path.startswith("/media/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @web.middleware
    async def _worker_guard(request: web.Request, handler):
        """Turn "the bot process isn't up" into an honest 503.

        Every live-Discord endpoint already 503s when its handler was never
        injected, so this just extends the same contract to "injected, but the
        bot is restarting right now" instead of surfacing a connection error.
        """
        try:
            return await handler(request)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ != "WorkerDown":
                raise
            return web.json_response(
                {
                    "ok": False,
                    "worker_down": True,
                    "error": "The Discord bot isn't running right now. "
                             "Check the Settings tab for why.",
                },
                status=503,
            )

    app = web.Application(middlewares=[_security_headers, _worker_guard])
    app.add_routes([
        web.get("/login", login_get),
        web.post("/login", login_post),
        web.post("/logout", logout_post),
        # Unauthenticated by necessity — nothing exists to authenticate against
        # until someone claims the instance. Both 302 to /login once claimed.
        web.get("/setup", setup_get),
        web.post("/setup", setup_post),
        web.get("/api/settings", api_settings),
        web.post("/api/settings", api_settings_set),
        web.post("/api/settings/apply", api_settings_apply),
        web.post("/api/settings/revert", api_settings_revert),
        web.get("/api/settings/export", api_settings_export),
        web.get("/api/settings/timezones", api_timezones),
        web.get("/api/worker", api_worker),
        web.post("/api/worker", api_worker),
        web.post("/api/password", api_password),
        web.get("/logo.svg", logo),
        web.get("/", index),
        # One route per tab, plus the member deep link, so these are real URLs
        # that survive a refresh and can be shared. Registered explicitly
        # rather than as a catch-all so they can never shadow /api/*, /login,
        # /setup, /media/* or /healthz.
        *(web.get(f"/{slug}", index) for slug, _icon in DASHBOARD_TABS),
        web.get("/members/{user_id}", index),
        web.get("/api/guilds", api_guilds),
        web.get("/api/overview", api_overview),
        web.get("/api/members", api_members),
        web.get("/api/member", api_member),
        web.get("/api/roles", api_roles),
        web.get("/api/role", api_role),
        web.get("/api/audit", api_audit),
        web.get("/api/lifts", api_lifts),
        web.get("/api/calories", api_calories),
        web.get("/api/protein", api_protein),
        web.get("/api/foods", api_foods),
        web.get("/api/equipment", api_equipment),
        web.get("/api/leaderboard", api_leaderboard),
        web.get("/api/activity", api_activity),
        web.get("/api/activity/log", api_activity_log),
        web.get("/api/sleep", api_sleep),
        web.get("/api/messages/channels", api_messages_channels),
        web.get("/api/messages/log", api_messages_log),
        web.get("/api/voice", api_voice),
        web.post("/api/blacklist/add", api_blacklist_add),
        web.post("/api/blacklist/remove", api_blacklist_remove),
        web.post("/api/lifts/delete", api_lift_delete),
        web.post("/api/lifts/edit", api_lift_edit),
        web.post("/api/calories/delete", api_calorie_delete),
        web.post("/api/protein/delete", api_protein_delete),
        web.post("/api/bodyweight/delete", api_bodyweight_delete),
        web.post("/api/foods/set", api_food_set),
        web.post("/api/foods/delete", api_food_delete),
        web.post("/api/foods/alias/set", api_food_alias_set),
        web.post("/api/foods/alias/delete", api_food_alias_delete),
        web.post("/api/nutrition/targets", api_nutrition_targets),
        web.post("/api/resync", api_resync),
        web.get("/api/channels", api_channels),
        web.post("/api/invite", api_invite),
        web.post("/api/member/role", api_member_role),
        web.get("/api/member/moderation", api_member_moderation),
        web.post("/api/member/untimeout", api_member_untimeout),
        web.post("/api/member/autountimeout", api_member_autountimeout),
        web.post("/api/member/track", api_member_track),
        web.get("/media/{path:.*}", media),
        web.get("/healthz", health),
    ])
    return app


async def start_server(
    app: web.Application, host: str, port: int
) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    LOG.info("Dashboard web server listening on %s:%d", host, port)
    return runner


# ---- serialization helpers -------------------------------------------------
# JS can't hold 64-bit Discord snowflakes precisely, so every id crosses the
# wire as a string.

def _stringify_ids(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, int) and ("id" in k.lower()):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def _member_dict(r) -> dict:
    return {
        "user_id": str(r["user_id"]),
        "username": r["username"],
        "display_name": r["display_name"],
        "avatar": r["avatar"] if "avatar" in r.keys() else None,
        "is_bot": bool(r["is_bot"]),
        "present": bool(r["present"]),
        "joined_at": r["joined_at"],
    }


def _role_dict(r) -> dict:
    keys = r.keys()
    out = {
        "role_id": str(r["role_id"]),
        "name": r["name"],
        "color": r["color"],
    }
    if "position" in keys:
        out["position"] = r["position"]
    if "members" in keys:
        out["members"] = r["members"]
    if "managed" in keys:
        out["managed"] = bool(r["managed"])
    return out


def _food_dict(r, aliases: dict[str, list[str]] | None = None) -> dict:
    return {
        "name": r["name"],
        "display": r["display"],
        "kcal": r["kcal"],
        "protein_g": r["protein_g"],
        "aliases": sorted((aliases or {}).get(r["name"], [])),
    }


def _alias_map(db, user_id: int) -> dict[str, list[str]]:
    """``{food name: [alias, ...]}`` for one user, in a single query."""
    out: dict[str, list[str]] = {}
    for row in db.calorie_food_aliases(user_id):
        out.setdefault(row["name"], []).append(row["alias"])
    return out


def _targets_dict(db, user_id: int) -> dict:
    """The four editable target fields, plus which set is live right now.

    Weekend values are null when the user runs one target all week, which is
    exactly what the settings form shows as an empty "same as weekday" box.
    Both bands are resolved against the next day of their kind, so a rule that
    only starts applying tomorrow still shows up in the form today.
    """
    rows = db.nutrition_target_rows(user_id)
    today = targets.local_today()
    days = [today + timedelta(days=n) for n in range(7)]
    weekday = targets.resolve(rows, next(d for d in days if not targets.is_weekend(d)))
    weekend = targets.resolve(rows, next(d for d in days if targets.is_weekend(d)))
    live = targets.resolve(rows, today)
    return {
        "calorie_weekday": weekday.kcal.value,
        "calorie_weekend": weekend.kcal.value if weekend.kcal.split else None,
        "protein_weekday": weekday.protein.value,
        "protein_weekend": (
            weekend.protein.value if weekend.protein.split else None
        ),
        "split": live.split,
        "is_weekend": live.is_weekend,
        "label": live.label,
    }


def _audit_dict(r) -> dict:
    keys = r.keys()
    return {
        "id": r["id"],
        "at": r["at"],
        "category": r["category"],
        "action": r["action"],
        "actor_id": str(r["actor_id"]) if r["actor_id"] else None,
        "actor_name": r["actor_name"],
        "subject_id": str(r["subject_id"]) if r["subject_id"] else None,
        "subject_name": r["subject_name"],
        "subject_avatar": r["subject_avatar"] if "subject_avatar" in keys else None,
        "detail": r["detail"],
    }


def _opt_user(request: web.Request) -> int | None:
    val = request.query.get("user")
    if val and val.isdigit():
        return int(val)
    return None


def _clamp_int(val, default: int, lo: int, hi: int) -> int:
    try:
        n = int(val)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


# ---- static assets ---------------------------------------------------------

# Served at /logo.svg and reused as the favicon and header mark. A gradient
# dumbbell on a rounded dark tile.
LOGO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#818cf8"/>
      <stop offset="1" stop-color="#22d3ee"/>
    </linearGradient>
  </defs>
  <rect width="64" height="64" rx="15" fill="#0d1117"/>
  <g fill="url(#g)">
    <rect x="21" y="29" width="22" height="6" rx="3"/>
    <rect x="9"  y="21" width="8" height="22" rx="3.5"/>
    <rect x="17" y="25" width="5" height="14" rx="2.5"/>
    <rect x="47" y="21" width="8" height="22" rx="3.5"/>
    <rect x="42" y="25" width="5" height="14" rx="2.5"/>
  </g>
</svg>"""


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gym Dashboard — Sign in</title>
<link rel="icon" type="image/svg+xml" href="/logo.svg">
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{font-family:'Inter',system-ui,-apple-system,sans-serif;margin:0;min-height:100dvh;
display:flex;align-items:center;justify-content:center;color:#e6edf3;
background:radial-gradient(1200px 600px at 50% -10%,#1b2540 0%,#0b0e14 55%)}
.card{background:rgba(22,27,34,.7);backdrop-filter:blur(12px);
border:1px solid rgba(255,255,255,.08);border-radius:18px;
padding:2.5rem 2.25rem;width:min(340px,calc(100vw - 2rem));
box-shadow:0 24px 60px rgba(0,0,0,.5)}
.brand{display:flex;align-items:center;gap:.7rem;margin-bottom:1.75rem}
.brand img{width:44px;height:44px}
.brand b{font-size:1.25rem;background:linear-gradient(90deg,#a5b4fc,#67e8f9);
-webkit-background-clip:text;background-clip:text;color:transparent}
label{font-size:.78rem;color:#8b949e;text-transform:uppercase;letter-spacing:.05em}
input{width:100%;padding:.7rem .8rem;margin:.4rem 0 1.1rem;font-size:1rem;
background:#0d1117;border:1px solid #30363d;border-radius:10px;color:#e6edf3}
input:focus{outline:none;border-color:#6366f1;box-shadow:0 0 0 3px #6366f133}
.remember{display:flex;align-items:flex-start;gap:.65rem;margin:-.15rem 0 1.1rem;
font-size:.82rem;line-height:1.3;color:#c9d1d9;text-transform:none;letter-spacing:0}
.remember input{width:auto;margin:.05rem 0 0;accent-color:#818cf8;flex:0 0 auto}
.remember small{display:block;margin-top:.15rem;color:#6e7681;font-size:.72rem}
button{width:100%;padding:.75rem;border:0;border-radius:10px;font-size:1rem;
font-weight:600;cursor:pointer;color:#fff;
background:linear-gradient(90deg,#6366f1,#22d3ee)}
button:hover{filter:brightness(1.08)}
button:focus-visible,input:focus-visible{outline:2px solid #67e8f9;outline-offset:2px}
.err{color:#f85149;margin:0 0 .75rem;font-size:.88rem}
.sub{color:#6e7681;font-size:.78rem;margin-top:1.25rem;text-align:center}
</style></head><body>
<form class="card" method="post" action="/login">
<div class="brand"><img src="/logo.svg" alt=""><b>Gym Dashboard</b></div>
<label for="password">Password</label>
<input id="password" type="password" name="password" autofocus autocomplete="current-password" required>
<label class="remember"><input type="checkbox" name="remember" value="1">
<span>Keep me signed in for 30 days<small>Use only on a trusted device</small></span></label>
<!--ERR-->
<button type="submit">Sign in</button>
<p class="sub">Operator access only</p>
</form></body></html>"""


# Shown once, on a brand-new install, until someone sets a password. Reuses
# LOGIN_HTML's stylesheet so the two pages are visually identical.
SETUP_HTML = LOGIN_HTML.replace(
    "<title>Gym Dashboard — Sign in</title>",
    "<title>Gym Dashboard — Set up</title>",
).replace(
    """<form class="card" method="post" action="/login">
<div class="brand"><img src="/logo.svg" alt=""><b>Gym Dashboard</b></div>
<label for="password">Password</label>
<input id="password" type="password" name="password" autofocus autocomplete="current-password" required>
<label class="remember"><input type="checkbox" name="remember" value="1">
<span>Keep me signed in for 30 days<small>Use only on a trusted device</small></span></label>
<!--ERR-->
<button type="submit">Sign in</button>
<p class="sub">Operator access only</p>
</form></body></html>""",
    """<form class="card" method="post" action="/setup">
<div class="brand"><img src="/logo.svg" alt=""><b>Gym Dashboard</b></div>
<p class="sub" style="margin:0 0 1.25rem;text-align:left;color:#8b949e">
Choose a password to secure this dashboard. You'll use it every time you sign
in.</p>
<label for="password">New password</label>
<input id="password" type="password" name="password" autofocus autocomplete="new-password"
 minlength="12" required>
<label for="password2">Confirm password</label>
<input id="password2" type="password" name="password2" autocomplete="new-password"
 minlength="12" required>
<label class="remember"><input type="checkbox" name="remember" value="1">
<span>Keep me signed in for 30 days<small>Use only on a trusted device</small></span></label>
<!--ERR-->
<button type="submit">Set password</button>
<p class="sub">At least 12 characters. Until this is set, anyone who can reach
this page can claim the bot — keep the port off the public internet.</p>
</form></body></html>""",
)


DASHBOARD_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gym Dashboard</title>
<link rel="icon" type="image/svg+xml" href="/logo.svg">
<style>
:root{
  color-scheme:dark;
  --bg:#0b0e14; --panel:#161b22; --panel2:#1c222c; --line:#262d38;
  --text:#e6edf3; --muted:#8b949e; --faint:#6e7681;
  --indigo:#818cf8; --cyan:#22d3ee; --accent:linear-gradient(90deg,#6366f1,#22d3ee);
  --header-h:112px;
}
*{box-sizing:border-box}
body{font-family:'Inter',system-ui,-apple-system,sans-serif;margin:0;color:var(--text);
background:radial-gradient(1100px 520px at 100% -5%,#172036 0%,rgba(11,14,20,0) 60%),
          radial-gradient(900px 500px at -5% 0%,#16242b 0%,rgba(11,14,20,0) 55%),var(--bg);
min-height:100vh;line-height:1.45}
a{color:inherit}
button,input,select{font:inherit}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,
summary:focus-visible{outline:2px solid var(--cyan);outline-offset:2px}
.sr-only{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;
overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.skip-link{position:fixed;left:1rem;top:.5rem;z-index:100;transform:translateY(-160%);
background:#fff;color:#0b0e14;border-radius:8px;padding:.45rem .75rem;font-weight:700;
text-decoration:none;transition:transform .15s}
.skip-link:focus{transform:none}
::-webkit-scrollbar{height:10px;width:10px}
::-webkit-scrollbar-thumb{background:#2a313c;border-radius:6px}

/* header */
.chrome{position:sticky;top:0;z-index:20;background:rgba(13,17,23,.82);
backdrop-filter:blur(14px);box-shadow:0 8px 24px #0003}
header{display:flex;align-items:center;gap:.85rem;padding:.7rem 1.4rem;
border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:.6rem}
.brand img{width:30px;height:30px}
.brand b{font-size:1.05rem;background:var(--accent);-webkit-background-clip:text;
background-clip:text;color:transparent;letter-spacing:.2px}
header .sp{flex:1}
.gselect{position:relative}
select,.btn{font:inherit;color:var(--text);background:var(--panel);
border:1px solid var(--line);border-radius:10px;padding:.45rem .7rem;cursor:pointer}
select:hover,.btn:hover{border-color:#3a4350;background:var(--panel2)}
.btn{display:inline-flex;align-items:center;gap:.4rem}
.btn:disabled{opacity:.55;cursor:not-allowed}
.btn.primary{background:var(--accent);border:0;color:#fff;font-weight:600}
.btn.primary:hover{filter:brightness(1.08)}
.btn.danger{color:#ff9a96;border-color:#5c2b2b}
.btn.danger:hover{background:#3a1d1d}
.link.danger{color:#ff9a96}
td.right{text-align:right}
.bwlist{margin-top:10px}
.bwlist summary{cursor:pointer;font-size:13px}
.bwlist table{margin-top:6px;font-size:13px}
form.inline{margin:0}

/* nav */
nav{display:flex;gap:.3rem;padding:.55rem 1.4rem;flex-wrap:nowrap;overflow-x:auto;
overscroll-behavior-x:contain;scrollbar-width:thin;border-bottom:1px solid var(--line);
background:rgba(13,17,23,.35)}
/* text-decoration:none is load-bearing: these carry real href attributes (so
   middle-click opens a new tab), which means the browser's default link
   underline applies unless it is turned off here. */
nav a{display:flex;align-items:center;gap:.4rem;padding:.4rem .85rem;border-radius:9px;
cursor:pointer;color:var(--muted);font-size:.92rem;font-weight:500;transition:.15s;
text-decoration:none;white-space:nowrap;flex:none}
nav a:hover{color:var(--text);background:#ffffff0a;text-decoration:none}
nav a.active{color:#fff;background:linear-gradient(90deg,#6366f133,#22d3ee22);
box-shadow:inset 0 0 0 1px #6366f155}

main{padding:1.5rem;max-width:1240px;margin:0 auto;min-height:calc(100vh - var(--header-h))}
main:focus{outline:none}
h1{font-size:1.45rem;margin:0;font-weight:700;letter-spacing:-.02em}
h2{font-size:1.15rem;margin:.2rem 0 1.1rem;font-weight:650}
.muted{color:var(--muted)}.faint{color:var(--faint)}
.page-head{display:flex;align-items:flex-end;justify-content:space-between;gap:1rem;
margin:.1rem 0 1.25rem}
.page-sub{color:var(--muted);font-size:.84rem;margin-top:.2rem}

/* stat cards */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
gap:1rem;margin-bottom:1.6rem}
.stat{position:relative;background:linear-gradient(180deg,#1a212c,#141a22);
border:1px solid var(--line);border-radius:14px;padding:1.1rem 1.2rem;overflow:hidden}
.stat::before{content:"";position:absolute;inset:0 auto auto 0;width:100%;height:3px;
background:var(--accent);opacity:.85}
.stat .n{font-size:1.85rem;font-weight:750;line-height:1.1;letter-spacing:-.5px}
.stat .l{color:var(--muted);font-size:.74rem;text-transform:uppercase;
letter-spacing:.06em;margin-top:.25rem}
/* overview live tiles */
.ov-live{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));
gap:1rem;margin:-.4rem 0 1.6rem}
.ov-tile{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:.7rem .9rem}
.ov-wide{grid-column:1/-1}
.ov-k{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:.35rem}
.ov-v{font-size:1.15rem;font-weight:650}
.ov-spark{display:flex;align-items:flex-end;gap:3px;height:30px}
.ovbar{flex:1;min-width:3px;background:linear-gradient(180deg,#6366f1,#22d3ee);border-radius:2px 2px 0 0}
.ov-games{display:flex;flex-wrap:wrap;gap:.4rem}
.ov-game{display:inline-flex;align-items:center;gap:.35rem;background:#ffffff08;
border:1px solid var(--line);border-radius:999px;padding:.2rem .55rem;font-size:.82rem}
.ov-game .ovgi{width:18px;height:18px;border-radius:5px;flex:none}

/* table card */
.tcard{background:var(--panel);border:1px solid var(--line);border-radius:14px;
overflow-x:auto;overscroll-behavior-x:contain}
.tcard>table{min-width:620px}
table{width:100%;border-collapse:collapse;font-size:.9rem}
thead th{position:sticky;top:0;background:#1a212c;color:var(--muted);
font-weight:600;font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;
text-align:left;padding:.7rem .9rem;border-bottom:1px solid var(--line)}
tbody td{padding:.6rem .9rem;border-bottom:1px solid #1e242e;vertical-align:middle}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:#ffffff05}

/* avatars + identity */
.av{border-radius:50%;object-fit:cover;background:#222;flex:none;
box-shadow:0 0 0 1px #ffffff14}
.av-fallback{display:inline-flex;align-items:center;justify-content:center;
color:#fff;font-weight:600;font-size:.8em}
.who{display:inline-flex;align-items:center;gap:.6rem;min-width:0}
.who a{font-weight:550;text-decoration:none}
.who a:hover{color:var(--indigo)}

.pill{display:inline-flex;align-items:center;gap:.35rem;padding:.16rem .6rem;
border-radius:999px;font-size:.78rem;border:1px solid var(--line);margin:2px 1px;
background:#ffffff08}
.pill .dot{width:8px;height:8px;border-radius:50%}
.modrow{display:flex;align-items:center;flex-wrap:wrap;gap:.5rem;margin:.35rem 0}
.pill a.rmrole{cursor:pointer;color:var(--faint);margin-left:.15rem;font-size:.8em}
.pill a.rmrole:hover{color:#f85149}
.rolectl{display:flex;gap:.5rem;align-items:center;margin-top:.7rem}
.rolectl select{max-width:280px}
.tag{padding:.12rem .5rem;border-radius:6px;font-size:.72rem;font-weight:600}
.link{color:var(--indigo);cursor:pointer;text-decoration:none}
.link:hover{text-decoration:underline}
.cat-role{color:#d2a8ff}.cat-member{color:#7ee787}.cat-data{color:#79c0ff}
.row-actions{display:flex;gap:.4rem;justify-content:flex-end}
.btn.sm{padding:.28rem .55rem;font-size:.82rem;border-radius:8px}

.filters{display:flex;gap:.5rem;align-items:center;margin-bottom:1.1rem;flex-wrap:wrap}
.seg{display:inline-flex;background:var(--panel);border:1px solid var(--line);
border-radius:10px;overflow:hidden}
.seg button{background:transparent;border:0;color:var(--muted);padding:.4rem .8rem;
cursor:pointer;font:inherit}
.seg button.on{background:linear-gradient(90deg,#6366f133,#22d3ee22);color:#fff}
.empty{color:var(--muted);padding:2.5rem;text-align:center}

/* member hero */
.hero{display:flex;align-items:center;gap:1.1rem;margin:.4rem 0 1.4rem}
.hero .av{box-shadow:0 0 0 3px #0b0e14,0 0 0 5px #6366f1aa}
.hero h2{margin:0;font-size:1.5rem}
.crumb{display:inline-flex;align-items:center;gap:.35rem;color:var(--muted);
cursor:pointer;margin-bottom:.6rem;font-size:.88rem}
.crumb:hover{color:var(--text)}
.chips{display:flex;flex-wrap:wrap;gap:.3rem;margin:.3rem 0}

dialog{background:var(--panel);color:var(--text);border:1px solid var(--line);
border-radius:16px;padding:1.5rem;width:min(620px,calc(100vw - 2rem));max-height:calc(100vh - 2rem);
overflow:auto;box-shadow:0 30px 80px #000a}
dialog::backdrop{background:#0009;backdrop-filter:blur(2px)}
dialog h2{margin-top:0}
dialog label{display:block;font-size:.74rem;color:var(--muted);
text-transform:uppercase;letter-spacing:.05em;margin:.7rem 0 .25rem}
dialog input{width:100%;padding:.6rem .7rem;background:#0d1117;border:1px solid var(--line);
border-radius:9px;color:var(--text);font:inherit}
.dlg-actions{display:flex;gap:.6rem;justify-content:flex-end;margin-top:1.4rem}

/* settings — form controls outside a dialog need their own rules */
.setform{display:grid;gap:1.1rem}
.setrow{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.15fr);
gap:.5rem 1.2rem;align-items:start;padding:.85rem 0;border-top:1px solid var(--line)}
.setrow:first-child{border-top:0}
.setrow .lbl{font-weight:550;display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}
.setrow .hlp{grid-column:1;color:var(--muted);font-size:.82rem;margin-top:.25rem}
.setrow .ctl{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
.setrow input[type=text],.setrow input[type=password],.setrow select{
flex:1 1 12rem;min-width:0;padding:.55rem .7rem;background:#0d1117;
border:1px solid var(--line);border-radius:9px;color:var(--text);font:inherit}
.setrow input:focus,.setrow select:focus{outline:none;border-color:var(--indigo);
box-shadow:0 0 0 3px #6366f133}
.setrow input:disabled,.setrow select:disabled{opacity:.55;cursor:not-allowed}
.setnote{grid-column:1/-1;font-size:.82rem;margin:.35rem 0 0}
.setnote.warn{color:#e3b341}
.pin{background:#e3b34118;border-color:#e3b34155;color:#e3b341}
.wlog{background:#0d1117;border:1px solid var(--line);border-radius:10px;
padding:.7rem .9rem;overflow:auto;max-height:22rem;font-size:.8rem;
white-space:pre-wrap;word-break:break-word;margin:.8rem 0 0}
.wstat{display:flex;align-items:center;gap:.6rem;font-weight:550}
.wdot{width:10px;height:10px;border-radius:50%;flex:none}
.wdot.ok{background:#3fb950}.wdot.warn{background:#e3b341}
.wdot.bad{background:#f85149}.wdot.idle{background:#6e7681}

.toast{position:fixed;bottom:1.4rem;right:1.4rem;padding:.7rem 1.1rem;border-radius:11px;
background:var(--panel2);border:1px solid var(--line);box-shadow:0 12px 30px #0007;
opacity:0;transform:translateY(8px);transition:.25s;pointer-events:none;z-index:50;
max-width:min(420px,calc(100vw - 2rem))}
.toast.show{opacity:1;transform:none}
.toast.error{border-color:#f8514966;color:#ffb4b0}
.spin{display:inline-block;width:34px;height:34px;border:3px solid #2a313c;
border-top-color:var(--indigo);border-radius:50%;animation:sp 1s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.center{display:flex;align-items:center;justify-content:center;gap:.7rem;padding:3rem;
color:var(--muted);font-size:.84rem}
.state-card{max-width:620px;margin:2.5rem auto;padding:1.4rem;text-align:center;
background:var(--panel);border:1px solid var(--line);border-radius:14px}
.state-card .state-icon{font-size:1.6rem;margin-bottom:.4rem}
.state-card h2{margin:0 0 .35rem}
.state-card p{color:var(--muted);margin:.2rem 0 1rem;overflow-wrap:anywhere}
.state-card p:last-child{margin-bottom:0}

/* search */
.search{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:.45rem .7rem .45rem 2rem;color:var(--text);font:inherit;min-width:220px;
background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' fill='none' stroke='%238b949e' stroke-width='2'%3E%3Ccircle cx='6' cy='6' r='4.5'/%3E%3Cpath d='M10 10l3 3'/%3E%3C/svg%3E");
background-repeat:no-repeat;background-position:.6rem center}
.search:focus{outline:none;border-color:#6366f1}

/* weekday/weekend target summary under the nutrition bars */
.tgt{margin-top:.9rem;border-top:1px solid var(--line);padding-top:.6rem}
.tgt-row{display:flex;justify-content:space-between;align-items:center;gap:.6rem;
font-size:.82rem;padding:.22rem 0;color:var(--muted)}
.tgt-row.tgt-live{color:var(--text);font-weight:600}
/* progress bars (nutrition + goals) */
.pgoal{margin:.5rem 0}
.pgrow{display:flex;justify-content:space-between;font-size:.85rem;margin-bottom:.3rem}
.ptrack{height:8px;background:#0d1117;border-radius:6px;overflow:hidden;
border:1px solid var(--line)}
.pfill{height:100%;border-radius:6px;transition:width .4s}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1.1rem;margin-bottom:1.4rem}
.box{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:1.1rem 1.2rem}
.box h3{margin:.1rem 0 .8rem;font-size:.8rem;text-transform:uppercase;
letter-spacing:.05em;color:var(--muted)}
.kv{display:flex;justify-content:space-between;padding:.3rem 0;
border-bottom:1px solid #1e242e;font-size:.9rem}
.kv:last-child{border-bottom:0}

/* sparkline */
.spark{width:100%;height:64px;display:block}
.spark path.line{fill:none;stroke:url(#grad);stroke-width:2}
.spark path.area{fill:url(#fade);opacity:.25}
.bodycomp{margin-bottom:1.4rem}
.bm-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.75rem}
.bm-card{min-width:0;background:#0d111780;border:1px solid var(--line);
border-radius:11px;padding:.75rem .8rem}
.bm-head{display:flex;align-items:center;gap:.4rem;color:var(--muted);
font-size:.76rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bm-value{margin-top:.35rem;font-size:1.35rem;font-weight:700;
font-variant-numeric:tabular-nums}
.bm-unit{font-size:.78rem;color:var(--muted);font-weight:500}
.bm-meta{display:flex;justify-content:space-between;align-items:center;gap:.4rem;
min-height:1.2rem;margin-top:.1rem;color:var(--muted);font-size:.75rem}
.bm-delta{color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap}
.bm-spark{display:block;width:100%;height:38px;margin-top:.35rem}
.bm-spark .bm-line{fill:none;stroke:var(--muted);stroke-width:1.7;
vector-effect:non-scaling-stroke;stroke-linecap:round;stroke-linejoin:round}
.bm-one{height:38px;display:flex;align-items:center;color:var(--muted);
font-size:.75rem}
.bm-caveat{margin:.8rem 0 0;color:var(--muted);font-size:.76rem;line-height:1.45}

/* medals on leaderboard */
.rank{display:inline-flex;width:24px;justify-content:center;font-weight:700}

/* activity feed */
.act-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1.1rem}
.act-card{background:var(--panel);border:1px solid var(--line);border-radius:16px;
padding:1.1rem;display:flex;flex-direction:column;gap:.9rem;position:relative;overflow:hidden}
.act-head{display:flex;align-items:center;gap:.7rem}
.act-av{position:relative;flex:none}
.act-av .dot{position:absolute;right:-2px;bottom:-2px;width:14px;height:14px;
border-radius:50%;border:3px solid var(--panel)}
.st-online{background:#3ba55d}.st-idle{background:#faa61a}.st-dnd{background:#ed4245}
.st-offline{background:#747f8d}
.act-name{font-weight:600}
.act-sub{font-size:.78rem;color:var(--muted);text-transform:capitalize}
.now{display:flex;align-items:center;gap:.7rem;background:#ffffff06;
border:1px solid var(--line);border-radius:12px;padding:.6rem}
.game-img{width:48px;height:48px;border-radius:10px;object-fit:cover;flex:none;
box-shadow:0 0 0 1px #ffffff14}
.game-tile{display:flex;align-items:center;justify-content:center;color:#fff;
font-weight:700;font-size:1.1rem;text-shadow:0 1px 2px #0006}
.now .meta{min-width:0}
.now .g{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.now .t{font-size:.74rem;color:var(--muted)}
.top-games{display:flex;flex-direction:column;gap:.5rem}
.tg{display:flex;align-items:center;gap:.6rem}
.tg .gi{width:30px;height:30px;border-radius:7px;flex:none}
.tg .nm{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:.88rem}
.tg .pt{font-size:.78rem;color:var(--muted);font-variant-numeric:tabular-nums}
.tg .barwrap,.lead-nm .barwrap{height:5px;background:#0d1117;border-radius:4px;overflow:hidden;margin-top:3px}
.tg .barfill,.lead-nm .barfill{height:100%;background:linear-gradient(90deg,#6366f1,#22d3ee)}
.offline-card{border-color:#747f8d55}
.offline-card .now{opacity:.72}
.nowlist{display:flex;flex-direction:column;gap:.4rem}
.act-presence{display:grid;grid-template-columns:repeat(3,1fr);gap:.45rem}
.act-metric{min-width:0;background:#0d111780;border:1px solid var(--line);
border-radius:9px;padding:.48rem .55rem}
.act-metric span{display:block;color:var(--muted);font-size:.66rem;text-transform:uppercase;
letter-spacing:.04em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.act-metric b{display:block;margin-top:.12rem;font-size:.88rem;
font-variant-numeric:tabular-nums}
.act-metric.low b{color:#faa61a}
.act-note{margin-top:-.45rem;color:#faa61a;font-size:.72rem;line-height:1.35}
.act-summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:.7rem;
margin-bottom:1.1rem}
.act-stat{display:flex;align-items:baseline;gap:.45rem;background:var(--panel);
border:1px solid var(--line);border-radius:11px;padding:.65rem .8rem}
.act-stat b{font-size:1.15rem;font-variant-numeric:tabular-nums}
.act-stat span{font-size:.76rem;color:var(--muted)}
/* per-player session log (in the shared dialog) */
.act-head .act-log-btn{margin-left:auto;flex:none}
.tg-go{cursor:pointer;border-radius:8px;padding:.15rem .3rem;margin:-.15rem -.3rem}
.tg-go:hover{background:#ffffff0a}
.tg-go.on{background:#ffffff12}
#editDlg .al-sec{font-size:.78rem;color:var(--muted);text-transform:uppercase;
letter-spacing:.04em;margin:1.1rem 0 .5rem;display:flex;align-items:center;gap:.5rem}
/* The shared dialog sizes to its content; pin a width here so a long game
   name can't stretch the panel across the screen. */
#editDlg .al-list,#editDlg .al-games{width:min(560px,86vw)}
.al-games{display:flex;flex-direction:column;gap:.45rem;max-height:26vh;overflow:auto}
.al-games .al-meta{flex:none;font-size:.74rem}
.al-list{display:flex;flex-direction:column;gap:.15rem;max-height:44vh;overflow:auto}
.al-day{position:sticky;top:0;background:var(--panel);z-index:1;font-size:.76rem;
color:var(--muted);padding:.5rem 0 .25rem}
.al-row{display:flex;align-items:center;gap:.55rem;padding:.28rem .3rem;border-radius:8px}
.al-row:hover{background:#ffffff08}
.al-row[data-open]{background:#3ba55d14}
.al-row .gi{width:24px;height:24px;border-radius:6px;flex:none}
.al-nm{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:.86rem}
.al-when{flex:none;font-size:.82rem;color:var(--muted);font-variant-numeric:tabular-nums}
.al-dur{flex:none;width:64px;text-align:right;font-size:.82rem;font-weight:600;
font-variant-numeric:tabular-nums}
.al-next{font-size:.68rem;color:var(--muted);margin-left:.2rem}
.al-now{color:#3ba55d;font-weight:600}
.al-clear{cursor:pointer;background:#ffffff10;border:1px solid var(--line);
border-radius:7px;padding:.1rem .4rem;font-size:.74rem;text-transform:none;letter-spacing:0}
.al-clear:hover{background:#ffffff1a}
/* day-window segmented control (Activity + Sleep tabs) */
.dayseg{display:inline-flex;border:1px solid var(--line);border-radius:9px;overflow:hidden}
.dayseg button{background:var(--panel);border:0;color:var(--muted);cursor:pointer;
font:inherit;font-size:.84rem;padding:.32rem .7rem}
.dayseg button:hover{background:#ffffff0a;color:#e6edf3}
.dayseg button.on{background:var(--accent);color:#fff;font-weight:600}
/* sleep feed */
.slplist{display:flex;flex-direction:column;gap:.45rem}
.slprow{display:flex;align-items:center;gap:.6rem}
.slot{position:relative;flex:1;min-width:0;height:14px;background:#0d1117;
border:1px solid var(--line);border-radius:7px;overflow:hidden}
.slotbar{position:absolute;top:0;bottom:0;border-radius:6px;
background:linear-gradient(90deg,#6366f1,#8b5cf6)}
.slpmeta{flex:none;width:118px;line-height:1.15}
.slpd{font-size:.82rem;font-variant-numeric:tabular-nums}
.slpt{font-size:.72rem}
.slph{flex:none;width:62px;text-align:right;font-size:.82rem;font-weight:600;
font-variant-numeric:tabular-nums}
.schips{display:flex;flex-wrap:wrap;gap:.35rem}
.schip{background:#ffffff08;border:1px solid var(--line);border-radius:7px;
padding:.18rem .45rem;font-size:.74rem;color:var(--muted)}
.schip b{color:#e6edf3;font-variant-numeric:tabular-nums}
.sspark{display:flex;align-items:flex-end;gap:2px;height:34px;margin:.2rem 0 .1rem}
.sbar{flex:1;min-width:2px;border-radius:2px 2px 0 0;background:#6366f1}
.sbar.low{background:#ed4245}.sbar.mid{background:#faa61a}.sbar.ok{background:#3ba55d}
/* live indicator + server game leaderboard */
.live-dot{width:8px;height:8px;border-radius:50%;background:#3ba55d;flex:none;
box-shadow:0 0 0 0 #3ba55d99;animation:livePulse 2s infinite}
@keyframes livePulse{0%{box-shadow:0 0 0 0 #3ba55d77}70%{box-shadow:0 0 0 6px #3ba55d00}100%{box-shadow:0 0 0 0 #3ba55d00}}
.lead-card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
padding:.9rem 1.1rem;margin-bottom:1.1rem}
.lead-h{font-weight:600;margin-bottom:.7rem;font-size:.92rem}
.lead-list{display:flex;flex-direction:column;gap:.5rem}
.lead-row{display:flex;align-items:center;gap:.6rem}
.lead-rank{flex:none;width:18px;text-align:center;color:var(--muted);font-size:.82rem;font-variant-numeric:tabular-nums}
.lead-row .gi{width:26px;height:26px;border-radius:6px;flex:none}
.lead-nm{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:.88rem}
.lead-who{flex:1.2;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:.78rem}
.lead-row .pt{flex:none;font-size:.8rem;color:var(--muted);font-variant-numeric:tabular-nums}
/* messages — Discord-style channel browser */
.dc{display:flex;height:calc(100vh - 200px);min-height:420px;border:1px solid var(--line);
border-radius:14px;overflow:hidden;background:var(--panel)}
.dc-side{width:240px;flex:none;background:#0d1117;border-right:1px solid var(--line);
display:flex;flex-direction:column}
.dc-side-h{display:flex;align-items:center;justify-content:space-between;gap:.5rem;
padding:.6rem .7rem;font-size:.74rem;text-transform:uppercase;letter-spacing:.04em;
color:var(--muted);border-bottom:1px solid var(--line)}
.dc-chans{flex:1;overflow-y:auto;padding:.4rem}
.dc-chan{display:flex;align-items:center;gap:.4rem;width:100%;text-align:left;
background:transparent;border:0;color:var(--muted);border-radius:7px;
padding:.4rem .5rem;cursor:pointer;font-size:.9rem}
.dc-chan:hover{background:#ffffff0a;color:#e6edf3}
.dc-chan.active{background:#ffffff14;color:#fff}
.dc-hash{color:#6b7280;font-weight:700}
.dc-cn{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dc-cc{font-size:.72rem;color:var(--muted);font-variant-numeric:tabular-nums}
.dc-main{flex:1;min-width:0;display:flex;flex-direction:column}
.dc-main-h{padding:.7rem .9rem;border-bottom:1px solid var(--line);font-weight:600;
display:flex;align-items:center;gap:.25rem}
.dc-chat{flex:1;overflow-y:auto;padding:1rem .9rem;display:flex;flex-direction:column;gap:1.1rem}
.dc-grp{display:flex;gap:.7rem;align-items:flex-start}
.dc-grp .av{flex:none;border-radius:50%}
.dc-gb{min-width:0;flex:1}
.dc-gh{display:flex;align-items:baseline;gap:.5rem;margin-bottom:.1rem}
.dc-au{font-weight:600;font-size:.92rem;color:#fff}
.dc-ts{font-size:.7rem;color:var(--muted)}
.dc-bl{margin-left:auto;background:transparent;border:0;cursor:pointer;opacity:0;
font-size:.8rem;line-height:1;padding:.1rem .35rem;border-radius:5px;transition:opacity .12s}
.dc-grp:hover .dc-bl{opacity:.5}
.dc-bl:hover{opacity:1;background:#ffffff12}
.dc-msg{font-size:.9rem;line-height:1.4;white-space:pre-wrap;word-break:break-word;
padding:.06rem .3rem;margin:0 -.3rem;border-radius:4px;color:#dbe1e8}
.dc-msg:hover{background:#ffffff08}
.mention{background:rgba(88,101,242,.3);color:#c9d1ff;border-radius:4px;padding:0 2px;font-weight:500}
.dc-media{display:flex;flex-wrap:wrap;gap:.4rem;margin:.25rem 0}
.dc-att{max-width:260px;max-height:260px;border-radius:8px;background:#0d1117;
border:1px solid var(--line);object-fit:cover;cursor:pointer}
video.dc-att{cursor:default}
.dc-file{display:inline-flex;align-items:center;gap:.3rem;padding:.4rem .6rem;
border-radius:8px;background:#0d1117;border:1px solid var(--line);
color:#c9d1ff;text-decoration:none;font-size:.85rem}
.dc-file:hover{background:#161b22}
.dc-line.dc-del{opacity:.6}
.dc-tag{font-size:.72rem;color:var(--muted)}
.dc-delm{display:inline-block;margin:.15rem 0;color:#f0883e}
.btn.sm{padding:.25rem .55rem;font-size:.8rem}
.bl-list{display:flex;flex-direction:column;gap:.5rem;max-height:240px;overflow-y:auto;margin-bottom:.8rem}
.bl-row{display:flex;align-items:center;justify-content:space-between;gap:.7rem;
background:#ffffff06;border:1px solid var(--line);border-radius:9px;padding:.5rem .6rem}
.bl-form{display:flex;gap:.5rem;flex-wrap:wrap}
.bl-form .search{flex:1;min-width:120px}
/* voice tab */
.vc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:1rem}
.vc-chan{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:.8rem .9rem}
.vc-chan-h{font-weight:600;margin-bottom:.6rem}
.vc-members{display:flex;flex-direction:column;gap:.45rem}
.vc-mem{display:flex;align-items:center;gap:.5rem;font-size:.9rem}
.vc-mem .av{flex:none}
.vc-ic{font-size:.8rem}
.vc-log{display:flex;flex-direction:column;gap:.3rem}
.vc-ev{display:flex;align-items:center;gap:.6rem;padding:.4rem .6rem;border:1px solid var(--line);
background:var(--panel);border-radius:9px;font-size:.86rem}
.vc-ev .av{flex:none}
.vc-ev-who{font-weight:600;min-width:110px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vc-ev-act{flex:1;color:var(--muted)}
.vc-ch{color:#7aa2f7}
.vc-ev-ts{font-size:.72rem;color:var(--muted);font-variant-numeric:tabular-nums;flex:none}

@media (max-width:720px){
  :root{--header-h:105px}
  header{padding:.6rem .75rem;gap:.5rem}
  .brand b{display:none}
  .gselect{flex:1;min-width:0}
  .gselect select{width:100%;min-width:0}
  header .sp{display:none}
  .header-action .btn-label{display:none}
  .header-action{padding:.45rem .58rem}
  nav{padding:.45rem .65rem}
  nav a{padding:.4rem .68rem;font-size:.86rem}
  main{padding:1rem .75rem 4rem}
  .cards{grid-template-columns:repeat(2,minmax(0,1fr));gap:.7rem}
  .stat{padding:.9rem}
  .stat .n{font-size:1.5rem}
  .filters{align-items:stretch}
  .filters h2{flex:1;align-self:center}
  .filters .sp{display:none}
  .filters .search{flex:1 1 100%;width:100%;min-width:0}
  .setrow{grid-template-columns:1fr;gap:.6rem}
  .setrow .ctl{grid-column:1}
  .page-head{align-items:flex-start;flex-direction:column}
  .hero{align-items:flex-start}
  .act-grid{grid-template-columns:1fr}
  .lead-who{display:none}
  .dc{height:auto;min-height:0;flex-direction:column}
  .dc-side{width:100%;max-height:210px;border-right:0;border-bottom:1px solid var(--line)}
  .dc-main{height:60vh;min-height:380px}
  .dc-att{max-width:min(260px,100%)}
  .vc-ev{flex-wrap:wrap}
  .vc-ev-who{min-width:0}
  .vc-ev-ts{width:100%;padding-left:30px}
  .toast{right:1rem;bottom:1rem}
}
@media (max-width:430px){
  .cards{grid-template-columns:1fr 1fr}
  .ov-live,.grid2{grid-template-columns:1fr}
  .act-presence{grid-template-columns:repeat(3,minmax(0,1fr))}
  .act-metric{padding:.42rem}
  .act-metric span{font-size:.6rem}
  .row-actions{flex-wrap:wrap}
  .pgrow{gap:.5rem;align-items:flex-start}
  dialog{padding:1rem}
}
@media (hover:none){
  .dc-bl{opacity:.55}
}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{scroll-behavior:auto!important;animation-duration:.01ms!important;
  animation-iteration-count:1!important;transition-duration:.01ms!important}
}
</style></head><body>
<a class="skip-link" href="#view">Skip to content</a>
<div class="chrome">
<header>
  <div class="brand"><img src="/logo.svg" alt=""><b>Gym Dashboard</b></div>
  <div class="gselect"><label class="sr-only" for="guild">Discord server</label>
    <select id="guild" onchange="onGuild()" aria-label="Discord server"></select></div>
  <span class="sp"></span>
  <button class="btn header-action" id="syncBtn" onclick="resync()"
    title="Re-pull members and roles from Discord"><span aria-hidden="true">↻</span>
    <span class="btn-label">Sync</span></button>
  <form class="inline" method="post" action="/logout"><button class="btn header-action"
    title="Sign out"><span aria-hidden="true">↪</span><span class="btn-label">Logout</span></button></form>
</header>
<nav id="nav" aria-label="Dashboard sections"></nav>
</div>
<main id="view" tabindex="-1"><div class="center" role="status">
  <div class="spin" aria-hidden="true"></div><span>Loading dashboard…</span></div></main>
<dialog id="editDlg"></dialog>
<div class="toast" id="toast" role="status" aria-live="polite" aria-atomic="true"></div>
<script>
// Injected from DASHBOARD_TABS in app/webui.py so the nav and the server-side
// routes cannot drift apart — every slug here must have a matching GET route
// or a hard refresh on that URL would 404.
const TABS=/*TABS*/;
const PALETTE=["#6366f1","#22d3ee","#f59e0b","#ef4444","#10b981","#ec4899","#8b5cf6","#14b8a6"];
const ACTION_LABEL={
  role_add:"➕ role added",role_remove:"➖ role removed",role_create:"🆕 role created",
  role_delete:"🗑️ role deleted",role_rename:"✏️ role renamed",
  join:"📥 joined",leave:"📤 left",nick_change:"🏷️ nickname",username_change:"🏷️ username",
  kick:"👢 kicked",ban:"🔨 banned",unban:"♻️ unbanned",invite_create:"✉️ invite sent",
  timeout_remove:"⏳ timeout removed",
  timeout_auto_removed:"⏳ timeout auto-removed",timeout_auto_skip:"⚠️ timeout (couldn't auto-remove)",
  auto_untimeout_enable:"🛡️ auto un-timeout on",auto_untimeout_disable:"🛡️ auto un-timeout off",
  message_blacklist_add:"🚫 msg blacklisted",message_blacklist_remove:"♻️ msg unblacklisted",
  lift_add:"🏋️ lift logged",lift_undo:"↩️ lift undone",lift_delete:"🗑️ lift deleted",lift_edit:"✏️ lift edited",
  calorie_add:"🔥 calories logged",calorie_undo:"↩️ calories undone",calorie_delete:"🗑️ calories deleted",calorie_edit:"✏️ calories edited",
  protein_add:"🥩 protein logged",protein_undo:"↩️ protein undone",protein_delete:"🗑️ protein deleted",protein_edit:"✏️ protein edited",
  food_set:"🍴 food saved",food_delete:"🗑️ food removed",
  goal_set:"🎯 goal set",goal_remove:"🎯 goal removed",
  calorie_goal_set:"🎯 calorie goal",calorie_goal_remove:"🛑 calorie tracking off",
  nutrition_targets_set:"🎯 nutrition targets",
  protein_goal_set:"🎯 protein goal",protein_goal_remove:"🛑 protein tracking off",
  presence_track_start:"🎮 tracking started",presence_track_stop:"🛑 tracking stopped",
  bodyweight_log:"⚖️ bodyweight"};
function actionLabel(a){return ACTION_LABEL[a]||esc(a);}
let guild=null,tab="overview",AV={},dataUserFilter=null,auditCat="",currentMember=null,lbEquip="",currentFoods=[],currentNutrition=null,auditOffset=0,auditRows=[],ALL_ROLES=[];
// Current search term, persisted across live re-renders so typing isn't lost.
let SEARCH="";

function searchBar(ph){
  const label=ph||"Search";
  return `<input class="search" type="search" aria-label="${esc(label)}"
    placeholder="${esc(label)}" value="${esc(SEARCH)}" oninput="filterTable(this.value)">`;
}
// Filters both data tables and card grids: table rows by text, cards by their
// explicit data-search key (so we match the player/game name, not icon alt-text).
function filterTable(term){SEARCH=term=(term||"").toLowerCase();
  document.querySelectorAll("#view tbody tr").forEach(tr=>{
    tr.style.display=tr.textContent.toLowerCase().includes(term)?"":"none";});
  document.querySelectorAll("#view [data-search]").forEach(el=>{
    el.style.display=el.dataset.search.includes(term)?"":"none";});}

// Live auto-refresh: the Activity and Voice tabs re-poll on a timer and patch
// just their grid so the view stays current without a manual refresh.
let LIVE=null;
function clearLive(){if(LIVE){clearInterval(LIVE);LIVE=null;}}
function setLive(fn){clearLive();LIVE=setInterval(fn,15000);}
function pct(v,g){return Math.max(0,Math.min(100,g?v/g*100:0));}
function bar(label,val,goal,unit,warnOver){
  val=val||0;
  if(!goal)return `<div class="pgoal"><div class="pgrow"><span>${label}</span>
    <span><b>${Math.round(val)}</b>${unit} <span class="faint">· no goal set</span></span></div></div>`;
  const over=val>goal;
  const col=over?(warnOver?"#f85149":"#f0a500"):"linear-gradient(90deg,#6366f1,#22d3ee)";
  return `<div class="pgoal"><div class="pgrow"><span>${label}</span>
    <span><b>${Math.round(val)}</b> / ${Math.round(goal)}${unit}${over?(warnOver?' ⚠️ over':' ✓ over'):''}</span></div>
    <div class="ptrack"><div class="pfill" style="width:${pct(val,goal)}%;background:${col}"></div></div></div>`;
}
/* Which of the two target sets today falls under. Silent for anyone running a
   single all-week target — there'd be no other set to be using instead. */
function activeTargetPill(t){
  if(!t||!t.split)return "";
  return `<span class="pill">${t.is_weekend?"🌤️":"📅"} ${esc(t.label||"")}</span>`;
}
function targetsTable(t,uid){
  t=t||{};
  const edit=`<a class="link" onclick="targetsDialog('${uid}')">edit</a>`;
  const fmt=(v,unit)=>v==null?'<span class="faint">—</span>':`${Math.round(v)}${unit}`;
  if(!t.split){
    const none=t.calorie_weekday==null&&t.protein_weekday==null;
    return `<div class="tgt"><div class="tgt-row"><span class="faint">Every day</span>
      <span>${none?'<span class="faint">no targets set</span>':
        `${fmt(t.calorie_weekday," kcal")} · ${fmt(t.protein_weekday," g")}`}</span>${edit}</div></div>`;
  }
  const row=(name,cal,pro,live)=>`<div class="tgt-row${live?" tgt-live":""}">
    <span>${name}${live?' <span class="faint">· today</span>':''}</span>
    <span>${fmt(cal," kcal")} · ${fmt(pro," g")}</span></div>`;
  return `<div class="tgt">
    ${row("Weekdays",t.calorie_weekday,t.protein_weekday,!t.is_weekend)}
    ${row("Weekends",t.calorie_weekend,t.protein_weekend,t.is_weekend)}
    <div class="tgt-row"><span></span><span></span>${edit}</div></div>`;
}
function targetsDialog(uid){
  const t=(currentNutrition&&currentNutrition.targets)||{};
  const dlg=document.getElementById("editDlg");
  const v=x=>x==null?"":Math.round(x);
  dlg.innerHTML=`<h2>Nutrition targets</h2>
    <p class="faint" style="margin:.2rem 0 .8rem">Leave a weekend box empty to use the
      weekday target all week. Clearing a weekday box turns that tracker off.
      Changes apply from today — past days keep the targets they had.</p>
    <label>Weekday calories (kcal)</label><input id="t_cw" type="number" value="${v(t.calorie_weekday)}">
    <label>Weekend calories (kcal)</label><input id="t_ce" type="number" value="${v(t.calorie_weekend)}" placeholder="same as weekday">
    <label>Weekday protein (g)</label><input id="t_pw" type="number" value="${v(t.protein_weekday)}">
    <label>Weekend protein (g)</label><input id="t_pe" type="number" value="${v(t.protein_weekend)}" placeholder="same as weekday">
    <div class="dlg-actions"><button class="btn" onclick="editDlg.close()">Cancel</button>
    <button class="btn primary" onclick="targetsSave('${uid}')">Save</button></div>`;
  dlg.showModal();
}
async function targetsSave(uid){
  const g=id=>{const s=document.getElementById(id).value.trim();return s===""?null:s;};
  const body={guild,user:uid,calorie_weekday:g("t_cw"),calorie_weekend:g("t_ce"),
    protein_weekday:g("t_pw"),protein_weekend:g("t_pe")};
  if(body.calorie_weekend!=null&&body.calorie_weekday==null){
    toast("Set a weekday calorie target first");return;}
  if(body.protein_weekend!=null&&body.protein_weekday==null){
    toast("Set a weekday protein target first");return;}
  const r=await post("/api/nutrition/targets",body);
  document.getElementById("editDlg").close();
  toast(r&&r.ok?"Saved ✓":"Failed");memberView(uid);
}
function sparkline(pts){
  if(!pts||pts.length<2)return '<div class="faint">Not enough data for a trend.</div>';
  const ys=pts.map(p=>p.weight_kg),mn=Math.min(...ys),mx=Math.max(...ys),rng=(mx-mn)||1;
  const W=600,H=64,pad=4;
  const xs=(i)=>pad+i*(W-2*pad)/(pts.length-1);
  const yy=(v)=>H-pad-((v-mn)/rng)*(H-2*pad);
  const line=pts.map((p,i)=>`${i?'L':'M'}${xs(i).toFixed(1)},${yy(p.weight_kg).toFixed(1)}`).join(" ");
  const area=`M${pad},${H} `+pts.map((p,i)=>`L${xs(i).toFixed(1)},${yy(p.weight_kg).toFixed(1)}`).join(" ")+` L${W-pad},${H} Z`;
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <defs><linearGradient id="grad" x1="0" x2="1"><stop offset="0" stop-color="#6366f1"/><stop offset="1" stop-color="#22d3ee"/></linearGradient>
    <linearGradient id="fade" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#22d3ee"/><stop offset="1" stop-color="#22d3ee" stop-opacity="0"/></linearGradient></defs>
    <path class="area" d="${area}"/><path class="line" d="${line}"/></svg>
    <div class="pgrow faint"><span>${mn.toFixed(1)} kg</span><span>latest ${ys[ys.length-1].toFixed(1)} kg</span><span>${mx.toFixed(1)} kg</span></div>`;
}

function bodyMetricSparkline(points,label){
  const samples=(points||[]).map(p=>({
    value:Number(p.value),at:Date.parse(p.at)
  })).filter(p=>Number.isFinite(p.value));
  if(samples.length<2)return '<div class="bm-one">One reading on file</div>';
  const ys=samples.map(p=>p.value),times=samples.map(p=>p.at);
  const W=240,H=38,pad=3,mn=Math.min(...ys),mx=Math.max(...ys),rng=(mx-mn)||1;
  const timed=times.every(Number.isFinite)&&Math.max(...times)>Math.min(...times);
  const firstTime=timed?Math.min(...times):0,timeRange=timed?Math.max(...times)-firstTime:1;
  // Time-proportional spacing keeps a cluster of same-morning readings close
  // together instead of giving it the same visual weight as a multi-day gap.
  const x=(p,i)=>timed
    ?pad+(p.at-firstTime)*(W-2*pad)/timeRange
    :pad+i*(W-2*pad)/(samples.length-1);
  const y=v=>H-pad-((v-mn)/rng)*(H-2*pad);
  const coords=samples.map((p,i)=>`${x(p,i).toFixed(1)},${y(p.value).toFixed(1)}`).join(" ");
  return `<svg class="bm-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
    role="img" aria-label="${esc(label)} trend"><polyline class="bm-line" points="${coords}"/></svg>`;
}

function bodyMetricCards(metrics){
  if(!metrics||!metrics.length)return "";
  const cards=metrics.map(m=>{
    const raw=(m.points||[]).filter(p=>Number.isFinite(Number(p.value)));
    if(!raw.length)return "";
    const newest=raw[raw.length-1],unit=newest.unit||m.unit||"";
    // A historical unit change must not produce a meaningless cross-unit delta
    // or line. Values are normally normalized at import; this is a legacy guard.
    const points=raw.filter(p=>(p.unit||m.unit||"")===unit);
    const last=points[points.length-1],prev=points.length>1?points[points.length-2]:null;
    const precision=Math.max(0,Math.min(3,Number(m.precision)||0));
    const value=Number(last.value);
    let delta="";
    if(prev){
      const rounded=Number((value-Number(prev.value)).toFixed(precision));
      const sign=rounded>0?"+":rounded<0?"-":"";
      delta=`<span class="bm-delta">&Delta; ${sign}${Math.abs(rounded).toFixed(precision)}${unit?` ${esc(unit)}`:""} vs previous</span>`;
    }
    return `<article class="bm-card">
      <div class="bm-head"><span aria-hidden="true">${esc(m.emoji||"")}</span>
        <span title="${esc(m.label)}">${esc(m.label)}</span></div>
      <div class="bm-value">${value.toFixed(precision)}${unit?` <span class="bm-unit">${esc(unit)}</span>`:""}</div>
      <div class="bm-meta"><span>${esc(fmtTs(last.at))}</span>${delta}</div>
      ${bodyMetricSparkline(points,m.label||m.key||"Body composition")}</article>`;
  }).filter(Boolean);
  if(!cards.length)return "";
  return `<div class="box bodycomp"><h3>Body composition</h3>
    <div class="bm-grid">${cards.join("")}</div>
    <p class="bm-caveat">Consumer smart-scale body-composition values are estimates
      and can shift with hydration, meals, exercise, and time of day. Compare
      longer-term trends under consistent conditions; these are not medical measurements.</p></div>`;
}

// Newest-first, because a reading you want gone is almost always a recent one —
// a scale that mis-assigned a measurement or logged a half-finished one. A stray
// weight is not cosmetic: it moves TDEE, the bodyweight-linked protein target and
// every true-load line on the leaderboard, so it needs to be removable here
// rather than by hand-editing the database.
function bwList(pts,uid){
  if(!pts||!pts.length)return '';
  const rows=pts.slice().reverse().slice(0,12);
  return `<details class="bwlist"><summary class="link">Recent weigh-ins (${pts.length})</summary>
    <table><tbody>${rows.map(p=>`<tr>
      <td><b>${Number(p.weight_kg).toFixed(2)}</b> kg</td>
      <td class="muted">${esc(fmtTs(p.at))}</td>
      <td class="right">${p.id?`<a class="link danger" onclick="delBodyweight('${uid}',${p.id},'${Number(p.weight_kg).toFixed(2)}')">delete</a>`:''}</td>
    </tr>`).join("")}</tbody></table></details>`;
}

async function delBodyweight(uid,id,kg){
  if(!confirm(`Delete the ${kg} kg weigh-in?\n\nIt won't be re-imported, and any body-composition numbers measured with it go too.`))return;
  const r=await post("/api/bodyweight/delete",{guild,user:uid,id});
  if(r&&r.ok){toast("Weigh-in deleted");memberView(uid);}else{toast("Could not delete it");}
}

let TOAST_TIMER=null;
function toast(m,kind){
  const t=document.getElementById("toast");if(!t)return;
  t.textContent=m;t.classList.toggle("error",kind==="error");
  t.classList.add("show");clearTimeout(TOAST_TIMER);
  TOAST_TIMER=setTimeout(()=>t.classList.remove("show"),2600);
}
function esc(s){return(s==null?"":String(s)).replace(/[&<>"']/g,
  c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
// Drop a string into an inline on*="…" handler: JSON-quote it so the JS parser
// sees one string literal, then HTML-escape so it can't break out of the
// attribute. Plain esc() is not enough — it renders ' as &#39;, which the HTML
// parser hands back to JS as a bare quote, and game names really do contain
// apostrophes ("Tom Clancy's Rainbow Six Siege").
function jsq(s){return esc(JSON.stringify(s==null?"":String(s)));}
function fmtTs(s){if(!s)return"";const d=new Date(s);return isNaN(d)?s:
  d.toLocaleString([], {dateStyle:"medium",timeStyle:"short"});}
function roleColor(c){return c?("#"+(c>>>0).toString(16).padStart(6,"0").slice(-6)):"#8b949e";}
function idColor(id){let h=0;const s=String(id);for(let i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))>>>0;
  return PALETTE[h%PALETTE.length];}
function avatar(uid,name,url,size){size=size||30;const st=`width:${size}px;height:${size}px`;
  if(url)return `<img class="av" style="${st}" src="${esc(url)}" alt="" loading="lazy"
    onerror="this.replaceWith(Object.assign(document.createElement('span'),
    {className:'av av-fallback',style:'${st};background:${idColor(uid)}',textContent:'${esc((name||'?')[0]||'?').toUpperCase()}'}))">`;
  return `<span class="av av-fallback" style="${st};background:${idColor(uid)}">${esc((name||'?')[0]||'?').toUpperCase()}</span>`;}
function avFor(uid,name,size){const m=AV[uid];return avatar(uid,name||(m&&m.name),m&&m.avatar,size);}
function who(uid,name){return `<span class="who">${avFor(uid,name)}<a class="link" onclick="memberView('${uid}')">${esc(name)}</a></span>`;}

// Bounce to login carrying the current view, so a session that expires while
// you are on /members/123 returns you there instead of the overview.
function toLogin(){
  location.href="/login?next="+encodeURIComponent(location.pathname+location.search);
}
async function api(p){const r=await fetch(p);if(r.status===401){toLogin();return null;}
  if(!r.ok)throw new Error((await r.text())||`Request failed (${r.status})`);
  return r.json();}
async function post(p,b){
  let r;
  try{r=await fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(b)});}
  catch(e){return {ok:false,error:"Dashboard service is unreachable."};}
  if(r.status===401){toLogin();return null;}
  const text=await r.text();let data={};
  try{data=text?JSON.parse(text):{};}catch(e){data={error:text};}
  if(!r.ok)return {ok:false,error:data.error||text||`Request failed (${r.status})`};
  return data;
}
function spinner(){return '<div class="center" role="status"><div class="spin" aria-hidden="true"></div><span>Loading…</span></div>';}
function errorState(error,retry){
  const message=error&&error.message?error.message:String(error||"Unknown error");
  return `<div class="state-card" role="alert"><div class="state-icon" aria-hidden="true">⚠️</div>
    <h2>Couldn’t load this page</h2><p>${esc(message)}</p>
    <button class="btn" onclick="${retry||'render()'}">Try again</button></div>`;
}
function emptyState(icon,title,detail,actions){
  return `<div class="state-card"><div class="state-icon" aria-hidden="true">${icon||"—"}</div>
    <h2>${esc(title)}</h2>${detail?`<p>${esc(detail)}</p>`:""}${actions||""}</div>`;
}
function pageHead(title,subtitle,actions){
  return `<div class="page-head"><div><h1>${esc(title)}</h1>
    ${subtitle?`<div class="page-sub">${esc(subtitle)}</div>`:""}</div>${actions||""}</div>`;
}

/* ---- routing ------------------------------------------------------------
   Every view has a real URL: /overview, /members, /members/<id>. Back then
   returns to the previous tab instead of leaving the dashboard entirely, and
   any view can be bookmarked, refreshed or opened in a new tab.
   The guild rides along as ?guild=<id> so a shared link resolves to the same
   server rather than whichever one happens to sort first. */
const TAB_SLUGS=TABS.map(([t])=>t);
function tabLabel(t){return t?t[0].toUpperCase()+t.slice(1):"Dashboard";}

function tabPath(t,uid){
  const base=(t==="members"&&uid)?`/members/${encodeURIComponent(uid)}`:`/${t}`;
  return guild?`${base}?guild=${encodeURIComponent(guild)}`:base;
}
let inPop=false;   // true while a popstate is being applied
function pushPath(t,uid){
  // Never push while handling Back/Forward. The browser has already moved the
  // URL, and adding an entry here would leave Back pointing at the page you
  // just came from — a trap worse than the friction this routing removes.
  if(inPop)return;
  const p=tabPath(t,uid);
  // Idempotent otherwise: memberView() is also called to re-render after an
  // edit, and those must not stack duplicate history entries that Back would
  // then have to walk through one by one.
  if(location.pathname+location.search===p)return;
  history.pushState({t:t,uid:uid||null},"",p);
}
function parsePath(){
  const seg=location.pathname.split("/").filter(Boolean);
  const q=new URLSearchParams(location.search);
  const t=(seg[0]&&TAB_SLUGS.includes(seg[0]))?seg[0]:null;
  return {tab:t,uid:(t==="members"&&seg[1])?decodeURIComponent(seg[1]):null,
          guild:q.get("guild")};
}
function applyRoute(t,uid){
  tab=t;dataUserFilter=null;SEARCH="";currentMember=uid||null;
  document.title=`${uid?"Member":tabLabel(t)} · Gym Dashboard`;
  // memberView() bypasses render(), which is what normally stops the previous
  // tab's auto-refresh. Without this, going Back from Settings into a member
  // view leaves pollWorker hitting /api/worker every 15s for the life of the
  // page, on every tab.
  clearLive();
  renderNav();
  if(uid)return memberView(uid);
  render();
}
window.addEventListener("popstate",async()=>{
  const r=parsePath();
  const sel=document.getElementById("guild");
  const switched=r.guild&&r.guild!==guild;
  if(switched){
    guild=r.guild;if(sel)sel.value=guild;
    // The avatar and role caches are per-server. Going Back across a server
    // change without reloading them left the role dropdown listing the OTHER
    // server's roles — and granting one POSTed a role id that doesn't exist
    // in this guild, failing with a bare "Failed".
    await loadAvatars();await loadRoles();
  }
  inPop=true;
  // Safe to clear synchronously: memberView() calls pushPath() in its
  // synchronous prefix, before its first await, so the only push this route
  // can attempt has already been suppressed by the time applyRoute returns.
  try{applyRoute(r.tab||"overview",r.uid);}
  finally{inPop=false;}
});

function renderNav(){
  const nav=document.getElementById("nav");
  nav.innerHTML=TABS.map(([t,ic])=>`<a class="${t===tab?'active':''}"
    ${t===tab?'aria-current="page"':''} href="${tabPath(t)}"
    onclick="return navClick(event,'${t}')"><span aria-hidden="true">${ic}</span>
    <span>${tabLabel(t)}</span></a>`).join("");
  const active=nav.querySelector("a.active");
  if(active)requestAnimationFrame(()=>active.scrollIntoView({block:"nearest",inline:"nearest"}));
}
function navClick(e,t){
  // Real hrefs, so middle-click and ctrl-click open a new tab natively. Only
  // plain left-clicks are intercepted for client-side navigation.
  if(e.metaKey||e.ctrlKey||e.shiftKey||e.altKey||e.button!==0)return true;
  e.preventDefault();go(t);return false;
}
function go(t){
  pushPath(t);applyRoute(t,null);
  requestAnimationFrame(()=>document.getElementById("view").focus({preventScroll:true}));
}
async function onGuild(){guild=document.getElementById("guild").value;
  // A member id is scoped to one server, so it cannot survive a server switch —
  // keeping it would leave the address bar pointing at a member page while the
  // members LIST is on screen, and refreshing that URL would show an empty stub
  // for a member who isn't in this server.
  currentMember=null;
  // replaceState, not push: switching server is a change to the current view,
  // not a new place to go Back to.
  history.replaceState({t:tab,uid:null},"",tabPath(tab,null));
  renderNav();await loadAvatars();await loadRoles();render();}

async function loadAvatars(){AV={};try{const d=await api(`/api/members?guild=${guild}`);
  if(d)for(const m of d.members)AV[m.user_id]={avatar:m.avatar,name:m.display_name};}catch(e){}}
async function loadRoles(){try{const d=await api(`/api/roles?guild=${guild}`);
  ALL_ROLES=(d&&d.roles)||[];}catch(e){ALL_ROLES=[];}}

async function boot(){
  const route=parsePath();
  const g=await api("/api/guilds");if(!g)return;
  const sel=document.getElementById("guild");
  // A brand-new install has no guilds yet, and the whole point of the Settings
  // tab is to get from that state to a working bot. So render the nav FIRST and
  // land on Settings, instead of returning early with an empty shell that has
  // no way to reach anything.
  if(!g.guilds.length){
    tab="settings";renderNav();
    sel.innerHTML='<option>No server yet</option>';
    history.replaceState({t:"settings",uid:null},"","/settings");
    return render();
  }
  sel.innerHTML=g.guilds.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join("");
  // Honour ?guild= from the URL, but only if it is a server we actually have —
  // otherwise a stale bookmark would leave every panel empty with no clue why.
  const ids=g.guilds.map(x=>String(x.id));
  guild=(route.guild&&ids.includes(route.guild))?route.guild:String(g.guilds[0].id);
  sel.value=guild;
  tab=route.tab||"overview";
  document.title=`${route.uid?"Member":tabLabel(tab)} · Gym Dashboard`;
  currentMember=route.uid||null;
  renderNav();
  await loadAvatars();await loadRoles();
  // Normalise the address bar (adds ?guild=, turns "/" into "/overview") without
  // creating a history entry the user never navigated to.
  history.replaceState({t:tab,uid:currentMember},"",tabPath(tab,currentMember));
  if(currentMember)return memberView(currentMember);
  render();
}
async function resync(){
  if(!guild)return;
  const btn=document.getElementById("syncBtn");
  if(btn&&btn.disabled)return;
  if(btn){btn.disabled=true;btn.setAttribute("aria-busy","true");}
  toast("Syncing…");
  try{
    const r=await post("/api/resync",{guild});
    if(r&&r.ok){await loadAvatars();toast("Synced ✓");render();}
    else toast((r&&r.error)||"Sync unavailable","error");
  }finally{
    if(btn){btn.disabled=false;btn.removeAttribute("aria-busy");}
  }
}

async function render(){
  const v=document.getElementById("view");
  clearLive();  // stop any prior tab's auto-refresh before drawing the new one
  v.setAttribute("aria-busy","true");
  // Settings are global, so they must be reachable before any guild exists —
  // this check has to come BEFORE the "Pick a guild" bail-out below. It needs
  // its own try/catch too: it sits outside the one below, so an error here
  // (e.g. /api/settings 503 in a deployment built without a SettingsService)
  // would otherwise leave the tab spinning forever with nothing shown.
  if(tab==="settings"){
    v.innerHTML=spinner();
    try{return await renderSettings(v);}
    catch(e){v.innerHTML=errorState(e);return;}
    finally{v.removeAttribute("aria-busy");}
  }
  if(!guild){
    v.innerHTML='<div class="state-card"><h2>Pick a server</h2><p>Select a Discord server above to continue.</p></div>';
    v.removeAttribute("aria-busy");return;
  }
  v.innerHTML=spinner();
  try{
    if(tab==="overview")await renderOverview(v);
    else if(tab==="members")await renderMembers(v);
    else if(tab==="activity")await renderActivity(v);
    else if(tab==="sleep")await renderSleep(v);
    else if(tab==="messages")await renderMessages(v);
    else if(tab==="voice")await renderVoice(v);
    else if(tab==="roles")await renderRoles(v);
    else if(tab==="leaderboard")await renderLeaderboard(v);
    else if(tab==="audit")await renderAudit(v);
    else if(["lifts","calories","protein"].includes(tab))await renderData(v,tab);
  }catch(e){v.innerHTML=errorState(e);}
  finally{v.removeAttribute("aria-busy");}
}
/* ---- settings ---------------------------------------------------------- */
let SETTINGS=null;
const WDOT={running:"ok",starting:"warn",restarting:"warn",
  quarantined:"bad",not_configured:"idle",stopped:"idle",unknown:"idle"};

async function renderSettings(v){
  const d=await api("/api/settings");if(!d)return;
  SETTINGS=d;
  v.innerHTML=pageHead("Settings","Bot configuration, service health and dashboard security")
    +workerCard(d.worker)+cryptoCard(d.crypto)+pendingCard(d)
    +d.groups.map(groupBox).join("")+accountBox()+historyBox(d.history);
  // Keep the bot-status card honest without hammering the API.
  setLive(pollWorker);
}

function workerCard(w){
  if(!w)return"";
  const cls=WDOT[w.state]||"idle";
  const log=w.log&&w.log.length?`<pre class="wlog">${esc(w.log.join("\n"))}</pre>`:"";
  // Revert is gated separately from Retry: with no recorded known-good
  // revision there is nothing safe to roll back to, so the button is hidden
  // rather than offered and then refused.
  const revert=w.can_revert
    ?` <button class="btn sm" onclick="revertSettings()">Revert last change</button>`:"";
  const retry=w.can_retry
    ?`<button class="btn sm" onclick="workerAction('restart')">Retry</button>${revert}`
    :`<button class="btn sm" onclick="workerAction('restart')">Restart bot</button>`;
  return `<div class="box" id="wcard" style="margin-bottom:1rem">
    <div class="wstat"><span class="wdot ${cls}"></span><span>${esc(w.headline||"")}</span></div>
    ${log}
    <div style="margin-top:.9rem;display:flex;gap:.5rem;flex-wrap:wrap">${retry}</div></div>`;
}

function cryptoCard(c){
  if(!c||c.available)return"";
  return `<div class="box" style="margin-bottom:1rem;border-color:#f8514955">
    <b style="color:#f85149">Secrets are not encrypted</b>
    <div class="faint" style="margin-top:.4rem">The <code>cryptography</code>
    package isn't installed, so tokens and passwords are stored in plain text in
    the database. Install it and restart to fix this.</div></div>`;
}

function pendingCard(d){
  if(!d.pending||!d.pending.length)return"";
  return `<div class="box" style="margin-bottom:1rem;border-color:#e3b34155">
    <b>${d.pending.length} change${d.pending.length>1?"s":""} need a bot restart</b>
    <div class="faint" style="margin:.4rem 0 .8rem">${d.pending.map(esc).join(", ")}</div>
    <button class="btn" onclick="applySettings()">Apply &amp; restart bot</button></div>`;
}

function groupBox(g){
  return `<div class="box" style="margin-bottom:1rem"><h3 style="margin-top:0">${esc(g.label)}</h3>
    <div class="setform">${g.items.map(settingRow).join("")}</div></div>`;
}

function settingRow(s){
  const id="set_"+s.key;
  const chip=s.pinned
    ?'<span class="pill pin" title="Set in docker-compose.yml — that wins over anything saved here">Pinned by environment</span>'
    :(s.source==="default"?'<span class="pill faint">Default</span>':"");
  const derived=s.derived!==undefined&&s.derived!==""
    ?`<span class="pill faint" title="Inherited because this field is blank">Inherited: ${esc(s.derived)}</span>`:"";
  const lock=!s.editable
    ?'<span class="pill faint" title="Only settable in docker-compose.yml">compose only</span>':"";

  let control;
  const dis=s.editable?"":" disabled";
  if(s.kind==="bool"){
    const on=String(s.value).toLowerCase();
    const isOn=["1","true","yes","y","on"].includes(on);
    control=`<div class="seg" id="${id}" data-val="${isOn}">
      <button class="${isOn?"on":""}" onclick="setBool('${s.key}',true)"${dis}>On</button>
      <button class="${!isOn?"on":""}" onclick="setBool('${s.key}',false)"${dis}>Off</button></div>`;
  }else if(s.choices&&s.choices.length){
    control=`<select id="${id}"${dis}>`+s.choices.map(c=>
      `<option value="${esc(c)}"${String(s.value)===c?" selected":""}>${esc(c)}</option>`).join("")
      +`</select><button class="btn sm" onclick="saveSetting('${s.key}')"${dis}>Save</button>`;
  }else if(s.secret){
    const hint=s.is_set?`<span class="faint">${esc(s.masked)}</span>`:'<span class="faint">not set</span>';
    control=`<input type="password" id="${id}" placeholder="${s.is_set?"unchanged":"not set"}"
      autocomplete="new-password"${dis}>${hint}
      <button class="btn sm" onclick="saveSetting('${s.key}')"${dis}>Save</button>`
      +(s.is_set?`<button class="btn sm" onclick="clearSetting('${s.key}')"${dis}>Clear</button>`:"");
  }else{
    control=`<input type="text" id="${id}" value="${esc(s.value)}"${dis}>
      <button class="btn sm" onclick="saveSetting('${s.key}')"${dis}>Save</button>`;
  }

  const note=s.restart_note?`<div class="setnote warn">${esc(s.restart_note)}</div>`:"";
  const stored=(s.pinned&&s.stored)
    ?`<div class="setnote faint">Saved here: <b>${esc(s.stored)}</b> — takes effect once you remove it from docker-compose.yml.</div>`:"";
  return `<div class="setrow">
    <div><div class="lbl">${esc(s.label)} ${chip}${derived}${lock}</div>
      ${s.help?`<div class="hlp">${esc(s.help)}</div>`:""}</div>
    <div class="ctl">${control}</div>${note}${stored}</div>`;
}

async function saveSetting(key,valueOverride){
  const el=document.getElementById("set_"+key);
  let value=valueOverride;
  if(value===undefined){
    if(!el)return;
    value=el.value;
    // An empty secret box means "leave it alone", not "clear it" — clearing is
    // an explicit button, so a stray Save can't wipe a token.
    const s=findSetting(key);
    if(s&&s.secret&&value==="")return toast("Nothing entered");
  }
  const r=await post("/api/settings",{key,value});
  if(!r||!r.ok)return toast((r&&r.error)||"Save failed");
  toast(r.effective?(r.needs_restart?"Saved — restart pending":"Saved ✓")
                   :"Saved, but your compose file still overrides it");
  render();
}
function clearSetting(key){
  if(!confirm("Clear "+key+"? The bot will fall back to its default."))return;
  post("/api/settings",{key,value:null}).then(r=>{
    toast(r&&r.ok?"Cleared ✓":"Failed");render();});
}
function setBool(key,on){saveSetting(key,on?"true":"false");}
function findSetting(key){
  if(!SETTINGS)return null;
  for(const g of SETTINGS.groups)for(const s of g.items)if(s.key===key)return s;
  return null;
}
async function applySettings(){
  toast("Restarting the bot…");
  await post("/api/settings/apply",{});
  setTimeout(render,1500);
}
async function revertSettings(){
  if(!confirm("Revert to the last configuration the bot started with?\n\n"
    +"Secrets can't be restored — any secret changed since then is cleared."))return;
  const r=await post("/api/settings/revert",{});
  toast(r&&r.ok?"Reverted ✓":((r&&r.error)||"Nothing to revert"));
  setTimeout(render,1200);
}
async function workerAction(action){
  toast(action==="restart"?"Restarting…":"Stopping…");
  await post("/api/worker",{action});
  setTimeout(render,1500);
}
async function pollWorker(){
  // Re-render the whole status card, not just the headline. A bot that
  // quarantines while this tab is open needs its dot, its stderr log and its
  // Retry/Revert buttons to appear — those are the recovery controls the card
  // exists to provide, and updating only the text left them invisible.
  try{
    const w=await api("/api/worker");if(!w)return;
    const host=document.getElementById("wcard");
    if(!host)return;
    const next=workerCard(w);
    if(host.outerHTML!==next)host.outerHTML=next;
  }catch(e){}
}

function accountBox(){
  return `<div class="box" style="margin-bottom:1rem"><h3 style="margin-top:0">Dashboard account</h3>
    <div class="setform"><div class="setrow">
      <div><div class="lbl">Change password</div>
        <div class="hlp">Signs out every other session, including your own on
        other devices.</div></div>
      <div class="ctl">
        <input type="password" id="pw_old" placeholder="Current password" autocomplete="current-password">
        <input type="password" id="pw_new" placeholder="New password (min 12)" autocomplete="new-password">
        <button class="btn sm" onclick="changePassword()">Update</button></div>
    </div>
    <div class="setrow">
      <div><div class="lbl">Export configuration</div>
        <div class="hlp">Downloads a .env file reproducing every setting.
        Contains all your secrets in plain text.</div></div>
      <div class="ctl"><button class="btn sm" onclick="exportEnv()">Download .env</button></div>
    </div></div></div>`;
}
async function changePassword(){
  const oldPw=document.getElementById("pw_old").value;
  const newPw=document.getElementById("pw_new").value;
  const r=await post("/api/password",{old:oldPw,new:newPw});
  if(r&&r.ok){toast("Password changed — signing you out");
    setTimeout(()=>location.href="/login",900);}
  else toast((r&&r.error)||"Failed");
}
function exportEnv(){
  if(!confirm("This file contains every secret in plain text, including your "
    +"Discord token.\n\nDownload it?"))return;
  location.href="/api/settings/export";
}

function historyBox(h){
  if(!h||!h.length)return"";
  const rows=h.map(x=>`<tr><td class="faint">${esc((x.at||"").replace("T"," ").slice(0,16))}</td>
    <td>${esc(x.key)}</td>
    <td class="faint">${x.redacted?"(hidden)":esc(x.new===null?"cleared":String(x.new))}</td>
    <td class="faint">${esc(x.actor||"")}</td></tr>`).join("");
  return `<div class="box"><h3 style="margin-top:0">Recent changes</h3>
    <div style="overflow-x:auto"><table><thead><tr><th>When</th><th>Setting</th>
    <th>New value</th><th>By</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
}

function stat(n,l){return `<div class="stat"><div class="n">${esc(n)}</div><div class="l">${esc(l)}</div></div>`;}

async function renderOverview(v){
  const d=await api(`/api/overview?guild=${guild}`);if(!d)return;const t=d.totals||{};
  const L=d.live||{};
  // Compact daily message sparkline (last 7d).
  const msgs=L.messages_7d||[];const mmax=Math.max(1,...msgs.map(x=>x.count));
  const msgSpark=msgs.length?`<div class="ov-spark" title="messages/day, last 7d">${msgs.map(x=>
    `<span class="ovbar" style="height:${Math.max(8,Math.round(x.count/mmax*100))}%" title="${esc(x.date)}: ${x.count}"></span>`).join("")}</div>`:"";
  const topToday=(L.top_today||[]).length?`<div class="ov-games">${L.top_today.map(g=>`<span class="ov-game">
      ${gameTile(g.name,g.image,18,"ovgi")}${esc(g.name)}</span>`).join("")}</div>`
    :'<span class="faint">nobody playing today</span>';
  v.innerHTML=`${pageHead("Overview","Live server health, tracking and recent changes")}<div class="cards">
    ${stat(d.member_count,"Members")}${stat(d.role_count,"Roles")}
    ${stat(t.total_lifts||0,"Lifts")}${stat(t.lifters||0,"Lifters")}
    ${stat(t.unique_equip||0,"Exercises")}</div>
    <div class="ov-live">
      <div class="ov-tile"><div class="ov-k">Tracked now</div>
        <div class="ov-v">${L.online||0} online <span class="faint">· ${L.playing||0} playing · ${L.tracked||0} tracked</span></div></div>
      <div class="ov-tile"><div class="ov-k">Avg sleep <span class="faint">7d</span></div>
        <div class="ov-v">${L.avg_sleep!=null?fmtHours(L.avg_sleep):"—"}</div></div>
      <div class="ov-tile"><div class="ov-k">Messages <span class="faint">7d</span></div>${msgSpark}</div>
      <div class="ov-tile ov-wide"><div class="ov-k">Top games today</div>${topToday}</div>
    </div>
    <h2>Recent activity</h2>${auditTable(d.recent_audit)}`;
}

async function renderMembers(v){
  const d=await api(`/api/members?guild=${guild}`);if(!d)return;
  if(!d.members.length){
    v.innerHTML=pageHead("Members","People mirrored from this Discord server")
      +emptyState("👥","No members synced yet",
        "Synchronize with Discord to populate members and roles.",
        '<button class="btn" onclick="resync()">↻ Sync now</button>');
    return;
  }
  v.innerHTML=`<div class="filters"><h2 style="margin:0">Members
      <span class="faint">· ${d.members.length}</span></h2><span class="sp" style="flex:1"></span>
      <button class="btn" onclick="inviteDialog()">➕ Invite by ID</button>
      ${searchBar("Search members…")}</div>
    <div class="tcard"><table><thead><tr><th>Member</th><th>Username</th><th>Roles</th>
    <th>Joined</th></tr></thead><tbody>${d.members.map(m=>`<tr>
      <td>${who(m.user_id,m.display_name)} ${m.is_bot?'<span class="pill">bot</span>':''}
        ${m.present?'':'<span class="pill faint">left</span>'}</td>
      <td class="muted">${esc(m.username)}</td>
      <td>${m.role_count}</td>
      <td class="muted">${fmtTs(m.joined_at)}</td></tr>`).join("")}</tbody></table></div>`;
}

async function memberView(uid){
  tab="members";currentMember=uid;renderNav();
  // No-ops when the URL already points here, so the re-render after an edit
  // (rename, role change, timeout removal) doesn't stack history entries.
  pushPath("members",uid);
  const v=document.getElementById("view");v.innerHTML=spinner();
  v.setAttribute("aria-busy","true");
  let d;
  try{d=await api(`/api/member?guild=${guild}&user=${uid}`);}
  catch(e){v.innerHTML=errorState(e,`memberView('${uid}')`);return;}
  finally{v.removeAttribute("aria-busy");}
  if(!d)return;
  const m=d.member,o=d.overview||{},L=o.lifts||{},cal=o.calories||{},pro=o.protein||{};
  const n=d.nutrition||{},foods=d.foods||[],goals=d.lift_goals||[],bw=d.bodyweights||[],
    bodyMetrics=d.body_metrics||[];
  currentFoods=foods;currentNutrition=n;
  v.innerHTML=`<div class="crumb" onclick="go('members')">← Members</div>
    <div class="hero">${avatar(uid,m.display_name,m.avatar,72)}
      <div><h2>${esc(m.display_name||uid)}</h2>
      <div class="muted">${esc(m.username||"")}</div>
      <div class="chips">${d.strava_linked?'<span class="pill">🟧 Strava</span>':''}
        ${d.revo_linked?'<span class="pill">🟢 Revo</span>':''}
        ${d.ha_server?`<span class="pill" title="${esc(d.ha_server)}${d.ha_prefix?" · "+esc(d.ha_prefix):" · no sensors linked yet"}">🏠 Home Assistant</span>`:''}
        ${d.calorie_streak>=2?`<span class="pill">🔥 ${d.calorie_streak}d calories</span>`:''}
        ${d.protein_streak>=2?`<span class="pill">🔥 ${d.protein_streak}d protein</span>`:''}
        ${m.present?'':'<span class="pill faint">left server</span>'}</div></div></div>
    <div class="cards">${stat(L.n||0,"Lifts")}${stat(L.equip||0,"Exercises")}
      ${stat(o.bodyweight?o.bodyweight.weight_kg+" kg":"—","Bodyweight")}
      ${stat(Math.round(cal.total||0),"kcal logged")}${stat(Math.round(pro.total||0),"g protein")}</div>
    <div class="grid2">
      <div class="box"><h3 style="display:flex;justify-content:space-between;align-items:center">
        <span>Today's nutrition</span>${activeTargetPill(n.targets)}</h3>
        ${bar("Calories",n.calorie_today,n.calorie_goal," kcal",false)}
        ${bar("Protein",n.protein_today,n.protein_goal," g",true)}
        ${targetsTable(n.targets,uid)}</div>
      <div class="box"><h3>Bodyweight trend</h3>${sparkline(bw)}
        ${bwList(bw,uid)}</div>
    </div>
    ${bodyMetricCards(bodyMetrics)}
    <div class="grid2">
      <div class="box"><h3 style="display:flex;justify-content:space-between">Saved foods
        <a class="link" onclick="foodDialog('${uid}')">+ add</a></h3>
        ${foods.length?`<table><tbody>${foods.map((f,i)=>`<tr>
          <td><b>${esc(f.display)}</b>${(f.aliases&&f.aliases.length)?
            `<div class="faint" style="margin-top:3px;font-size:11px">also: ${
              f.aliases.map(a=>`<span class="pill">${esc(a)}
                <a class="link" title="remove this name"
                   onclick="foodAliasDel('${uid}','${esc(a)}')">×</a></span>`).join(" ")
            }</div>`:''}</td>
          <td>${Math.round(f.kcal)} kcal</td>
          <td>${f.protein_g!=null?Math.round(f.protein_g)+' g':'<span class="faint">—</span>'}</td>
          <td><div class="row-actions">
          <button class="btn sm" onclick="foodAliasAdd('${uid}',${i})">+ name</button>
          <button class="btn sm" onclick="foodEditIdx('${uid}',${i})">edit</button>
          <button class="btn sm danger" onclick="foodDeleteIdx('${uid}',${i})">del</button>
          </div></td></tr>`).join("")}</tbody></table>`:'<div class="faint">No saved foods.</div>'}</div>
      <div class="box"><h3>Lift goals</h3>${goals.length?goals.map(g=>{
        const p=pct(g.current_best,g.target_kg),done=g.current_best>=g.target_kg;
        return `<div class="pgoal"><div class="pgrow"><span>${esc(g.equipment)}${g.bw?' <span class="faint">(BW+)</span>':''}</span>
          <span>${g.current_best}/${g.target_kg} kg${done?' 🎯':''}</span></div>
          <div class="ptrack"><div class="pfill" style="width:${p}%;background:${done?'#10b981':'linear-gradient(90deg,#6366f1,#22d3ee)'}"></div></div></div>`;
        }).join(""):'<div class="faint">No goals set.</div>'}</div>
    </div>
    <h2>Roles</h2><div class="chips">${d.roles.length?d.roles.map(r=>roleChip(uid,r)).join("")
      :'<span class="muted">No roles.</span>'}</div>
    ${m.present?roleAdder(uid,d.roles):''}
    ${m.present?`<h2 style="margin-top:1.6rem">Moderation</h2><div id="modbox" class="faint">Checking timeout…</div>`:''}
    ${d.presence_tracking_available?`<h2 style="margin-top:1.6rem">Presence tracking</h2><div id="trackbox">${trackBox(uid,d.presence_tracked)}</div>`:''}
    <h2 style="margin-top:1.6rem">History</h2>${auditTable(d.audit)}
    <p style="margin-top:1rem" class="muted">View this member's
      <a class="link" onclick="go2('lifts','${uid}')">lifts</a>,
      <a class="link" onclick="go2('calories','${uid}')">calories</a> or
      <a class="link" onclick="go2('protein','${uid}')">protein</a>.</p>`;
  if(m.present)loadModeration(uid);
}

// ---- moderation (remove timeout + auto un-timeout protection) ----
async function loadModeration(uid){
  const box=document.getElementById("modbox");if(!box)return;
  let d;try{d=await api(`/api/member/moderation?guild=${guild}&user=${uid}`);}catch(e){box.remove();return;}
  if(!d||!d.ok){box.remove();return;}
  box.classList.remove("faint");
  const timeoutLine=d.timed_out
    ? `<div class="modrow"><span class="pill" style="border-color:#5c2b2b">⏳ Timed out until ${fmtTs(d.timed_out_until)}</span>
        <button class="btn sm danger" ${d.can_moderate?'':'disabled title="Bot lacks Moderate Members or is outranked"'}
          onclick="removeTimeout('${uid}')">Remove timeout</button></div>`
    : `<div class="modrow"><span class="faint">No active timeout.</span></div>`;
  // Auto un-timeout protection toggle (only when the feature's master switch is on).
  let protLine="";
  if(d.auto_untimeout_available){
    protLine=d.auto_untimeout
      ? `<div class="modrow"><span class="pill">🛡️ Auto un-timeout: ON — timeouts are removed automatically</span>
          <button class="btn sm danger" onclick="setAutoUntimeout('${uid}',false)">Turn off</button></div>`
      : `<div class="modrow"><span class="faint">🛡️ Auto un-timeout: off for this member.</span>
          <button class="btn sm" onclick="setAutoUntimeout('${uid}',true)">Protect from timeouts</button></div>`;
  }
  box.innerHTML=timeoutLine+protLine;
}
async function removeTimeout(uid){
  const r=await post("/api/member/untimeout",{guild,user:uid});
  if(r&&r.ok){toast(r.changed===false?"Wasn't timed out":"Timeout removed ✓");memberView(uid);}
  else{toast((r&&r.error)||"Failed");}
}
async function setAutoUntimeout(uid,enable){
  const r=await post("/api/member/autountimeout",{guild,user:uid,enable});
  if(r&&r.ok){toast(enable?"Auto un-timeout enabled 🛡️":"Auto un-timeout disabled");loadModeration(uid);}
  else{toast((r&&r.error)||"Failed");}
}

// ---- presence tracking (start/stop from the member panel) ----
function trackBox(uid,tracked){
  return tracked
    ? `<span class="pill">🎮 Recording presence &amp; activity</span>
       <button class="btn sm danger" onclick="setTrack('${uid}',false)">Stop tracking</button>`
    : `<span class="faint">Not tracked.</span>
       <button class="btn sm" onclick="setTrack('${uid}',true)">Start tracking</button>`;
}
async function setTrack(uid,start){
  if(start&&!confirm("Start recording this member's online/offline status and game activity?"))return;
  const r=await post("/api/member/track",{guild,user:uid,action:start?'start':'stop'});
  if(r&&r.ok){toast(start?"Tracking started ✓":"Tracking stopped ✓");memberView(uid);}
  else{toast((r&&r.error)||"Failed");}
}

function foodEditIdx(uid,i){foodDialog(uid,currentFoods[i]);}
function foodDeleteIdx(uid,i){foodDelete(uid,currentFoods[i].name);}
function foodAliasAdd(uid,i){
  const f=currentFoods[i];
  const dlg=document.getElementById("editDlg");
  dlg.innerHTML=`<h2>Another name for ${esc(f.display)}</h2>
    <div class="faint" style="margin-bottom:8px">Typing this in chat logs
      ${esc(f.display)}. Plurals already work automatically — add one here for
      anything else you call it.</div>
    <label>Name</label><input id="fa_alias" placeholder="e.g. boneless wicked wings">
    <div class="dlg-actions"><button class="btn" onclick="editDlg.close()">Cancel</button>
    <button class="btn primary" onclick="foodAliasSave('${uid}','${esc(f.name)}')">Add</button></div>`;
  dlg.showModal();
}
async function foodAliasSave(uid,name){
  const alias=document.getElementById("fa_alias").value.trim();
  if(!alias){toast("Name required");return;}
  const r=await post("/api/foods/alias/set",{guild,user:uid,name,alias});
  document.getElementById("editDlg").close();
  toast(r&&r.ok?"Added ✓":"Failed");memberView(uid);
}
async function foodAliasDel(uid,alias){
  if(!confirm(`Remove the name "${alias}"?`))return;
  const r=await post("/api/foods/alias/delete",{guild,user:uid,alias});
  toast(r&&r.ok?"Removed ✓":"Failed");memberView(uid);
}
function foodDialog(uid,f){
  const dlg=document.getElementById("editDlg");f=f||{};
  dlg.innerHTML=`<h2>${f.name?'Edit':'Add'} food</h2>
    <label>Name</label><input id="f_name" value="${f.display?esc(f.display):''}" ${f.name?'readonly':''} placeholder="e.g. Protein shake">
    <label>Calories (kcal)</label><input id="f_kcal" type="number" value="${f.kcal!=null?f.kcal:''}">
    <label>Protein (g, optional)</label><input id="f_pro" type="number" value="${f.protein_g!=null?f.protein_g:''}">
    <div class="dlg-actions"><button class="btn" onclick="editDlg.close()">Cancel</button>
    <button class="btn primary" onclick="foodSave('${uid}')">Save</button></div>`;
  dlg.showModal();
}
async function foodSave(uid){
  const display=document.getElementById("f_name").value.trim();
  const kcal=document.getElementById("f_kcal").value;
  const pro=document.getElementById("f_pro").value;
  if(!display||kcal===""){toast("Name and calories required");return;}
  const r=await post("/api/foods/set",{guild,user:uid,display,kcal,protein_g:pro===""?null:pro});
  document.getElementById("editDlg").close();toast(r&&r.ok?"Saved ✓":"Failed");memberView(uid);
}
async function foodDelete(uid,name){
  if(!confirm("Delete this saved food?"))return;
  const r=await post("/api/foods/delete",{guild,user:uid,name});
  toast(r&&r.ok?"Deleted ✓":"Failed");memberView(uid);
}

// ---- role grants (member detail) ----
function roleChip(uid,r){
  return `<span class="pill"><span class="dot" style="background:${roleColor(r.color)}"></span>${esc(r.name)}
    <a class="rmrole" title="Remove role" onclick="setRole('${uid}','${r.role_id}','remove')">✕</a></span>`;
}
function roleAdder(uid,have){
  const had=new Set((have||[]).map(r=>String(r.role_id)));
  const opts=ALL_ROLES.filter(r=>!had.has(String(r.role_id)));
  if(!opts.length)return '<div class="faint" style="margin-top:.5rem">Member has every role.</div>';
  return `<div class="rolectl"><select id="addRoleSel">${opts.map(r=>
    `<option value="${r.role_id}">${esc(r.name)}${r.managed?' (managed)':''}</option>`).join("")}</select>
    <button class="btn sm" onclick="addRole('${uid}')">+ Add role</button></div>`;
}
function addRole(uid){const sel=document.getElementById("addRoleSel");
  if(!sel||!sel.value){toast("Pick a role");return;}setRole(uid,sel.value,'add');}
async function setRole(uid,rid,action){
  const r=await post("/api/member/role",{guild,user:uid,role_id:rid,action});
  if(r&&r.ok){toast(action==='add'?"Role added ✓":"Role removed ✓");
    await loadRoles();memberView(uid);}
  else{toast((r&&r.error)||"Failed");}
}

// ---- invite a user by ID ----
async function inviteDialog(){
  const dlg=document.getElementById("editDlg");
  dlg.innerHTML=`<h2>Invite a user by ID</h2>
    <p class="muted" style="margin:.2rem 0 .6rem;font-size:.85rem">Creates a one-use
    invite and tries to DM it to the user. If their DMs are closed you can copy the
    link and send it yourself.</p>
    <label>User ID</label><input id="inv_uid" placeholder="e.g. 123456789012345678" inputmode="numeric">
    <label>Channel</label><select id="inv_ch"><option value="">Loading…</option></select>
    <div id="inv_result" style="margin-top:.8rem"></div>
    <div class="dlg-actions"><button class="btn" onclick="editDlg.close()">Close</button>
    <button class="btn primary" onclick="sendInvite()">Create &amp; send</button></div>`;
  dlg.showModal();
  let chans=[];try{const d=await api(`/api/channels?guild=${guild}`);chans=(d&&d.channels)||[];}catch(e){}
  const sel=document.getElementById("inv_ch");
  sel.innerHTML=chans.length?chans.map(c=>`<option value="${c.id}">#${esc(c.name)}</option>`).join("")
    :'<option value="">(bot picks a channel)</option>';
}
async function sendInvite(){
  const uid=(document.getElementById("inv_uid").value||"").trim();
  const ch=document.getElementById("inv_ch").value;
  if(!/^\d+$/.test(uid)){toast("Enter a numeric user ID");return;}
  const res=document.getElementById("inv_result");res.innerHTML='<span class="muted">Creating invite…</span>';
  const r=await post("/api/invite",{guild,user_id:uid,channel_id:ch||null});
  if(!r)return;
  if(r.ok){
    res.innerHTML=`<div class="kv"><span>Invite</span><span><a class="link" href="${esc(r.link)}" target="_blank">${esc(r.link)}</a>
      <button class="btn sm" onclick="navigator.clipboard.writeText('${esc(r.link)}');toast('Copied')">Copy</button></span></div>
      <div style="margin-top:.5rem" class="${r.dmed?'':'faint'}">${r.dmed?'✅ DM sent to the user.'
        :'⚠️ Could not DM them'+(r.error?': '+esc(r.error):'')+'. Share the link manually.'}</div>`;
    toast(r.dmed?"Invite sent ✓":"Invite link ready");
  }else{
    res.innerHTML=`<span style="color:#f85149">${esc(r.error||"Failed")}</span>`;
  }
}

async function renderLeaderboard(v){
  const eq=await api(`/api/equipment?guild=${guild}`);if(!eq)return;
  const list=eq.equipment||[];
  if(!list.length){
    v.innerHTML=pageHead("Leaderboard","Best recorded lift for each exercise")
      +emptyState("🏆","No leaderboard yet",
        "Logged lifts will appear here once members start training.");
    return;
  }
  if(!lbEquip||!list.includes(lbEquip))lbEquip=list[0];
  const d=await api(`/api/leaderboard?guild=${guild}&equipment=${encodeURIComponent(lbEquip)}`);if(!d)return;
  const medal=["🥇","🥈","🥉"];
  v.innerHTML=`<div class="filters"><h2 style="margin:0">🏆 Leaderboard</h2>
    <select onchange="lbEquip=this.value;render()">${list.map(e=>
      `<option ${e===lbEquip?'selected':''}>${esc(e)}</option>`).join("")}</select>
    <span class="sp" style="flex:1"></span>${searchBar("Search…")}</div>
    <div class="tcard"><table><thead><tr><th>#</th><th>Member</th><th>Best</th><th>Set</th></tr></thead>
    <tbody>${d.rows.map((r,i)=>`<tr><td><span class="rank">${medal[i]||(i+1)}</span></td>
      <td>${who(r.user_id,r.username)}</td>
      <td><b>${r.best}${r.bw?' <span class="faint">(BW+)</span>':''}</b> kg</td>
      <td class="muted">${fmtTs(r.set_on)}</td></tr>`).join("")||
      '<tr><td colspan="4" class="muted">No entries.</td></tr>'}</tbody></table></div>`;
}
function go2(t,uid){
  // Jump to a data tab pre-filtered to one member. The filter itself is
  // transient (not in the URL), so Back returns to the unfiltered tab.
  pushPath(t);dataUserFilter=uid;tab=t;SEARCH="";renderNav();render();}

async function renderRoles(v){
  const d=await api(`/api/roles?guild=${guild}`);if(!d)return;
  if(!d.roles.length){
    v.innerHTML=pageHead("Roles","Discord roles and their current membership")
      +emptyState("🛡️","No roles synced yet",
        "Synchronize with Discord to populate the role directory.",
        '<button class="btn" onclick="resync()">↻ Sync now</button>');
    return;
  }
  v.innerHTML=`<h2>Roles <span class="faint">· ${d.roles.length}</span></h2>
    <div class="tcard"><table><thead><tr><th>Role</th><th>Members</th><th>Position</th></tr></thead>
    <tbody>${d.roles.map(r=>`<tr>
      <td><span class="pill"><span class="dot" style="background:${roleColor(r.color)}"></span>${esc(r.name)}</span>
        ${r.managed?'<span class="pill faint">managed</span>':''}</td>
      <td><a class="link" onclick="roleView('${r.role_id}','${esc(r.name).replace(/'/g,"&#39;")}')">${r.members}</a></td>
      <td class="muted">${r.position}</td></tr>`).join("")}</tbody></table></div>`;
}
async function roleView(rid,name){
  const v=document.getElementById("view");v.innerHTML=spinner();
  const d=await api(`/api/role?guild=${guild}&role=${rid}`);if(!d)return;
  v.innerHTML=`<div class="crumb" onclick="go('roles')">← Roles</div><h2>${esc(name)}
    <span class="faint">· ${d.members.length}</span></h2>
    <div class="tcard"><table><thead><tr><th>Member</th><th>Username</th></tr></thead>
    <tbody>${d.members.map(m=>`<tr><td>${who(m.user_id,m.display_name)}
      ${m.present?'':'<span class="pill faint">left</span>'}</td>
      <td class="muted">${esc(m.username)}</td></tr>`).join("")||
      '<tr><td colspan="2" class="muted">No members.</td></tr>'}</tbody></table></div>`;
}

async function renderAudit(v){auditOffset=0;auditRows=[];await loadAuditPage(v);}
async function loadAuditPage(v){
  const d=await api(`/api/audit?guild=${guild}&limit=100&offset=${auditOffset}${auditCat?'&category='+auditCat:''}`);if(!d)return;
  auditRows=auditRows.concat(d.audit);auditOffset+=d.audit.length;
  v.innerHTML=`${pageHead("Audit log","Who changed what across Discord and tracked data")}
    <div class="filters"><div class="seg" aria-label="Audit category">${["","role","member","data"].map(c=>
      `<button class="${c===auditCat?'on':''}" onclick="auditCat='${c}';render()">${c||"all"}</button>`).join("")}</div>
      <span class="sp" style="flex:1"></span>${searchBar("Search audit…")}
      <span class="faint">${auditRows.length} / ${d.total}</span></div>${auditTable(auditRows)}
      ${auditOffset<d.total?`<div style="text-align:center;margin-top:1rem">
        <button class="btn" onclick="loadAuditPage(document.getElementById('view'))">Load more (${d.total-auditOffset} more)</button></div>`:''}`;
}
function auditTable(rows){
  if(!rows||!rows.length)return '<div class="empty">Nothing here yet.</div>';
  return `<div class="tcard"><table><thead><tr><th>When</th><th>Category</th><th>Action</th>
    <th>Actor</th><th>Subject</th><th>Detail</th></tr></thead><tbody>${rows.map(a=>`<tr>
      <td class="muted" style="white-space:nowrap">${fmtTs(a.at)}</td>
      <td class="cat-${a.category}">${esc(a.category)}</td>
      <td style="white-space:nowrap">${actionLabel(a.action)}</td>
      <td>${a.actor_id?`<span class="who">${avatar(a.actor_id,a.actor_name,(AV[a.actor_id]||{}).avatar,24)}
        <a class="link" onclick="memberView('${a.actor_id}')">${esc(a.actor_name)}</a></span>`
        :`<span class="muted">${esc(a.actor_name||"—")}</span>`}</td>
      <td>${a.subject_id?`<span class="who">${avatar(a.subject_id,a.subject_name,a.subject_avatar||(AV[a.subject_id]||{}).avatar)}
        <a class="link" onclick="memberView('${a.subject_id}')">${esc(a.subject_name||a.subject_id)}</a></span>`
        :esc(a.subject_name||"—")}</td>
      <td class="muted">${esc(a.detail||"")}</td></tr>`).join("")}</tbody></table></div>`;
}

async function renderData(v,kind){
  const u=dataUserFilter;
  const d=await api(`/api/${kind}?guild=${guild}&limit=200${u?'&user='+u:''}`);if(!d)return;
  const rows=d[kind];
  const head=kind==="lifts"?"<th>Exercise</th><th>Weight</th><th>Reps</th>":
    kind==="calories"?"<th>kcal</th><th>Note</th>":"<th>Protein</th><th>Note</th>";
  v.innerHTML=`<div class="filters"><h2 style="margin:0">${kind[0].toUpperCase()+kind.slice(1)}
      <span class="faint">· ${rows.length}${u?' · filtered':''}</span></h2>
      ${u?`<a class="link" onclick="dataUserFilter=null;render()">× clear member filter</a>`:''}
      <span class="sp" style="flex:1"></span>${searchBar("Search…")}</div>
    <div class="tcard"><table><thead><tr><th>When</th><th>Member</th>${head}<th></th></tr></thead>
    <tbody>${rows.map(r=>dataRow(kind,r)).join("")||
      '<tr><td colspan="6" class="empty">Nothing logged.</td></tr>'}</tbody></table></div>`;
}
function dataRow(kind,r){
  let cells;
  if(kind==="lifts")cells=`<td><b>${esc(r.equipment)}</b></td><td>${r.weight_kg}${r.bw?' <span class="faint">(BW+)</span>':''}</td><td class="muted">${r.reps??""}</td>`;
  else if(kind==="calories")cells=`<td><b>${Math.round(r.kcal)}</b></td><td class="muted">${esc(r.note||"")}</td>`;
  else cells=`<td><b>${Math.round(r.grams)} g</b></td><td class="muted">${esc(r.note||"")}</td>`;
  const editBtn=kind==="lifts"?`<button class="btn sm" onclick='editLift(${JSON.stringify(r)})'>Edit</button>`:"";
  return `<tr><td class="muted" style="white-space:nowrap">${fmtTs(r.logged_at)}</td>
    <td>${who(r.user_id,r.username)}</td>${cells}
    <td><div class="row-actions">${editBtn}
    <button class="btn sm danger" onclick="delData('${kind}',${r.id})">Delete</button></div></td></tr>`;
}
async function delData(kind,id){
  if(!confirm("Delete this entry? This is audited and cannot be undone."))return;
  const path={lifts:"/api/lifts/delete",calories:"/api/calories/delete",protein:"/api/protein/delete"}[kind];
  const r=await post(path,{guild,id});toast(r&&r.ok?"Deleted ✓":"Failed");render();
}
function editLift(r){
  const dlg=document.getElementById("editDlg");
  dlg.innerHTML=`<h2>Edit lift</h2>
    <label>Exercise</label><input id="e_eq" value="${esc(r.equipment)}">
    <label>Weight (kg)</label><input id="e_w" type="number" step="0.5" value="${r.weight_kg}">
    <label>Reps</label><input id="e_r" type="number" value="${r.reps??''}">
    <div class="dlg-actions"><button class="btn" onclick="editDlg.close()">Cancel</button>
    <button class="btn primary" onclick="saveLift(${r.id})">Save</button></div>`;
  dlg.showModal();
}
async function saveLift(id){
  const eq=document.getElementById("e_eq").value.trim();
  const w=document.getElementById("e_w").value;const rp=document.getElementById("e_r").value;
  const r=await post("/api/lifts/edit",{guild,id,equipment:eq,weight_kg:w,reps:rp||null});
  document.getElementById("editDlg").close();toast(r&&r.ok?"Saved ✓":"Failed");render();
}
// ---- activity feed -------------------------------------------------------
const STATUS_RANK={online:0,idle:1,dnd:2,offline:3};
function statusClass(s){return "st-"+(["online","idle","dnd"].includes(s)?s:"offline");}
function statusLabel(s){return s||"unknown";}
function fmtPlaytime(sec){sec=sec||0;const h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60);
  const d=Math.floor(h/24),rh=h%24;if(d>=1)return d+"d "+(rh?rh+"h":"");
  if(h>=1)return h+"h "+(m?m+"m":"");return m>=1?m+"m":"<1m";}
function gameTile(name,url,size,cls){
  const px=`width:${size}px;height:${size}px`;
  if(url)return `<img class="${cls}" style="${px}" src="${esc(url)}" alt="" loading="lazy"
    onerror="this.replaceWith(Object.assign(document.createElement('span'),
    {className:'${cls} game-tile',style:'${px};background:${idColor(name)}',textContent:'${esc((name||'?')[0]||'?').toUpperCase()}'}))">`;
  return `<span class="${cls} game-tile" style="${px};background:${idColor(name)}">${esc((name||'?')[0]||'?').toUpperCase()}</span>`;}

// Day-window filter shared by the Activity and Sleep tabs. Defaults to 7 days
// (re-renders the active tab on change).
let WIN_DAYS=7;
function setWinDays(d){WIN_DAYS=d;render();}
function daySeg(){return `<span class="dayseg">`+
  [7,30].map(d=>`<button class="${WIN_DAYS===d?'on':''}" onclick="setWinDays(${d})">${d}d</button>`).join("")+
  `</span>`;}

async function renderActivity(v){
  const d=await api(`/api/activity?guild=${guild}&days=${WIN_DAYS}`);if(!d)return;
  if(!(d.users||[]).length){
    v.innerHTML=pageHead("Activity","Live Discord presence and game history")
      +emptyState("🎮","No tracked members yet",
        "Open a member and choose Start tracking, or use /track start in Discord.");
    return;
  }
  v.innerHTML=`<div class="filters"><h2 style="margin:0">🎮 Activity
      <span class="faint" id="actCount"></span></h2>
      <span class="live-dot" title="Live — refreshes automatically"></span>
      <span class="sp" style="flex:1"></span>${daySeg()}${searchBar("Search players or games…")}</div>
    <div id="actSummary">${actSummaryHTML(d.users)}</div>
    <div id="actLeader">${actLeaderHTML(d.leaderboard)}</div>
    <div class="act-grid" id="actGrid">${actCardsHTML(d.users)}</div>`;
  setActCount(d);
  setLive(liveActivity);
}
// Re-poll and patch just the grid/leaderboard so the search box keeps focus.
async function liveActivity(){
  if(tab!=="activity"){clearLive();return;}
  let d;try{d=await api(`/api/activity?guild=${guild}&days=${WIN_DAYS}`);}catch(e){return;}
  if(!d||tab!=="activity")return;
  const grid=document.getElementById("actGrid");if(!grid)return;
  grid.innerHTML=actCardsHTML(d.users);
  const summary=document.getElementById("actSummary");
  if(summary)summary.innerHTML=actSummaryHTML(d.users);
  const lead=document.getElementById("actLeader");if(lead)lead.innerHTML=actLeaderHTML(d.leaderboard);
  setActCount(d);
  filterTable(SEARCH);  // reapply the active search to the freshly drawn cards
}
function setActCount(d){const el=document.getElementById("actCount");if(!el)return;
  const online=(d.users||[]).filter(u=>["online","idle","dnd"].includes(u.status)).length;
  el.textContent=`· ${online}/${(d.users||[]).length} online · last ${d.window_days}d`;}
function actSummaryHTML(users){
  users=users||[];
  const online=users.filter(u=>["online","idle","dnd"].includes(u.status)).length;
  const playing=users.filter(u=>(u.current_games||[]).length).length;
  const coverage=users.length?Math.round(users.reduce(
    (n,u)=>n+((u.presence&&u.presence.coverage_percent)||0),0)/users.length):0;
  return `<div class="act-summary">
    <div class="act-stat"><b>${online}</b><span>online now</span></div>
    <div class="act-stat"><b>${playing}</b><span>playing now</span></div>
    <div class="act-stat" title="Average share of this window with a known status"><b>${coverage}%</b><span>capture coverage</span></div>
  </div>`;
}
function actCardsHTML(users){
  // online first, then by whether currently playing, then name.
  users=users.slice().sort((a,b)=>(STATUS_RANK[a.status]??3)-(STATUS_RANK[b.status]??3)
    || ((b.current_games||[]).length?1:0)-((a.current_games||[]).length?1:0)
    || a.display_name.localeCompare(b.display_name));
  return users.map(actCard).join("");
}
// Search key: player name plus every game shown, so a game name filters too.
function actSearchKey(u){return [u.display_name,...(u.current_games||[]).map(g=>g.name),
  ...(u.top_games||[]).map(g=>g.name)].join(" ").toLowerCase();}
function actLeaderHTML(rows){
  if(!rows||!rows.length)return"";
  const max=Math.max(1,...rows.map(r=>r.seconds));
  return `<div class="lead-card"><div class="lead-h">🏆 Most played · server · last ${WIN_DAYS}d</div>
    <div class="lead-list">${rows.map((r,i)=>`<div class="lead-row">
      <span class="lead-rank">${i+1}</span>${gameTile(r.name,r.image,26,"gi")}
      <div class="lead-nm">${esc(r.name)}<div class="barwrap"><div class="barfill" style="width:${Math.round(r.seconds/max*100)}%"></div></div></div>
      <div class="lead-who faint">${(r.players||[]).map(esc).join(", ")}</div>
      <div class="pt">${fmtPlaytime(r.seconds)}</div></div>`).join("")}</div></div>`;
}
function actCard(u){
  const offline=!["online","idle","dnd"].includes(u.status);
  const p=u.presence||{};
  const coverage=p.coverage_percent??0;
  const onlinePct=p.online_percent==null?"—":Math.round(p.online_percent)+"%";
  const statusFor=u.status_for_seconds==null?"":" · for "+fmtPlaytime(u.status_for_seconds);
  const trackingSince=u.tracking_since?" Tracking since "+fmtTs(u.tracking_since)+".":"";
  const maxSec=Math.max(1,...(u.top_games||[]).map(g=>g.seconds));
  const cur=u.current_games||[];
  const now=cur.length?`<div class="nowlist">${cur.map((g,i)=>`<div class="now">
      ${gameTile(g.name,g.image,48,"game-img")}
      <div class="meta"><div class="g">${esc(g.name)}</div>
      <div class="t">${i===0?"▶ playing now":"＋ also playing"}${g.since?" · since "+fmtTs(g.since):""}</div></div></div>`).join("")}</div>`
    : `<div class="now"><div class="meta"><div class="g faint">Not playing anything</div>
      <div class="t">${u.status_at?"Status since "+fmtTs(u.status_at):"Waiting for a status event"}</div></div></div>`;
  // Each title opens the log already filtered to it — the usual next question
  // after "he's played a lot of R6" is "when?".
  const top=(u.top_games||[]).length?`<div class="top-games">${u.top_games.map(g=>`<div class="tg tg-go"
      onclick="activityLog('${u.user_id}',${jsq(g.name)})" title="Session log for ${esc(g.name)}">
      ${gameTile(g.name,g.image,30,"gi")}
      <div class="nm">${esc(g.name)}<div class="barwrap"><div class="barfill" style="width:${Math.round(g.seconds/maxSec*100)}%"></div></div></div>
      <div class="pt">${fmtPlaytime(g.seconds)}</div></div>`).join("")}</div>`
    : '<div class="faint" style="font-size:.84rem">No games tracked in this window.</div>';
  return `<div class="act-card${offline?' offline-card':''}" data-search="${esc(actSearchKey(u))}">
    <div class="act-head">
      <span class="act-av">${avatar(u.user_id,u.display_name,u.avatar,44)}
        <span class="dot ${statusClass(u.status)}"></span></span>
      <div style="min-width:0"><div class="act-name"><a class="link" onclick="memberView('${u.user_id}')">${esc(u.display_name)}</a></div>
      <div class="act-sub">${statusLabel(u.status)}${statusFor}</div></div>
      <button class="btn sm act-log-btn" onclick="activityLog('${u.user_id}')"
        title="When they played what — every session in this window">📜 Log</button>
    </div>
    ${now}
    <div class="act-presence">
      <div class="act-metric" title="Known time online in this window"><span>Online</span><b>${fmtPlaytime(p.online_seconds)}</b></div>
      <div class="act-metric" title="Online share of time with a recorded status"><span>Of observed</span><b>${onlinePct}</b></div>
      <div class="act-metric${coverage<75?' low':''}" title="Share of the selected window with a known status"><span>Coverage</span><b>${Math.round(coverage)}%</b></div>
    </div>
    ${coverage<75?`<div class="act-note">Partial capture — totals may understate this window.${trackingSince}</div>`:''}
    <div><div class="act-sub" style="margin-bottom:.4rem">Most played</div>${top}</div>
  </div>`;
}

/* ---- per-player session log ---------------------------------------------
   "Most played" answers what; this answers when — every stretch of a title
   with its clock times, newest first, grouped by the day it started.
   It lives in the shared dialog rather than in the tab because the Activity
   grid re-polls on a timer and rewrites #actGrid wholesale — a panel rendered
   inside it would be yanked out from under whoever was reading it. */
let ACTLOG=null;        // last payload, kept so the title filter can redraw
let ACTLOG_GAME=null;   // active title filter, or null for "everything"

async function activityLog(uid,game){
  const dlg=document.getElementById("editDlg");
  ACTLOG=null;ACTLOG_GAME=game||null;
  dlg.innerHTML=`<h2>📜 Activity log</h2>${spinner()}`;
  dlg.showModal();
  let d;
  try{d=await api(`/api/activity/log?guild=${guild}&user=${uid}&days=${WIN_DAYS}`);}
  catch(e){
    dlg.innerHTML=`<h2>📜 Activity log</h2>
      <p class="faint">Couldn't load the log — try again in a moment.</p>
      <div class="dlg-actions"><button class="btn" onclick="editDlg.close()">Close</button></div>`;
    return;
  }
  if(!d)return;  // 401 — api() has already bounced us to the login page
  ACTLOG=d;drawActLog();
}
function actLogFilter(name){
  // Clicking the active title again clears the filter.
  ACTLOG_GAME=(name&&name!==ACTLOG_GAME)?name:null;drawActLog();
}
// "2026-08-04" (already in the dashboard's display timezone) -> "Tue 4 Aug".
// Parsed as local midnight so the browser's own zone can't shift the day.
function actLogDay(d){const dt=new Date(d+"T00:00:00");
  return isNaN(dt)?d:dt.toLocaleDateString([],{weekday:"short",day:"numeric",month:"short"});}
function actLogRow(s){
  const from=s.start_local.slice(11),to=s.end_local.slice(11);
  // A session that runs past midnight ends on the next date — say so instead
  // of showing "22:04 → 00:31" as if it were a 22-hour backwards stint.
  const over=s.end_local.slice(0,10)!==s.start_local.slice(0,10)
    ?'<span class="al-next">+1d</span>':"";
  const when=s.open?`${esc(from)} → <span class="al-now">now</span>`
    :`${esc(from)} → ${esc(to)}${over}`;
  return `<div class="al-row"${s.open?' data-open="1"':""}>
    ${gameTile(s.name,s.image,24,"gi")}
    <div class="al-nm">${esc(s.name)}</div>
    <div class="al-when">${when}</div>
    <div class="al-dur">${fmtPlaytime(s.seconds)}</div></div>`;
}
function drawActLog(){
  const d=ACTLOG;if(!d)return;
  const dlg=document.getElementById("editDlg");
  const games=d.games||[];
  const maxSec=Math.max(1,...games.map(g=>g.seconds));
  const sessions=(d.sessions||[]).filter(s=>!ACTLOG_GAME||s.name===ACTLOG_GAME);

  const chips=games.length?`<div class="al-games">${games.map(g=>`<div
      class="tg tg-go${g.name===ACTLOG_GAME?" on":""}" onclick="actLogFilter(${jsq(g.name)})">
      ${gameTile(g.name,g.image,26,"gi")}
      <div class="nm">${esc(g.name)}${g.playing_now?' <span class="al-now">▶ now</span>':""}
        <div class="barwrap"><div class="barfill" style="width:${Math.round(g.seconds/maxSec*100)}%"></div></div></div>
      <div class="al-meta faint">${g.sessions} session${g.sessions===1?"":"s"}</div>
      <div class="pt">${fmtPlaytime(g.seconds)}</div></div>`).join("")}</div>`
    : '<div class="faint">Nothing tracked in this window.</div>';

  // Group consecutive sessions by their local start date — the payload is
  // already newest-first, so a simple run-length walk keeps that order.
  let rows="",day=null;
  for(const s of sessions){
    if(s.date!==day){day=s.date;rows+=`<div class="al-day">${esc(actLogDay(day))}</div>`;}
    rows+=actLogRow(s);
  }
  const note=d.truncated
    ?`<div class="faint" style="margin-top:.5rem">Showing the ${d.sessions.length}
       most recent of ${d.session_count} sessions.</div>`:"";
  const filtered=ACTLOG_GAME
    ?`<span class="al-clear" onclick="actLogFilter(null)">${esc(ACTLOG_GAME)} ✕</span>`:"";

  dlg.innerHTML=`<h2 style="display:flex;align-items:center;gap:.6rem;margin-bottom:.2rem">
      ${avatar(d.user_id,d.display_name,d.avatar,30)}${esc(d.display_name)}
      <span class="faint" style="font-size:.8rem;font-weight:400">last ${d.window_days}d</span></h2>
    ${d.tracked?"":`<div class="faint" style="font-size:.8rem;margin-bottom:.6rem">
      Tracking is off for this member — this is the history recorded up to then.</div>`}
    <div class="al-sec">Most played</div>${chips}
    <div class="al-sec">Sessions ${filtered}</div>
    ${sessions.length?`<div class="al-list">${rows}</div>`
      :'<div class="faint">No sessions in this window.</div>'}
    ${note}
    <div class="dlg-actions"><button class="btn" onclick="editDlg.close()">Close</button></div>`;
}

// ---- sleep feed ----------------------------------------------------------
function fmtHours(h){if(h==null)return"—";const hr=Math.floor(h),m=Math.round((h-hr)*60);
  return hr+"h"+(m?" "+m+"m":"");}
// Render a night as a horizontal bar positioned on a 24h clock (local time),
// so consecutive nights line up visually. Wraps across midnight when needed.
function sleepBar(s){
  const start=new Date(s.start_local.replace(" ","T")), end=new Date(s.end_local.replace(" ","T"));
  const sh=isNaN(start)?0:start.getHours()+start.getMinutes()/60;
  const eh=isNaN(end)?sh:end.getHours()+end.getMinutes()/60;
  // Anchor the clock at 18:00 so a typical night sits in the middle, not split.
  const off=h=>((h-18+24)%24)/24*100;
  const a=off(sh); let w=(off(eh)-a+100)%100; if(w<=0)w=100;
  return `<div class="slot"><div class="slotbar" style="left:${a.toFixed(1)}%;width:${Math.max(2,w).toFixed(1)}%"
    title="${esc(s.start_local)} → ${esc(s.end_local)}"></div></div>`;
}
async function renderSleep(v){
  const d=await api(`/api/sleep?guild=${guild}&days=${WIN_DAYS}`);if(!d)return;
  const users=d.users||[];
  if(!users.length){
    v.innerHTML=pageHead("Sleep","Overnight offline periods estimated from presence")
      +emptyState("💤","No sleep estimates yet",
        "Start presence tracking from a member page or with /track start in Discord.");
    return;
  }
  // Most data first: nights tracked, then name.
  users.sort((a,b)=>(b.nights-a.nights)||a.display_name.localeCompare(b.display_name));
  v.innerHTML=`<div class="filters"><h2 style="margin:0">💤 Sleep
      <span class="faint">· estimated from presence · last ${d.window_days}d</span></h2>
      <span class="sp" style="flex:1"></span>${daySeg()}${searchBar("Search players…")}</div>
    <div class="act-grid">${users.map(sleepCard).join("")}</div>`;
}
// A row of summary chips (average, consistency, typical bed/wake, weekday vs
// weekend, sleep debt vs target) above the per-night list.
function sleepStatsHTML(st){
  if(!st||!st.nights)return"";
  const chip=(l,v,t)=>`<span class="schip"${t?` title="${esc(t)}"`:""}><b>${v}</b> ${l}</span>`;
  const debt=st.debt_hours||0;
  const debtStr=(debt>0?"−":"+")+fmtHours(Math.abs(debt));
  return `<div class="schips">
    ${chip("avg",fmtHours(st.avg_hours))}
    ${st.std_hours!=null?chip("±",fmtHours(st.std_hours),"consistency (std-dev of nightly hours)"):""}
    ${st.bedtime?chip("bed",st.bedtime,"typical bedtime"):""}
    ${st.wake?chip("wake",st.wake,"typical wake-up"):""}
    ${st.weekday_avg!=null?chip("wk",fmtHours(st.weekday_avg),"weekday average"):""}
    ${st.weekend_avg!=null?chip("we",fmtHours(st.weekend_avg),"weekend average"):""}
    ${chip("vs "+fmtHours(st.target_hours),debtStr,"sleep debt vs target ("+(debt>0?"under":"over")+")")}
  </div>`;
}
// Per-night mini bar chart, coloured by how much sleep that night got.
function sleepSpark(series,target){
  if(!series||!series.length)return"";
  const max=Math.max(target||8,...series.map(p=>p.hours));
  return `<div class="sspark">${series.map(p=>{
    const h=Math.max(6,Math.round(p.hours/max*100));
    const cls=p.hours<6?"low":(p.hours<7.5?"mid":"ok");
    return `<span class="sbar ${cls}" style="height:${h}%" title="${esc(p.date)}: ${fmtHours(p.hours)}"></span>`;
  }).join("")}</div>`;
}
function sleepCard(u){
  const offline=!["online","idle","dnd"].includes(u.status);
  const st=u.stats||{};
  const sessions=(u.sessions||[]).slice().reverse(); // newest first
  const rows=sessions.length?`<div class="slplist">${sessions.slice(0,10).map(s=>`<div class="slprow">
      ${sleepBar(s)}
      <div class="slpmeta"><div class="slpd">${esc(s.date)}</div>
      <div class="slpt faint">${esc(s.start_local.slice(11))}–${esc(s.end_local.slice(11))}</div></div>
      <div class="slph">${fmtHours(s.duration_hours)}</div></div>`).join("")}</div>`
    : '<div class="faint" style="font-size:.84rem">No sleep sessions detected in this window.</div>';
  return `<div class="act-card${offline?' offline-card':''}" data-search="${esc((u.display_name||'').toLowerCase())}">
    <div class="act-head">
      <span class="act-av">${avatar(u.user_id,u.display_name,u.avatar,44)}
        <span class="dot ${statusClass(u.status)}"></span></span>
      <div><div class="act-name"><a class="link" onclick="memberView('${u.user_id}')">${esc(u.display_name)}</a></div>
      <div class="act-sub">${u.nights} night${u.nights===1?"":"s"} · avg ${fmtHours(u.avg_hours)}</div></div>
    </div>
    ${sleepStatsHTML(st)}
    ${sleepSpark(st.series,st.target_hours)}
    ${rows}
  </div>`;
}

// ---- messages (Discord-style channel browser) ----------------------------
let msgChannel=null, BLACKLIST=[], MSG_CHANS={};
function roleName(id){const r=ALL_ROLES.find(r=>String(r.role_id)===String(id));return r&&r.name;}
// Turn raw Discord mention tokens in message content into readable, escaped
// chips: <@id>/<@!id> → @name, <@&id> → @role, <#id> → #channel. Everything
// outside a token is HTML-escaped so message text can never inject markup.
function renderContent(text){
  if(text==null)return"";
  let out="",last=0,m;const re=/<(#|@[!&]?)(\d+)>/g;
  while((m=re.exec(text))){
    out+=esc(text.slice(last,m.index));
    const kind=m[1],id=m[2];let label;
    if(kind==="#")label="#"+(MSG_CHANS[id]||"channel");
    else if(kind==="@&")label="@"+(roleName(id)||"role");
    else label="@"+(((AV[id]||{}).name)||"unknown");
    out+=`<span class="mention" title="${esc(id)}">${esc(label)}</span>`;
    last=re.lastIndex;
  }
  out+=esc(text.slice(last));
  return out;
}
function msgGroups(msgs){
  // msgs are chronological (oldest→newest); group consecutive messages from the
  // same author within a few minutes, like Discord collapses a sender's run.
  const GAP=7*60*1000, groups=[];
  for(const m of msgs){
    const t=Date.parse(m.at), g=groups[groups.length-1];
    if(g&&g.user_id===m.user_id&&(t-g.lastT)<=GAP){g.items.push(m);g.lastT=t;}
    else groups.push({user_id:m.user_id,name:m.display_name,avatar:m.avatar,at:m.at,lastT:t,items:[m]});
  }
  return groups;
}
function mediaHtml(media){
  if(!media||!media.length)return"";
  return `<div class="dc-media">${media.map(m=>
    m.kind==="video"
    ? `<video class="dc-att" src="${esc(m.url)}" autoplay loop muted playsinline></video>`
    : m.kind==="image"
    ? `<img class="dc-att" src="${esc(m.url)}" loading="lazy" alt="" onclick="window.open('${esc(m.url)}','_blank')">`
    : `<a class="dc-file" href="${esc(m.url)}" target="_blank" rel="noopener">📎 ${esc(m.name||"file")}</a>`
  ).join("")}</div>`;
}
function renderChat(msgs){
  if(!msgs||!msgs.length)return '<div class="faint" style="padding:1.2rem">No messages logged in this channel.</div>';
  return msgGroups(msgs).map(g=>`<div class="dc-grp">
    ${avatar(g.user_id,g.name,g.avatar,40)}
    <div class="dc-gb"><div class="dc-gh"><a class="dc-au link" onclick="memberView('${g.user_id}')">${esc(g.name||g.user_id)}</a>
      <span class="dc-ts">${fmtTs(g.at)}</span>
      <button class="dc-bl" title="Blacklist this user from message logging" onclick="blacklistUser('${g.user_id}')">🚫</button></div>
    ${g.items.map(it=>`<div class="dc-line${it.deleted?' dc-del':''}">${it.content?`<div class="dc-msg">${renderContent(it.content)}${it.edited?' <span class="dc-tag">(edited)</span>':''}</div>`:""}${mediaHtml(it.media)}${it.deleted?'<span class="dc-tag dc-delm">🗑️ deleted</span>':''}</div>`).join("")}</div></div>`).join("");
}
// Open the blacklist dialog pre-filled with a user picked straight from a chat
// message — no need to hunt down their numeric ID.
function blacklistUser(uid){
  openBlacklist();
  const i=document.getElementById("bl_uid");if(i)i.value=uid;
  const r=document.getElementById("bl_reason");if(r)r.focus();
}
async function openChan(cid){
  msgChannel=cid;let name="";
  document.querySelectorAll(".dc-chan").forEach(b=>{const on=b.dataset.cid===cid;
    b.classList.toggle("active",on);if(on)name=b.dataset.cn||"";});
  const head=document.getElementById("dcHead");if(head)head.innerHTML=`<span class="dc-hash">#</span>${esc(name)}`;
  const chat=document.getElementById("dcChat");if(chat)chat.innerHTML=spinner();
  let d;
  try{d=await api(`/api/messages/log?guild=${guild}&channel=${cid}`);}
  catch(e){if(chat)chat.innerHTML=errorState(e,`openChan('${cid}')`);return;}
  if(!d||!chat)return;
  chat.innerHTML=renderChat(d.messages);chat.scrollTop=chat.scrollHeight;
}
async function renderMessages(v){
  const d=await api(`/api/messages/channels?guild=${guild}`);if(!d)return;
  const chans=d.channels||[];BLACKLIST=d.blacklist||[];
  MSG_CHANS={};chans.forEach(c=>{MSG_CHANS[c.channel_id]=c.channel_name;});
  if(!chans.length){
    v.innerHTML=pageHead("Messages","Logged Discord history by channel")
      +emptyState("💬","No messages logged yet",
        "Messages appear after someone chats; recent history is also scanned when the bot starts.",
        blButton());
    return;
  }
  const sel=chans.find(c=>c.channel_id===msgChannel)||chans[0];msgChannel=sel.channel_id;
  v.innerHTML=`${pageHead("Messages","Logged Discord history by channel")}<div class="dc">
    <div class="dc-side">
      <div class="dc-side-h"><span>Channels</span>${blButton()}</div>
      <div class="dc-chans">${chans.map(c=>`<button class="dc-chan${c.channel_id===msgChannel?' active':''}"
        data-cid="${c.channel_id}" data-cn="${esc(c.channel_name||'')}" onclick="openChan('${c.channel_id}')">
        <span class="dc-hash">#</span><span class="dc-cn">${esc(c.channel_name||c.channel_id)}</span>
        <span class="dc-cc">${c.count}</span></button>`).join("")}</div>
    </div>
    <div class="dc-main"><div class="dc-main-h" id="dcHead"></div>
      <div class="dc-chat" id="dcChat">${spinner()}</div></div>
  </div>`;
  await openChan(sel.channel_id);
}
function blButton(){return `<button class="btn sm" onclick="openBlacklist()">🚫 Blacklist${BLACKLIST.length?` (${BLACKLIST.length})`:""}</button>`;}
function openBlacklist(){
  const dlg=document.getElementById("editDlg");
  dlg.innerHTML=`<h3 style="margin:.2rem 0 .6rem">🚫 Message-log blacklist</h3>
    <p class="faint" style="font-size:.82rem;margin:.2rem 0 .9rem">Blacklisted members can no longer add anything to the
      bot (lifts, calories, commands) — their chat is still logged and kept. The bot posts a public message pinging them with the reason.</p>
    <div class="bl-list">${BLACKLIST.length?BLACKLIST.map(b=>`<div class="bl-row">
        <div><b>${esc(b.display_name)}</b> <span class="faint">${esc(b.user_id)}</span>
          <div class="faint" style="font-size:.8rem">${b.reason?esc(b.reason):"<i>no reason given</i>"}</div></div>
        <button class="btn sm" onclick="removeBlacklist('${b.user_id}')">Remove</button></div>`).join(""):'<div class="faint">Nobody blacklisted.</div>'}</div>
    <div class="bl-form">
      <input id="bl_uid" class="search" placeholder="User ID" inputmode="numeric">
      <input id="bl_reason" class="search" placeholder="Reason (shown publicly)">
      <button class="btn primary" onclick="addBlacklist()">Blacklist</button></div>
    <div class="dlg-actions"><button class="btn" onclick="editDlg.close()">Close</button></div>`;
  dlg.showModal();
}
async function addBlacklist(){
  const uid=document.getElementById("bl_uid").value.trim();
  const reason=document.getElementById("bl_reason").value.trim();
  if(!uid){toast("User ID required");return;}
  const r=await post("/api/blacklist/add",{guild,user_id:uid,reason});
  if(r&&r.ok){toast(r.announced?"Blacklisted ✓ (announced in chat)":"Blacklisted ✓");
    document.getElementById("editDlg").close();render();}
  else toast("Failed");
}
async function removeBlacklist(uid){
  const r=await post("/api/blacklist/remove",{guild,user_id:uid});
  toast(r&&r.ok?"Removed ✓":"Failed");document.getElementById("editDlg").close();render();
}

// ---- voice (who's in VC + join/leave log) --------------------------------
const VC_EV={join:["📥","joined"],leave:["📤","left"],move:["🔀","moved to"]};
async function renderVoice(v){
  const d=await api(`/api/voice?guild=${guild}`);if(!d)return;
  v.innerHTML=`<div class="filters"><h2 style="margin:0">🔊 Voice
      <span class="faint" id="vcCount"></span></h2>
      <span class="live-dot" title="Live — refreshes automatically"></span>
      <span class="sp" style="flex:1"></span>${searchBar("Search members…")}</div>
    <div id="vcBody">${voiceBodyHTML(d)}</div>`;
  setVcCount(d);
  setLive(liveVoice);
}
async function liveVoice(){
  if(tab!=="voice"){clearLive();return;}
  let d;try{d=await api(`/api/voice?guild=${guild}`);}catch(e){return;}
  if(!d||tab!=="voice")return;
  const body=document.getElementById("vcBody");if(!body)return;
  body.innerHTML=voiceBodyHTML(d);setVcCount(d);filterTable(SEARCH);
}
function setVcCount(d){const el=document.getElementById("vcCount");if(!el)return;
  const inVc=(d.occupancy||[]).reduce((n,c)=>n+(c.members||[]).length,0);
  el.textContent=`· ${inVc} in voice now`;}
function voiceBodyHTML(d){
  const occ=d.occupancy||[], events=d.events||[], totals=d.totals||[];
  const occHtml=occ.length?occ.map(c=>`<div class="vc-chan">
      <div class="vc-chan-h">🔊 ${esc(c.channel_name)} <span class="faint">· ${(c.members||[]).length}</span></div>
      <div class="vc-members">${(c.members||[]).map(m=>`<div class="vc-mem" data-search="${esc((m.display_name||'').toLowerCase())}">
        ${avFor(m.user_id,m.display_name,26)}
        <a class="link" onclick="memberView('${m.user_id}')">${esc(m.display_name)}</a>
        ${m.streaming?'<span class="vc-ic" title="Streaming">🔴</span>':""}
        ${m.self_deaf?'<span class="vc-ic" title="Deafened">🔇</span>':(m.self_mute?'<span class="vc-ic" title="Muted">🔈</span>':"")}
      </div>`).join("")}</div></div>`).join("")
    : '<div class="faint">Nobody is in a voice channel right now.</div>';
  // 7-day per-user totals. Muted/deafened carry the % of in-call; a zero shows
  // "—" so an unmuted/undeafened member doesn't read as a sub-minute stint. The
  // <table> lets the tab's search box filter rows by member name for free.
  const durCell=(sec,whole,withPct)=>!sec?'<span class="faint">—</span>'
    :`${fmtPlaytime(sec)}${withPct?` <span class="faint">(${Math.round(pct(sec,whole))}%)</span>`:""}`;
  const totHtml=totals.length?`<div class="tcard"><table><thead><tr>
      <th>Member</th><th>In call</th><th>Active</th><th>Muted</th><th>Deafened</th></tr></thead>
    <tbody>${totals.map(t=>`<tr>
      <td>${who(t.user_id,t.display_name)}</td>
      <td>${fmtPlaytime(t.in_call)}</td>
      <td>${durCell(t.active)}</td>
      <td>${durCell(t.muted,t.in_call,true)}</td>
      <td>${durCell(t.deafened,t.in_call,true)}</td></tr>`).join("")}</tbody></table></div>`
    : '<div class="faint">No voice time in the last 7 days.</div>';
  const logHtml=events.length?events.map(e=>{const m=VC_EV[e.event]||["•",e.event];
    return `<div class="vc-ev" data-search="${esc((e.display_name||'').toLowerCase())}">${avFor(e.user_id,e.display_name,24)}
      <span class="vc-ev-who"><a class="link" onclick="memberView('${e.user_id}')">${esc(e.display_name)}</a></span>
      <span class="vc-ev-act">${m[0]} ${m[1]}${e.channel?` <span class="vc-ch">🔊 ${esc(e.channel)}</span>`:""}</span>
      <span class="vc-ev-ts">${fmtTs(e.at)}</span></div>`;}).join("")
    : '<div class="faint">No voice activity logged yet.</div>';
  return `<div class="vc-grid">${occHtml}</div>
    <h3 style="margin:1.4rem 0 .6rem">Voice time (7 days)</h3>
    ${totHtml}
    <h3 style="margin:1.4rem 0 .6rem">Recent voice activity</h3>
    <div class="vc-log">${logHtml}</div>`;
}

// "/" jumps to the current page's search box, matching the convention used by
// Discord and many admin tools. Never steal the key while someone is typing.
window.addEventListener("keydown",event=>{
  if(event.key!=="/"||event.ctrlKey||event.metaKey||event.altKey)return;
  const tag=(document.activeElement&&document.activeElement.tagName)||"";
  if(["INPUT","TEXTAREA","SELECT"].includes(tag))return;
  const search=document.querySelector("#view .search");
  if(search){event.preventDefault();search.focus();search.select();}
});

boot().catch(error=>{
  const view=document.getElementById("view");
  if(view)view.innerHTML=errorState(error,"location.reload()");
});
</script></body></html>"""


# Generated from DASHBOARD_TABS so the nav and the URL routes share one source
# of truth — a tab added to the nav but not to the routes would 404 on refresh.
#
# The precondition is checked BEFORE the replace. Asserting the marker is absent
# afterwards would be a tautology: it passes both when the injection worked and
# when the marker was renamed so the replace matched nothing. The latter ships a
# page whose script starts `const TABS=;` — a SyntaxError that stops every
# function in the file from being defined, leaving an empty nav over a permanent
# spinner on every route.
_TABS_MARKER = "/*TABS*/"
assert DASHBOARD_HTML.count(_TABS_MARKER) == 1, (
    f"expected exactly one {_TABS_MARKER} placeholder in DASHBOARD_HTML"
)
DASHBOARD_HTML = DASHBOARD_HTML.replace(
    _TABS_MARKER, json.dumps([list(t) for t in DASHBOARD_TABS], ensure_ascii=False),
)

