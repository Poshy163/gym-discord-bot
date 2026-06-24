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
import logging
import secrets
import time
from typing import Awaitable, Callable

from aiohttp import web

LOG = logging.getLogger("gymbot.webui")

SESSION_COOKIE = "gymdash_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600  # a week

# A callable the bot can inject so the dashboard can resync members/roles on
# demand (the "Refresh" button). Optional — None disables the button.
ResyncHandler = Callable[[int], Awaitable[bool]]


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
) -> web.Application:
    """Construct the dashboard aiohttp application.

    ``db`` is the shared :class:`app.db.Database`. ``password`` is the shared
    login secret. ``resync`` (optional) re-pulls member/role state from Discord.
    """
    sessions = _Sessions()

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
        return web.json_response({
            "member": _member_dict(member) if member else {"user_id": str(uid)},
            "roles": [_role_dict(r) for r in roles],
            "overview": _stringify_ids(overview),
            "audit": audit,
            "strava_linked": db.get_strava_account(uid) is not None,
            "revo_linked": db.get_revo_account(uid) is not None,
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

    async def api_resync(request: web.Request) -> web.Response:
        _require(request)
        gid = _guild_id(request)
        if resync is None:
            return web.json_response(
                {"ok": False, "error": "resync unavailable"}, status=503,
            )
        ok = await resync(gid)
        return web.json_response({"ok": bool(ok)})

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

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.add_routes([
        web.get("/login", login_get),
        web.post("/login", login_post),
        web.post("/logout", logout_post),
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
        web.post("/api/lifts/delete", api_lift_delete),
        web.post("/api/lifts/edit", api_lift_edit),
        web.post("/api/calories/delete", api_calorie_delete),
        web.post("/api/protein/delete", api_protein_delete),
        web.post("/api/resync", api_resync),
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


def _audit_dict(r) -> dict:
    return {
        "id": r["id"],
        "at": r["at"],
        "category": r["category"],
        "action": r["action"],
        "actor_id": str(r["actor_id"]) if r["actor_id"] else None,
        "actor_name": r["actor_name"],
        "subject_id": str(r["subject_id"]) if r["subject_id"] else None,
        "subject_name": r["subject_name"],
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


# ---- static HTML -----------------------------------------------------------

LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gym Dashboard — Login</title>
<style>
:root{color-scheme:dark}
body{font-family:system-ui,sans-serif;background:#0d1117;color:#e6edf3;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;
padding:2.5rem;width:320px;box-shadow:0 8px 30px rgba(0,0,0,.4)}
h1{margin:0 0 1.25rem;font-size:1.3rem}
input{width:100%;box-sizing:border-box;padding:.65rem;margin:.25rem 0 1rem;
background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#e6edf3;
font-size:1rem}
button{width:100%;padding:.7rem;background:#238636;border:0;border-radius:8px;
color:#fff;font-size:1rem;font-weight:600;cursor:pointer}
button:hover{background:#2ea043}
.err{color:#f85149;margin:.25rem 0 0;font-size:.9rem}
.sub{color:#7d8590;font-size:.8rem;margin-top:1rem;text-align:center}
</style></head><body>
<form class="card" method="post" action="/login">
<h1>🏋️ Gym Dashboard</h1>
<label>Password</label>
<input type="password" name="password" autofocus autocomplete="current-password">
<!--ERR-->
<button type="submit">Sign in</button>
<p class="sub">Operator access only.</p>
</form></body></html>"""


DASHBOARD_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gym Dashboard</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{font-family:system-ui,sans-serif;background:#0d1117;color:#e6edf3;margin:0}
header{display:flex;align-items:center;gap:1rem;padding:.75rem 1.25rem;
background:#161b22;border-bottom:1px solid #30363d;position:sticky;top:0;z-index:5}
header h1{font-size:1.05rem;margin:0}
header .sp{flex:1}
select,button,input{font:inherit;color:#e6edf3;background:#0d1117;
border:1px solid #30363d;border-radius:7px;padding:.4rem .55rem}
button{cursor:pointer;background:#21262d}
button:hover{background:#30363d}
button.danger{border-color:#5c2626}button.danger:hover{background:#5c2626}
nav{display:flex;gap:.25rem;padding:.5rem 1.25rem;background:#161b22;
border-bottom:1px solid #30363d;flex-wrap:wrap}
nav a{padding:.4rem .8rem;border-radius:7px;cursor:pointer;color:#9da7b3;
text-decoration:none;font-size:.92rem}
nav a.active{background:#1f6feb33;color:#e6edf3}
main{padding:1.25rem;max-width:1200px;margin:0 auto}
.cards{display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:1.25rem}
.stat{background:#161b22;border:1px solid #30363d;border-radius:10px;
padding:1rem 1.25rem;min-width:130px}
.stat .n{font-size:1.7rem;font-weight:700}
.stat .l{color:#7d8590;font-size:.8rem;text-transform:uppercase;letter-spacing:.04em}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th,td{text-align:left;padding:.5rem .6rem;border-bottom:1px solid #21262d}
th{color:#7d8590;font-weight:600;font-size:.78rem;text-transform:uppercase}
tr:hover td{background:#161b2255}
.pill{display:inline-block;padding:.1rem .5rem;border-radius:999px;
font-size:.78rem;border:1px solid #30363d;margin:1px}
.muted{color:#7d8590}
.cat-role{color:#d2a8ff}.cat-member{color:#7ee787}.cat-data{color:#79c0ff}
.row-actions{display:flex;gap:.35rem}
a.link{color:#58a6ff;cursor:pointer;text-decoration:none}
a.link:hover{text-decoration:underline}
.filters{display:flex;gap:.5rem;align-items:center;margin-bottom:1rem;flex-wrap:wrap}
.empty{color:#7d8590;padding:2rem;text-align:center}
h2{font-size:1.1rem;margin:.2rem 0 1rem}
dialog{background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:12px;
padding:1.5rem;min-width:320px}
dialog::backdrop{background:rgba(0,0,0,.6)}
dialog label{display:block;font-size:.8rem;color:#7d8590;margin:.6rem 0 .2rem}
dialog input{width:100%}
.dlg-actions{display:flex;gap:.5rem;justify-content:flex-end;margin-top:1.25rem}
.toast{position:fixed;bottom:1.25rem;right:1.25rem;background:#1f6feb;
color:#fff;padding:.7rem 1rem;border-radius:8px;opacity:0;transition:.25s;
pointer-events:none}
.toast.show{opacity:1}
</style></head><body>
<header>
  <h1>🏋️ Gym Dashboard</h1>
  <select id="guild" onchange="onGuild()"></select>
  <span class="sp"></span>
  <button onclick="resync()" title="Re-pull members & roles from Discord">↻ Sync</button>
  <form method="post" action="/logout" style="margin:0"><button>Logout</button></form>
</header>
<nav id="nav"></nav>
<main id="view"><div class="empty">Loading…</div></main>
<dialog id="editDlg"></dialog>
<div class="toast" id="toast"></div>
<script>
const TABS = ["overview","members","roles","audit","lifts","calories","protein"];
let guild = null, tab = "overview";

function toast(msg){const t=document.getElementById("toast");t.textContent=msg;
  t.classList.add("show");setTimeout(()=>t.classList.remove("show"),2200);}
function esc(s){return (s==null?"":String(s)).replace(/[&<>"']/g,
  c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function fmtTs(s){if(!s)return"";const d=new Date(s);return isNaN(d)?s:
  d.toLocaleString();}
function roleColor(c){if(!c)return"#8b949e";return"#"+c.toString(16).padStart(6,"0");}
async function api(path){const r=await fetch(path);if(r.status===401){
  location.href="/login";return null;}if(!r.ok)throw new Error(await r.text());
  return r.json();}
async function post(path,body){const r=await fetch(path,{method:"POST",
  headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  if(r.status===401){location.href="/login";return null;}return r.json();}

function renderNav(){const n=document.getElementById("nav");
  n.innerHTML=TABS.map(t=>`<a class="${t===tab?'active':''}" onclick="go('${t}')">${t}</a>`).join("");}
function go(t){tab=t;renderNav();render();}
function onGuild(){guild=document.getElementById("guild").value;render();}

async function boot(){
  const g=await api("/api/guilds");if(!g)return;
  const sel=document.getElementById("guild");
  if(!g.guilds.length){document.getElementById("view").innerHTML=
    '<div class="empty">No guilds tracked yet. Once the bot syncs a server it appears here.</div>';return;}
  sel.innerHTML=g.guilds.map(x=>`<option value="${x.id}">${esc(x.name||("Guild "+x.id))}</option>`).join("");
  guild=g.guilds[0].id;renderNav();render();
}
async function resync(){if(!guild)return;toast("Syncing…");
  const r=await post("/api/resync",{guild});
  toast(r&&r.ok?"Synced":"Sync unavailable");render();}

async function render(){
  const v=document.getElementById("view");
  if(!guild){v.innerHTML='<div class="empty">Pick a guild.</div>';return;}
  v.innerHTML='<div class="empty">Loading…</div>';
  try{
    if(tab==="overview")return renderOverview(v);
    if(tab==="members")return renderMembers(v);
    if(tab==="roles")return renderRoles(v);
    if(tab==="audit")return renderAudit(v);
    if(["lifts","calories","protein"].includes(tab))return renderData(v,tab);
  }catch(e){v.innerHTML='<div class="empty">Error: '+esc(e.message)+'</div>';}
}

async function renderOverview(v){
  const d=await api(`/api/overview?guild=${guild}`);if(!d)return;
  const t=d.totals||{};
  v.innerHTML=`<div class="cards">
    ${stat(d.member_count,"Members")}
    ${stat(d.role_count,"Roles")}
    ${stat(t.total_lifts||0,"Lifts")}
    ${stat(t.lifters||0,"Lifters")}
    ${stat(t.unique_equip||0,"Exercises")}
  </div>
  <h2>Recent activity</h2>${auditTable(d.recent_audit)}`;
}
function stat(n,l){return `<div class="stat"><div class="n">${esc(n)}</div><div class="l">${esc(l)}</div></div>`;}

async function renderMembers(v){
  const d=await api(`/api/members?guild=${guild}`);if(!d)return;
  if(!d.members.length){v.innerHTML='<div class="empty">No members synced. Hit ↻ Sync.</div>';return;}
  v.innerHTML=`<h2>Members (${d.members.length})</h2><table><thead><tr>
    <th>Name</th><th>Username</th><th>Roles</th><th>Joined</th><th></th></tr></thead><tbody>${
    d.members.map(m=>`<tr>
      <td><a class="link" onclick="memberView('${m.user_id}')">${esc(m.display_name)}</a>
        ${m.is_bot?'<span class="pill">bot</span>':''}${m.present?'':'<span class="pill muted">left</span>'}</td>
      <td class="muted">${esc(m.username)}</td>
      <td>${m.role_count}</td>
      <td class="muted">${fmtTs(m.joined_at)}</td>
      <td><a class="link" onclick="memberView('${m.user_id}')">view</a></td>
    </tr>`).join("")}</tbody></table>`;
}

async function memberView(uid){
  const v=document.getElementById("view");v.innerHTML='<div class="empty">Loading…</div>';
  const d=await api(`/api/member?guild=${guild}&user=${uid}`);if(!d)return;
  const m=d.member,o=d.overview||{};
  const lifts=o.lifts||{},cal=o.calories||{},pro=o.protein||{};
  v.innerHTML=`<a class="link" onclick="go('members')">&larr; members</a>
    <h2>${esc(m.display_name||uid)} <span class="muted" style="font-size:.8rem">${esc(m.username||"")}</span></h2>
    <div class="cards">
      ${stat(lifts.n||0,"Lifts")}
      ${stat(lifts.equip||0,"Exercises")}
      ${stat(o.bodyweight?o.bodyweight.weight_kg+"kg":"—","Bodyweight")}
      ${stat(Math.round(cal.total||0),"kcal logged")}
      ${stat(Math.round(pro.total||0),"g protein")}
    </div>
    <p>${d.strava_linked?'<span class="pill">Strava linked</span>':''}
       ${d.revo_linked?'<span class="pill">Revo linked</span>':''}</p>
    <h2>Roles</h2><div>${d.roles.length?d.roles.map(r=>
      `<span class="pill" style="border-color:${roleColor(r.color)};color:${roleColor(r.color)}">${esc(r.name)}</span>`
      ).join(""):'<span class="muted">none</span>'}</div>
    <h2 style="margin-top:1.5rem">History (this member)</h2>${auditTable(d.audit)}
    <p style="margin-top:1rem"><a class="link" onclick="go2('lifts','${uid}')">lifts</a> ·
       <a class="link" onclick="go2('calories','${uid}')">calories</a> ·
       <a class="link" onclick="go2('protein','${uid}')">protein</a> for this member</p>`;
}
let dataUserFilter=null;
function go2(t,uid){dataUserFilter=uid;tab=t;renderNav();render();}

async function renderRoles(v){
  const d=await api(`/api/roles?guild=${guild}`);if(!d)return;
  if(!d.roles.length){v.innerHTML='<div class="empty">No roles synced. Hit ↻ Sync.</div>';return;}
  v.innerHTML=`<h2>Roles (${d.roles.length})</h2><table><thead><tr>
    <th>Role</th><th>Members</th><th>Position</th></tr></thead><tbody>${
    d.roles.map(r=>`<tr>
      <td><span class="pill" style="border-color:${roleColor(r.color)};color:${roleColor(r.color)}">${esc(r.name)}</span>
        ${r.managed?'<span class="pill muted">managed</span>':''}</td>
      <td><a class="link" onclick="roleView('${r.role_id}','${esc(r.name)}')">${r.members}</a></td>
      <td class="muted">${r.position}</td></tr>`).join("")}</tbody></table>`;
}
async function roleView(rid,name){
  const v=document.getElementById("view");
  const d=await api(`/api/role?guild=${guild}&role=${rid}`);if(!d)return;
  v.innerHTML=`<a class="link" onclick="go('roles')">&larr; roles</a><h2>${esc(name)}</h2>
    <table><thead><tr><th>Name</th><th>Username</th></tr></thead><tbody>${
    d.members.map(m=>`<tr><td><a class="link" onclick="memberView('${m.user_id}')">${esc(m.display_name)}</a>
      ${m.present?'':'<span class="pill muted">left</span>'}</td>
      <td class="muted">${esc(m.username)}</td></tr>`).join("")||
      '<tr><td colspan=2 class="muted">No members.</td></tr>'}</tbody></table>`;
}

let auditCat="";
async function renderAudit(v){
  const d=await api(`/api/audit?guild=${guild}&limit=200${auditCat?'&category='+auditCat:''}`);if(!d)return;
  v.innerHTML=`<div class="filters">Filter:
    ${["","role","member","data"].map(c=>`<button onclick="auditCat='${c}';render()"
      ${c===auditCat?'style="background:#1f6feb33"':''}>${c||"all"}</button>`).join("")}
    <span class="muted">${d.total} total</span></div>
    ${auditTable(d.audit)}`;
}
function auditTable(rows){
  if(!rows||!rows.length)return '<div class="empty">Nothing yet.</div>';
  return `<table><thead><tr><th>When</th><th>Category</th><th>Action</th>
    <th>Actor</th><th>Subject</th><th>Detail</th></tr></thead><tbody>${
    rows.map(a=>`<tr>
      <td class="muted">${fmtTs(a.at)}</td>
      <td class="cat-${a.category}">${esc(a.category)}</td>
      <td>${esc(a.action)}</td>
      <td>${esc(a.actor_name||"—")}</td>
      <td>${a.subject_id?`<a class="link" onclick="memberView('${a.subject_id}')">${esc(a.subject_name||a.subject_id)}</a>`:esc(a.subject_name||"—")}</td>
      <td class="muted">${esc(a.detail||"")}</td></tr>`).join("")}</tbody></table>`;
}

async function renderData(v,kind){
  const u=dataUserFilter;dataUserFilter=null;
  const d=await api(`/api/${kind}?guild=${guild}&limit=200${u?'&user='+u:''}`);if(!d)return;
  const rows=d[kind];
  const head=kind==="lifts"?"<th>Exercise</th><th>Weight</th><th>Reps</th>":
    kind==="calories"?"<th>kcal</th><th>Note</th>":"<th>grams</th><th>Note</th>";
  v.innerHTML=`<h2>${kind[0].toUpperCase()+kind.slice(1)} ${u?'(filtered)':''} — ${rows.length} shown</h2>
    ${u?`<p><a class="link" onclick="render()">clear filter</a></p>`:''}
    <table><thead><tr><th>When</th><th>Member</th>${head}<th></th></tr></thead><tbody>${
    rows.map(r=>dataRow(kind,r)).join("")||'<tr><td colspan=6 class="empty">Nothing logged.</td></tr>'}</tbody></table>`;
}
function dataRow(kind,r){
  let cells;
  if(kind==="lifts")cells=`<td>${esc(r.equipment)}</td><td>${r.weight_kg}${r.bw?' (BW+)':''}</td><td>${r.reps??""}</td>`;
  else if(kind==="calories")cells=`<td>${Math.round(r.kcal)}</td><td class="muted">${esc(r.note||"")}</td>`;
  else cells=`<td>${Math.round(r.grams)}</td><td class="muted">${esc(r.note||"")}</td>`;
  const edit=kind==="lifts"?`<button onclick='editLift(${JSON.stringify(r)})'>edit</button>`:"";
  return `<tr><td class="muted">${fmtTs(r.logged_at)}</td>
    <td><a class="link" onclick="memberView('${r.user_id}')">${esc(r.username)}</a></td>
    ${cells}<td><div class="row-actions">${edit}
    <button class="danger" onclick="delData('${kind}',${r.id})">del</button></div></td></tr>`;
}
async function delData(kind,id){
  if(!confirm("Delete this entry? This is audited and cannot be undone."))return;
  const path={lifts:"/api/lifts/delete",calories:"/api/calories/delete",protein:"/api/protein/delete"}[kind];
  const r=await post(path,{guild,id});toast(r&&r.ok?"Deleted":"Failed");render();
}
function editLift(r){
  const dlg=document.getElementById("editDlg");
  dlg.innerHTML=`<h2 style="margin-top:0">Edit lift</h2>
    <label>Exercise</label><input id="e_eq" value="${esc(r.equipment)}">
    <label>Weight (kg)</label><input id="e_w" type="number" step="0.5" value="${r.weight_kg}">
    <label>Reps</label><input id="e_r" type="number" value="${r.reps??''}">
    <div class="dlg-actions"><button onclick="document.getElementById('editDlg').close()">Cancel</button>
    <button onclick="saveLift(${r.id})" style="background:#238636">Save</button></div>`;
  dlg.showModal();
}
async function saveLift(id){
  const eq=document.getElementById("e_eq").value.trim();
  const w=document.getElementById("e_w").value;
  const rp=document.getElementById("e_r").value;
  const r=await post("/api/lifts/edit",{guild,id,equipment:eq,weight_kg:w,reps:rp||null});
  document.getElementById("editDlg").close();
  toast(r&&r.ok?"Saved":"Failed");render();
}
boot();
</script>
</body></html>"""
