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
opaque session token held in-process and set it as an HttpOnly cookie; sessions
evaporate on restart, which is fine for an operator tool. There is no per-user
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
POST /api/lifts/delete, /api/lifts/edit, /api/calories/delete,
     /api/protein/delete   Edit endpoints (audited).
GET  /healthz          Liveness probe (no auth).
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from aiohttp import web

from . import presence

LOG = logging.getLogger("gymbot.webui")

SESSION_COOKIE = "gymdash_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600  # a week

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
# (guild_id, user_id, reason|None, actor_name) -> result dict — posts a public
# chat message pinging the blacklisted user with the reason.
BlacklistAnnouncer = Callable[[int, int, "str | None", str], Awaitable[dict]]
# (guild_id) -> list of {channel_id, channel_name, members:[…]} currently in VC.
VoiceSnapshotHandler = Callable[[int], Awaitable[list[dict]]]
# (guild_id, user_id, start, actor_name) -> result dict — start/stop recording a
# member's presence + activity (the dashboard's "Presence tracking" control).
PresenceTrackHandler = Callable[[int, int, bool, str], Awaitable[dict]]


class _Sessions:
    """Tiny in-process session store: token -> expiry epoch."""

    def __init__(self) -> None:
        self._store: dict[str, float] = {}

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        self._store[token] = time.time() + SESSION_TTL_SECONDS
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        exp = self._store.get(token)
        if exp is None:
            return False
        if exp < time.time():
            self._store.pop(token, None)
            return False
        return True

    def drop(self, token: str | None) -> None:
        if token:
            self._store.pop(token, None)


def build_app(
    *,
    db,
    password: str,
    resync: ResyncHandler | None = None,
    today_window: Callable[[], tuple[str, str]] | None = None,
    list_channels: ChannelsHandler | None = None,
    invite_user: InviteHandler | None = None,
    set_member_role: RoleHandler | None = None,
    member_moderation: ModerationHandler | None = None,
    remove_timeout: TimeoutHandler | None = None,
    announce_blacklist: BlacklistAnnouncer | None = None,
    voice_snapshot: VoiceSnapshotHandler | None = None,
    presence_track: PresenceTrackHandler | None = None,
    presence_enabled: bool = False,
) -> web.Application:
    """Construct the dashboard aiohttp application.

    ``db`` is the shared :class:`app.db.Database`. ``password`` is the shared
    login secret. ``resync`` (optional) re-pulls member/role state from Discord.
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
    """
    sessions = _Sessions()

    def _today() -> tuple[str, str]:
        if today_window is not None:
            return today_window()
        from datetime import datetime, timedelta, timezone
        start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        return start.isoformat(), (start + timedelta(days=1)).isoformat()

    # ---- auth helpers ----------------------------------------------------

    def _authed(request: web.Request) -> bool:
        return sessions.valid(request.cookies.get(SESSION_COOKIE))

    def _require(request: web.Request) -> None:
        if not _authed(request):
            raise web.HTTPUnauthorized(text="login required")

    def _actor(request: web.Request) -> str:
        # No per-user identity; attribute edits to the requesting IP so the
        # audit trail at least distinguishes operators on a shared deployment.
        peer = request.headers.get("X-Forwarded-For") or (
            request.remote or "?"
        )
        return f"web:{peer.split(',')[0].strip()}"

    def _guild_id(request: web.Request) -> int:
        try:
            return int(request.query["guild"])
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text="missing or invalid ?guild")

    # ---- auth routes -----------------------------------------------------

    async def login_get(request: web.Request) -> web.Response:
        if _authed(request):
            raise web.HTTPFound("/")
        return web.Response(text=LOGIN_HTML, content_type="text/html")

    async def login_post(request: web.Request) -> web.Response:
        data = await request.post()
        supplied = str(data.get("password", ""))
        # Constant-time compare so the form can't be used as a timing oracle.
        if password and hmac.compare_digest(supplied, password):
            token = sessions.create()
            resp = web.HTTPFound("/")
            resp.set_cookie(
                SESSION_COOKIE, token, httponly=True, samesite="Lax",
                max_age=SESSION_TTL_SECONDS,
            )
            LOG.info("Dashboard login from %s", _actor(request))
            return resp
        body = LOGIN_HTML.replace(
            "<!--ERR-->", '<p class="err">Wrong password.</p>'
        )
        return web.Response(text=body, content_type="text/html", status=401)

    async def logout_post(request: web.Request) -> web.Response:
        sessions.drop(request.cookies.get(SESSION_COOKIE))
        resp = web.HTTPFound("/login")
        resp.del_cookie(SESSION_COOKIE)
        return resp

    async def index(request: web.Request) -> web.Response:
        if not _authed(request):
            raise web.HTTPFound("/login")
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

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
            {"weight_kg": r["weight_kg"], "at": r["recorded_at"]}
            for r in db.bodyweight_history(gid, uid, limit=400)
        ]
        return web.json_response({
            "member": _member_dict(member) if member else {"user_id": str(uid)},
            "roles": [_role_dict(r) for r in roles],
            "overview": _stringify_ids(overview),
            "audit": audit,
            "strava_linked": db.get_strava_account(uid) is not None,
            "revo_linked": db.get_revo_account(uid) is not None,
            "presence_tracked": db.presence_is_tracked(gid, uid),
            "presence_tracking_available": bool(
                presence_enabled and presence_track is not None
            ),
            "nutrition": {
                "calorie_goal": (
                    cal_goal["daily_target_kcal"] if cal_goal else None
                ),
                "calorie_today": cal_today,
                "protein_goal": (
                    pro_goal["daily_target_g"] if pro_goal else None
                ),
                "protein_today": pro_today,
            },
            "foods": [_food_dict(r) for r in db.calorie_food_list(gid, uid)],
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
        return web.json_response({"foods": [_food_dict(r) for r in rows]})

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

    async def api_activity(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        days = _clamp_int(request.query.get("days"), 30, 1, 90)
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=days)
        users = []
        for row in db.presence_track_list(gid):
            uid = int(row["user_id"])
            member = db.get_member(gid, uid)
            pres = db.presence_current(gid, uid)
            cur = db.activity_current(gid, uid)
            imgmap = db.activity_image_map(gid, uid)
            events = [
                (r["activity"], r["at"])
                for r in db.activity_events_for(gid, uid, since=since)
            ]
            totals = presence.summarize_activities(events, since, now)
            top = [
                {"name": nm, "seconds": round(secs), "image": imgmap.get(nm)}
                for nm, secs in list(totals.items())[:6]
            ]
            current_game = None
            if cur and cur["activity"]:
                current_game = {
                    "name": cur["activity"],
                    "image": cur["image_url"] or imgmap.get(cur["activity"]),
                    "since": cur["at"],
                }
            users.append({
                "user_id": str(uid),
                "display_name": (
                    member["display_name"] if member else str(uid)
                ),
                "avatar": member["avatar"] if member else None,
                "status": pres["status"] if pres else None,
                "status_at": pres["at"] if pres else None,
                "current_game": current_game,
                "top_games": top,
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
            }
            for r in rows
        ]
        return web.json_response({"messages": messages})

    async def api_voice(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        occupancy = await voice_snapshot(gid) if voice_snapshot else []
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
        return web.json_response({"occupancy": occupancy, "events": events})

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

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.add_routes([
        web.get("/login", login_get),
        web.post("/login", login_post),
        web.post("/logout", logout_post),
        web.get("/logo.svg", logo),
        web.get("/", index),
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
        web.get("/api/messages/channels", api_messages_channels),
        web.get("/api/messages/log", api_messages_log),
        web.get("/api/voice", api_voice),
        web.post("/api/blacklist/add", api_blacklist_add),
        web.post("/api/blacklist/remove", api_blacklist_remove),
        web.post("/api/lifts/delete", api_lift_delete),
        web.post("/api/lifts/edit", api_lift_edit),
        web.post("/api/calories/delete", api_calorie_delete),
        web.post("/api/protein/delete", api_protein_delete),
        web.post("/api/foods/set", api_food_set),
        web.post("/api/foods/delete", api_food_delete),
        web.post("/api/resync", api_resync),
        web.get("/api/channels", api_channels),
        web.post("/api/invite", api_invite),
        web.post("/api/member/role", api_member_role),
        web.get("/api/member/moderation", api_member_moderation),
        web.post("/api/member/untimeout", api_member_untimeout),
        web.post("/api/member/track", api_member_track),
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


def _food_dict(r) -> dict:
    return {
        "name": r["name"],
        "display": r["display"],
        "kcal": r["kcal"],
        "protein_g": r["protein_g"],
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
body{font-family:'Inter',system-ui,-apple-system,sans-serif;margin:0;height:100vh;
display:flex;align-items:center;justify-content:center;color:#e6edf3;
background:radial-gradient(1200px 600px at 50% -10%,#1b2540 0%,#0b0e14 55%)}
.card{background:rgba(22,27,34,.7);backdrop-filter:blur(12px);
border:1px solid rgba(255,255,255,.08);border-radius:18px;
padding:2.5rem 2.25rem;width:340px;box-shadow:0 24px 60px rgba(0,0,0,.5)}
.brand{display:flex;align-items:center;gap:.7rem;margin-bottom:1.75rem}
.brand img{width:44px;height:44px}
.brand b{font-size:1.25rem;background:linear-gradient(90deg,#a5b4fc,#67e8f9);
-webkit-background-clip:text;background-clip:text;color:transparent}
label{font-size:.78rem;color:#8b949e;text-transform:uppercase;letter-spacing:.05em}
input{width:100%;padding:.7rem .8rem;margin:.4rem 0 1.1rem;font-size:1rem;
background:#0d1117;border:1px solid #30363d;border-radius:10px;color:#e6edf3}
input:focus{outline:none;border-color:#6366f1;box-shadow:0 0 0 3px #6366f133}
button{width:100%;padding:.75rem;border:0;border-radius:10px;font-size:1rem;
font-weight:600;cursor:pointer;color:#fff;
background:linear-gradient(90deg,#6366f1,#22d3ee)}
button:hover{filter:brightness(1.08)}
.err{color:#f85149;margin:0 0 .75rem;font-size:.88rem}
.sub{color:#6e7681;font-size:.78rem;margin-top:1.25rem;text-align:center}
</style></head><body>
<form class="card" method="post" action="/login">
<div class="brand"><img src="/logo.svg" alt=""><b>Gym Dashboard</b></div>
<label>Password</label>
<input type="password" name="password" autofocus autocomplete="current-password">
<!--ERR-->
<button type="submit">Sign in</button>
<p class="sub">Operator access only</p>
</form></body></html>"""


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
}
*{box-sizing:border-box}
body{font-family:'Inter',system-ui,-apple-system,sans-serif;margin:0;color:var(--text);
background:radial-gradient(1100px 520px at 100% -5%,#172036 0%,rgba(11,14,20,0) 60%),
          radial-gradient(900px 500px at -5% 0%,#16242b 0%,rgba(11,14,20,0) 55%),var(--bg);
min-height:100vh}
a{color:inherit}
::-webkit-scrollbar{height:10px;width:10px}
::-webkit-scrollbar-thumb{background:#2a313c;border-radius:6px}

/* header */
header{display:flex;align-items:center;gap:.85rem;padding:.7rem 1.4rem;
background:rgba(13,17,23,.72);backdrop-filter:blur(12px);
border-bottom:1px solid var(--line);position:sticky;top:0;z-index:20}
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
.btn.primary{background:var(--accent);border:0;color:#fff;font-weight:600}
.btn.primary:hover{filter:brightness(1.08)}
.btn.danger{color:#ff9a96;border-color:#5c2b2b}
.btn.danger:hover{background:#3a1d1d}
form.inline{margin:0}

/* nav */
nav{display:flex;gap:.3rem;padding:.6rem 1.4rem;flex-wrap:wrap;
border-bottom:1px solid var(--line);background:rgba(13,17,23,.4)}
nav a{display:flex;align-items:center;gap:.4rem;padding:.4rem .85rem;border-radius:9px;
cursor:pointer;color:var(--muted);font-size:.92rem;font-weight:500;transition:.15s}
nav a:hover{color:var(--text);background:#ffffff0a}
nav a.active{color:#fff;background:linear-gradient(90deg,#6366f133,#22d3ee22);
box-shadow:inset 0 0 0 1px #6366f155}

main{padding:1.5rem;max-width:1240px;margin:0 auto}
h2{font-size:1.15rem;margin:.2rem 0 1.1rem;font-weight:650}
.muted{color:var(--muted)}.faint{color:var(--faint)}

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

/* table card */
.tcard{background:var(--panel);border:1px solid var(--line);border-radius:14px;
overflow:hidden}
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
border-radius:16px;padding:1.5rem;min-width:340px;box-shadow:0 30px 80px #000a}
dialog::backdrop{background:#0009;backdrop-filter:blur(2px)}
dialog h2{margin-top:0}
dialog label{display:block;font-size:.74rem;color:var(--muted);
text-transform:uppercase;letter-spacing:.05em;margin:.7rem 0 .25rem}
dialog input{width:100%;padding:.6rem .7rem;background:#0d1117;border:1px solid var(--line);
border-radius:9px;color:var(--text);font:inherit}
.dlg-actions{display:flex;gap:.6rem;justify-content:flex-end;margin-top:1.4rem}

.toast{position:fixed;bottom:1.4rem;right:1.4rem;padding:.7rem 1.1rem;border-radius:11px;
background:var(--panel2);border:1px solid var(--line);box-shadow:0 12px 30px #0007;
opacity:0;transform:translateY(8px);transition:.25s;pointer-events:none;z-index:50}
.toast.show{opacity:1;transform:none}
.spin{display:inline-block;width:34px;height:34px;border:3px solid #2a313c;
border-top-color:var(--indigo);border-radius:50%;animation:sp 1s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.center{display:flex;justify-content:center;padding:3rem}

/* search */
.search{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:.45rem .7rem .45rem 2rem;color:var(--text);font:inherit;min-width:220px;
background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' fill='none' stroke='%238b949e' stroke-width='2'%3E%3Ccircle cx='6' cy='6' r='4.5'/%3E%3Cpath d='M10 10l3 3'/%3E%3C/svg%3E");
background-repeat:no-repeat;background-position:.6rem center}
.search:focus{outline:none;border-color:#6366f1}

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
.tg .barwrap{height:5px;background:#0d1117;border-radius:4px;overflow:hidden;margin-top:3px}
.tg .barfill{height:100%;background:linear-gradient(90deg,#6366f1,#22d3ee)}
.offline-card{opacity:.6}
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
</style></head><body>
<header>
  <div class="brand"><img src="/logo.svg" alt=""><b>Gym Dashboard</b></div>
  <div class="gselect"><select id="guild" onchange="onGuild()"></select></div>
  <span class="sp"></span>
  <button class="btn" onclick="resync()" title="Re-pull members & roles from Discord">↻ Sync</button>
  <form class="inline" method="post" action="/logout"><button class="btn">Logout</button></form>
</header>
<nav id="nav"></nav>
<main id="view"><div class="center"><div class="spin"></div></div></main>
<dialog id="editDlg"></dialog>
<div class="toast" id="toast"></div>
<script>
const TABS=[["overview","📊"],["members","👥"],["activity","🎮"],["messages","💬"],["voice","🔊"],["roles","🛡️"],
  ["leaderboard","🏆"],["audit","📜"],["lifts","🏋️"],["calories","🔥"],["protein","🥩"]];
const PALETTE=["#6366f1","#22d3ee","#f59e0b","#ef4444","#10b981","#ec4899","#8b5cf6","#14b8a6"];
const ACTION_LABEL={
  role_add:"➕ role added",role_remove:"➖ role removed",role_create:"🆕 role created",
  role_delete:"🗑️ role deleted",role_rename:"✏️ role renamed",
  join:"📥 joined",leave:"📤 left",nick_change:"🏷️ nickname",username_change:"🏷️ username",
  kick:"👢 kicked",ban:"🔨 banned",unban:"♻️ unbanned",invite_create:"✉️ invite sent",
  timeout_remove:"⏳ timeout removed",
  message_blacklist_add:"🚫 msg blacklisted",message_blacklist_remove:"♻️ msg unblacklisted",
  lift_add:"🏋️ lift logged",lift_undo:"↩️ lift undone",lift_delete:"🗑️ lift deleted",lift_edit:"✏️ lift edited",
  calorie_add:"🔥 calories logged",calorie_undo:"↩️ calories undone",calorie_delete:"🗑️ calories deleted",
  protein_add:"🥩 protein logged",protein_undo:"↩️ protein undone",protein_delete:"🗑️ protein deleted",
  food_set:"🍴 food saved",food_delete:"🗑️ food removed",
  goal_set:"🎯 goal set",goal_remove:"🎯 goal removed",
  calorie_goal_set:"🎯 calorie goal",calorie_goal_remove:"🛑 calorie tracking off",
  protein_goal_set:"🎯 protein goal",protein_goal_remove:"🛑 protein tracking off",
  bodyweight_log:"⚖️ bodyweight"};
function actionLabel(a){return ACTION_LABEL[a]||esc(a);}
let guild=null,tab="overview",AV={},dataUserFilter=null,auditCat="",currentMember=null,lbEquip="",currentFoods=[],auditOffset=0,auditRows=[],ALL_ROLES=[];

function searchBar(ph){return `<input class="search" placeholder="${ph||'Search…'}" oninput="filterTable(this.value)">`;}
function filterTable(term){term=(term||"").toLowerCase();
  document.querySelectorAll("#view tbody tr").forEach(tr=>{
    tr.style.display=tr.textContent.toLowerCase().includes(term)?"":"none";});}
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

function toast(m){const t=document.getElementById("toast");t.textContent=m;
  t.classList.add("show");setTimeout(()=>t.classList.remove("show"),2200);}
function esc(s){return(s==null?"":String(s)).replace(/[&<>"']/g,
  c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
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

async function api(p){const r=await fetch(p);if(r.status===401){location.href="/login";return null;}
  if(!r.ok)throw new Error(await r.text());return r.json();}
async function post(p,b){const r=await fetch(p,{method:"POST",
  headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});
  if(r.status===401){location.href="/login";return null;}return r.json();}
function spinner(){return '<div class="center"><div class="spin"></div></div>';}

function renderNav(){document.getElementById("nav").innerHTML=
  TABS.map(([t,ic])=>`<a class="${t===tab?'active':''}" onclick="go('${t}')">${ic} ${t[0].toUpperCase()+t.slice(1)}</a>`).join("");}
function go(t){tab=t;dataUserFilter=null;renderNav();render();}
async function onGuild(){guild=document.getElementById("guild").value;await loadAvatars();await loadRoles();render();}

async function loadAvatars(){AV={};try{const d=await api(`/api/members?guild=${guild}`);
  if(d)for(const m of d.members)AV[m.user_id]={avatar:m.avatar,name:m.display_name};}catch(e){}}
async function loadRoles(){try{const d=await api(`/api/roles?guild=${guild}`);
  ALL_ROLES=(d&&d.roles)||[];}catch(e){ALL_ROLES=[];}}

async function boot(){
  const g=await api("/api/guilds");if(!g)return;
  const sel=document.getElementById("guild");
  if(!g.guilds.length){document.getElementById("view").innerHTML=
    '<div class="empty">No guilds tracked yet. Once the bot syncs a server it appears here.</div>';return;}
  sel.innerHTML=g.guilds.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join("");
  guild=g.guilds[0].id;renderNav();await loadAvatars();await loadRoles();render();
}
async function resync(){if(!guild)return;toast("Syncing…");
  const r=await post("/api/resync",{guild});await loadAvatars();
  toast(r&&r.ok?"Synced ✓":"Sync unavailable");render();}

async function render(){
  const v=document.getElementById("view");
  if(!guild){v.innerHTML='<div class="empty">Pick a guild.</div>';return;}
  v.innerHTML=spinner();
  try{
    if(tab==="overview")return renderOverview(v);
    if(tab==="members")return renderMembers(v);
    if(tab==="activity")return renderActivity(v);
    if(tab==="messages")return renderMessages(v);
    if(tab==="voice")return renderVoice(v);
    if(tab==="roles")return renderRoles(v);
    if(tab==="leaderboard")return renderLeaderboard(v);
    if(tab==="audit")return renderAudit(v);
    if(["lifts","calories","protein"].includes(tab))return renderData(v,tab);
  }catch(e){v.innerHTML='<div class="empty">Error: '+esc(e.message)+'</div>';}
}
function stat(n,l){return `<div class="stat"><div class="n">${esc(n)}</div><div class="l">${esc(l)}</div></div>`;}

async function renderOverview(v){
  const d=await api(`/api/overview?guild=${guild}`);if(!d)return;const t=d.totals||{};
  v.innerHTML=`<div class="cards">
    ${stat(d.member_count,"Members")}${stat(d.role_count,"Roles")}
    ${stat(t.total_lifts||0,"Lifts")}${stat(t.lifters||0,"Lifters")}
    ${stat(t.unique_equip||0,"Exercises")}</div>
    <h2>Recent activity</h2>${auditTable(d.recent_audit)}`;
}

async function renderMembers(v){
  const d=await api(`/api/members?guild=${guild}`);if(!d)return;
  if(!d.members.length){v.innerHTML='<div class="empty">No members synced yet. Hit ↻ Sync.</div>';return;}
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
  tab="members";renderNav();currentMember=uid;
  const v=document.getElementById("view");v.innerHTML=spinner();
  const d=await api(`/api/member?guild=${guild}&user=${uid}`);if(!d)return;
  const m=d.member,o=d.overview||{},L=o.lifts||{},cal=o.calories||{},pro=o.protein||{};
  const n=d.nutrition||{},foods=d.foods||[],goals=d.lift_goals||[],bw=d.bodyweights||[];
  currentFoods=foods;
  v.innerHTML=`<div class="crumb" onclick="go('members')">← Members</div>
    <div class="hero">${avatar(uid,m.display_name,m.avatar,72)}
      <div><h2>${esc(m.display_name||uid)}</h2>
      <div class="muted">${esc(m.username||"")}</div>
      <div class="chips">${d.strava_linked?'<span class="pill">🟧 Strava</span>':''}
        ${d.revo_linked?'<span class="pill">🟢 Revo</span>':''}
        ${m.present?'':'<span class="pill faint">left server</span>'}</div></div></div>
    <div class="cards">${stat(L.n||0,"Lifts")}${stat(L.equip||0,"Exercises")}
      ${stat(o.bodyweight?o.bodyweight.weight_kg+" kg":"—","Bodyweight")}
      ${stat(Math.round(cal.total||0),"kcal logged")}${stat(Math.round(pro.total||0),"g protein")}</div>
    <div class="grid2">
      <div class="box"><h3>Today's nutrition</h3>
        ${bar("Calories",n.calorie_today,n.calorie_goal," kcal",false)}
        ${bar("Protein",n.protein_today,n.protein_goal," g",true)}</div>
      <div class="box"><h3>Bodyweight trend</h3>${sparkline(bw)}</div>
    </div>
    <div class="grid2">
      <div class="box"><h3 style="display:flex;justify-content:space-between">Saved foods
        <a class="link" onclick="foodDialog('${uid}')">+ add</a></h3>
        ${foods.length?`<table><tbody>${foods.map((f,i)=>`<tr>
          <td><b>${esc(f.display)}</b></td><td>${Math.round(f.kcal)} kcal</td>
          <td>${f.protein_g!=null?Math.round(f.protein_g)+' g':'<span class="faint">—</span>'}</td>
          <td><div class="row-actions">
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

// ---- moderation (remove timeout) ----
async function loadModeration(uid){
  const box=document.getElementById("modbox");if(!box)return;
  let d;try{d=await api(`/api/member/moderation?guild=${guild}&user=${uid}`);}catch(e){box.remove();return;}
  if(!d||!d.ok){box.remove();return;}
  if(d.timed_out){
    box.classList.remove("faint");
    box.innerHTML=`<span class="pill" style="border-color:#5c2b2b">⏳ Timed out until ${fmtTs(d.timed_out_until)}</span>
      <button class="btn sm danger" ${d.can_moderate?'':'disabled title="Bot lacks Moderate Members or is outranked"'}
        onclick="removeTimeout('${uid}')">Remove timeout</button>`;
  }else{
    box.innerHTML='<span class="faint">No active timeout.</span>';
  }
}
async function removeTimeout(uid){
  const r=await post("/api/member/untimeout",{guild,user:uid});
  if(r&&r.ok){toast(r.changed===false?"Wasn't timed out":"Timeout removed ✓");memberView(uid);}
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
  if(!list.length){v.innerHTML='<div class="empty">No lifts logged yet.</div>';return;}
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
function go2(t,uid){dataUserFilter=uid;tab=t;renderNav();render();}

async function renderRoles(v){
  const d=await api(`/api/roles?guild=${guild}`);if(!d)return;
  if(!d.roles.length){v.innerHTML='<div class="empty">No roles synced yet. Hit ↻ Sync.</div>';return;}
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
  v.innerHTML=`<div class="filters"><div class="seg">${["","role","member","data"].map(c=>
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
  if(h>=1)return h+"h "+(m?m+"m":"");return m>=1?m+"m":"<1m";}
function gameTile(name,url,size,cls){
  const px=`width:${size}px;height:${size}px`;
  if(url)return `<img class="${cls}" style="${px}" src="${esc(url)}" alt="" loading="lazy"
    onerror="this.replaceWith(Object.assign(document.createElement('span'),
    {className:'${cls} game-tile',style:'${px};background:${idColor(name)}',textContent:'${esc((name||'?')[0]||'?').toUpperCase()}'}))">`;
  return `<span class="${cls} game-tile" style="${px};background:${idColor(name)}">${esc((name||'?')[0]||'?').toUpperCase()}</span>`;}

async function renderActivity(v){
  const d=await api(`/api/activity?guild=${guild}`);if(!d)return;
  const users=d.users||[];
  if(!users.length){v.innerHTML=`<div class="empty">No tracked users yet.<br>
    <span class="faint">Open a member and hit <b>Start tracking</b>, or use <code>/track start</code>.
    Needs <code>ENABLE_PRESENCE_TRACKING=true</code> + the Presence intent.</span></div>`;return;}
  // online first, then by current game, then name.
  users.sort((a,b)=>(STATUS_RANK[a.status]??3)-(STATUS_RANK[b.status]??3)
    || (b.current_game?1:0)-(a.current_game?1:0)
    || a.display_name.localeCompare(b.display_name));
  const online=users.filter(u=>["online","idle","dnd"].includes(u.status)).length;
  v.innerHTML=`<div class="filters"><h2 style="margin:0">🎮 Activity
      <span class="faint">· ${online}/${users.length} online · last ${d.window_days}d</span></h2>
      <span class="sp" style="flex:1"></span>${searchBar("Search players…")}</div>
    <div class="act-grid">${users.map(actCard).join("")}</div>`;
}
function actCard(u){
  const offline=!["online","idle","dnd"].includes(u.status);
  const maxSec=Math.max(1,...(u.top_games||[]).map(g=>g.seconds));
  const now=u.current_game?`<div class="now">${gameTile(u.current_game.name,u.current_game.image,48,"game-img")}
      <div class="meta"><div class="g">${esc(u.current_game.name)}</div>
      <div class="t">▶ playing now${u.current_game.since?" · since "+fmtTs(u.current_game.since):""}</div></div></div>`
    : `<div class="now"><div class="meta"><div class="g faint">Not playing anything</div>
      <div class="t">${u.status_at?"updated "+fmtTs(u.status_at):""}</div></div></div>`;
  const top=(u.top_games||[]).length?`<div class="top-games">${u.top_games.map(g=>`<div class="tg">
      ${gameTile(g.name,g.image,30,"gi")}
      <div class="nm">${esc(g.name)}<div class="barwrap"><div class="barfill" style="width:${Math.round(g.seconds/maxSec*100)}%"></div></div></div>
      <div class="pt">${fmtPlaytime(g.seconds)}</div></div>`).join("")}</div>`
    : '<div class="faint" style="font-size:.84rem">No games tracked in this window.</div>';
  return `<div class="act-card${offline?' offline-card':''}">
    <div class="act-head">
      <span class="act-av">${avatar(u.user_id,u.display_name,u.avatar,44)}
        <span class="dot ${statusClass(u.status)}"></span></span>
      <div><div class="act-name"><a class="link" onclick="memberView('${u.user_id}')">${esc(u.display_name)}</a></div>
      <div class="act-sub">${statusLabel(u.status)}</div></div>
    </div>
    ${now}
    <div><div class="act-sub" style="margin-bottom:.4rem">Most played</div>${top}</div>
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
  return `<div class="dc-media">${media.map(m=>m.kind==="video"
    ? `<video class="dc-att" src="${esc(m.url)}" autoplay loop muted playsinline></video>`
    : `<img class="dc-att" src="${esc(m.url)}" loading="lazy" alt="" onclick="window.open('${esc(m.url)}','_blank')">`
  ).join("")}</div>`;
}
function renderChat(msgs){
  if(!msgs||!msgs.length)return '<div class="faint" style="padding:1.2rem">No messages logged in this channel.</div>';
  return msgGroups(msgs).map(g=>`<div class="dc-grp">
    ${avatar(g.user_id,g.name,g.avatar,40)}
    <div class="dc-gb"><div class="dc-gh"><a class="dc-au link" onclick="memberView('${g.user_id}')">${esc(g.name||g.user_id)}</a>
      <span class="dc-ts">${fmtTs(g.at)}</span>
      <button class="dc-bl" title="Blacklist this user from message logging" onclick="blacklistUser('${g.user_id}')">🚫</button></div>
    ${g.items.map(it=>`${it.content?`<div class="dc-msg">${renderContent(it.content)}</div>`:""}${mediaHtml(it.media)}`).join("")}</div></div>`).join("");
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
  const d=await api(`/api/messages/log?guild=${guild}&channel=${cid}`);
  if(!d||!chat)return;
  chat.innerHTML=renderChat(d.messages);chat.scrollTop=chat.scrollHeight;
}
async function renderMessages(v){
  const d=await api(`/api/messages/channels?guild=${guild}`);if(!d)return;
  const chans=d.channels||[];BLACKLIST=d.blacklist||[];
  MSG_CHANS={};chans.forEach(c=>{MSG_CHANS[c.channel_id]=c.channel_name;});
  if(!chans.length){v.innerHTML=`<div class="empty">No messages logged yet.<br>
    <span class="faint">Messages are logged for every member once they chat
    (<code>ENABLE_MESSAGE_LOGGING</code>, on by default); the bot also back-scans recent history on startup.</span>
    <div style="margin-top:1rem">${blButton()}</div></div>`;return;}
  const sel=chans.find(c=>c.channel_id===msgChannel)||chans[0];msgChannel=sel.channel_id;
  v.innerHTML=`<div class="dc">
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
  const occ=d.occupancy||[], events=d.events||[];
  const inVc=occ.reduce((n,c)=>n+(c.members||[]).length,0);
  const occHtml=occ.length?occ.map(c=>`<div class="vc-chan">
      <div class="vc-chan-h">🔊 ${esc(c.channel_name)} <span class="faint">· ${(c.members||[]).length}</span></div>
      <div class="vc-members">${(c.members||[]).map(m=>`<div class="vc-mem">
        ${avFor(m.user_id,m.display_name,26)}
        <a class="link" onclick="memberView('${m.user_id}')">${esc(m.display_name)}</a>
        ${m.streaming?'<span class="vc-ic" title="Streaming">🔴</span>':""}
        ${m.self_deaf?'<span class="vc-ic" title="Deafened">🔇</span>':(m.self_mute?'<span class="vc-ic" title="Muted">🔈</span>':"")}
      </div>`).join("")}</div></div>`).join("")
    : '<div class="faint">Nobody is in a voice channel right now.</div>';
  const logHtml=events.length?events.map(e=>{const m=VC_EV[e.event]||["•",e.event];
    return `<div class="vc-ev">${avFor(e.user_id,e.display_name,24)}
      <span class="vc-ev-who"><a class="link" onclick="memberView('${e.user_id}')">${esc(e.display_name)}</a></span>
      <span class="vc-ev-act">${m[0]} ${m[1]}${e.channel?` <span class="vc-ch">🔊 ${esc(e.channel)}</span>`:""}</span>
      <span class="vc-ev-ts">${fmtTs(e.at)}</span></div>`;}).join("")
    : '<div class="faint">No voice activity logged yet.</div>';
  v.innerHTML=`<div class="filters"><h2 style="margin:0">🔊 Voice
      <span class="faint">· ${inVc} in voice now</span></h2>
      <span class="sp" style="flex:1"></span>
      <button class="btn" onclick="render()" title="Refresh">↻ Refresh</button></div>
    <div class="vc-grid">${occHtml}</div>
    <h3 style="margin:1.4rem 0 .6rem">Recent voice activity</h3>
    <div class="vc-log">${logHtml}</div>`;
}

boot();
</script></body></html>"""

