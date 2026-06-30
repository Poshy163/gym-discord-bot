"""Dynamic best-effort icon lookup for game/app activities.

Discord publishes a public, auth-free list of every "detectable" application it
recognises — each entry carries the icon hash needed to build a stable CDN URL
(``https://cdn.discordapp.com/app-icons/{id}/{hash}.png``). We pull that list at
runtime, build a name→icon map from it, and cache it to disk so *any* game a
tracked member plays resolves to real art — not just a hand-maintained subset.

Apps that aren't in the games list (CurseForge, Crunchyroll, …) often still have
a registered Discord application, so when one shows up with an ``application_id``
we resolve its icon on demand from the per-application RPC endpoint
(``/applications/{id}/rpc``) and cache that by id too. Both the list and the
images are hosted by Discord, so there's nothing to self-host.

Lifecycle:

* On startup the bot calls :func:`configure` (loads the on-disk cache, or the
  bundled offline seed if there's no cache yet) and then schedules
  :func:`refresh`, which fetches the live list and rewrites the cache when it's
  missing or stale.
* :func:`icon_for` is a cheap synchronous lookup against the in-memory map, so
  request handlers can call it without awaiting anything.

Resolution order at the call site is: the activity's own stored rich-presence
image first, then :func:`icon_for` here, then a client-side coloured tile.
Unknown titles return ``None`` and fall through to that tile.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

LOG = logging.getLogger("gymbot.game_icons")

# Discord's public "detectable games" list — the authoritative online source.
DETECTABLE_URL = "https://discord.com/api/v10/applications/detectable"
# Per-application metadata (name + icon hash) for *any* registered app, not just
# games — how we resolve icons for apps like CurseForge/Crunchyroll that aren't
# in the detectable list but carry an application id in their presence.
RPC_URL = "https://discord.com/api/v10/applications/{app_id}/rpc"
_CDN = "https://cdn.discordapp.com/app-icons/{app_id}/{icon}.png"

# Bundled offline fallback (generated from the same list) so common games still
# show art before the first successful fetch or when the network is unavailable.
_SEED_FILE = Path(__file__).with_name("game_icons_seed.json")

# In-memory normalised-name → icon URL map. Keys are lowercased with all
# non-alphanumeric characters stripped so lookups survive punctuation/spacing/
# casing drift ("PUBG: BATTLEGROUNDS", "Baldur's Gate 3", "ROBLOX").
_ICONS: dict[str, str] = {}
# Per-application-id icon cache, resolved lazily from the RPC endpoint. Values
# are a URL, or "" to mark an id we've checked that has no icon (so we don't
# re-fetch it every request).
_APP_ICONS: dict[str, str] = {}
# Where to persist the app-icon cache (set by ``configure``). The name map has
# its own cache; this one is tiny and updated independently as apps appear.
_APP_CACHE_PATH: Path | None = None
_NORM_RE = re.compile(r"[^a-z0-9]+")
# "Rust with Medal" / "Minecraft with Medal" — Medal's capture overlay renames
# the activity; strip the suffix so it resolves to the underlying game. The
# leading-colon split reduces a subtitled name to its base when the full name
# isn't mapped.
_BASE_RE = re.compile(r"\s+with\s+medal\b|:", re.IGNORECASE)


def _norm(name: str) -> str:
    return _NORM_RE.sub("", name.lower())


def _load_seed() -> dict[str, str]:
    try:
        with _SEED_FILE.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(k): str(v) for k, v in data.items()}
    except (OSError, ValueError):  # pragma: no cover - bundled file is valid
        return {}


def build_index(apps: list[dict]) -> dict[str, str]:
    """Build a normalised name→icon-URL map from a detectable-apps list.

    Each app contributes its primary name and any aliases; the first URL seen
    for a key wins (the list is roughly popularity-ordered). Apps without an
    icon hash are skipped.
    """
    index: dict[str, str] = {}
    for app in apps:
        icon = app.get("icon_hash") or app.get("icon")
        app_id = app.get("id")
        if not icon or not app_id:
            continue
        url = _CDN.format(app_id=app_id, icon=icon)
        names = [app.get("name", "")] + list(app.get("aliases") or [])
        for nm in names:
            key = _norm(nm or "")
            if key and key not in index:
                index[key] = url
    return index


def icon_for(name: str | None) -> str | None:
    """Return a best-effort icon URL for an activity ``name``, or ``None``.

    Matches case/punctuation-insensitively and retries on a trimmed base name
    (dropping a ``with Medal`` suffix or a ``:`` subtitle) before giving up.
    """
    if not name:
        return None
    if not _ICONS:
        _ICONS.update(_load_seed())
    key = _norm(name)
    hit = _ICONS.get(key)
    if hit:
        return hit
    base = _BASE_RE.split(name, maxsplit=1)[0]
    base_key = _norm(base)
    if base_key and base_key != key:
        return _ICONS.get(base_key)
    return None


def _apply(index: dict[str, str]) -> None:
    """Replace the in-memory map, keeping bundled seed keys the live list omits
    (apps the seed knows but Discord no longer lists keep working)."""
    merged = _load_seed()
    merged.update(index)
    _ICONS.clear()
    _ICONS.update(merged)


# ---- cache + refresh -------------------------------------------------------

def load_cache(cache_path: str | os.PathLike) -> bool:
    """Load a previously-saved icon map into memory. Returns True on success."""
    try:
        with open(cache_path, encoding="utf-8") as fh:
            blob = json.load(fh)
        icons = blob.get("icons") if isinstance(blob, dict) else None
        if not isinstance(icons, dict):
            return False
        _apply({str(k): str(v) for k, v in icons.items()})
        return True
    except (OSError, ValueError):
        return False


def save_cache(cache_path: str | os.PathLike, index: dict[str, str]) -> None:
    """Persist ``index`` (plus a build timestamp) so restarts skip the fetch."""
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump({"built_at": time.time(), "icons": index}, fh)
    tmp.replace(path)  # atomic swap so a crash mid-write can't corrupt the cache


def _cache_age_days(cache_path: str | os.PathLike) -> float | None:
    """Age of the cache in days from its ``built_at`` stamp, or None if absent."""
    try:
        with open(cache_path, encoding="utf-8") as fh:
            built = json.load(fh).get("built_at")
        if not built:
            return None
        return (time.time() - float(built)) / 86400.0
    except (OSError, ValueError, TypeError):
        return None


def _app_cache_path(cache_path: str | os.PathLike) -> Path:
    """Sibling file holding the small per-app-id icon cache."""
    return Path(cache_path).with_name("game_icons_apps.json")


def configure(cache_path: str | os.PathLike | None) -> None:
    """Populate the in-memory maps at startup: the on-disk caches if present,
    otherwise the bundled seed. Cheap and network-free — call before serving."""
    global _APP_CACHE_PATH
    if cache_path is not None:
        _APP_CACHE_PATH = _app_cache_path(cache_path)
        try:
            with _APP_CACHE_PATH.open(encoding="utf-8") as fh:
                cached = json.load(fh)
            if isinstance(cached, dict):
                _APP_ICONS.update({str(k): str(v) for k, v in cached.items()})
        except (OSError, ValueError):
            pass
    if cache_path and load_cache(cache_path):
        LOG.info(
            "Game icons: loaded %d entries from cache (%d app icons)",
            len(_ICONS), len(_APP_ICONS),
        )
        return
    _apply({})  # seed only
    LOG.info("Game icons: using bundled seed (%d entries)", len(_ICONS))


def app_icon(app_id: int | str | None) -> str | None:
    """Return a cached icon URL for a Discord application id, or None.

    Synchronous and cache-only — call :func:`resolve_app_icons` first to
    populate the cache. A cached empty string (an id known to have no icon)
    also returns None."""
    if not app_id:
        return None
    return _APP_ICONS.get(str(app_id)) or None


async def _fetch_app_icon(app_id: str, session) -> str | None:
    """Resolve one application id to an icon URL via the RPC endpoint."""
    import aiohttp

    try:
        async with session.get(
            RPC_URL.format(app_id=app_id),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None
    icon = data.get("icon") if isinstance(data, dict) else None
    return _CDN.format(app_id=app_id, icon=icon) if icon else None


async def resolve_app_icons(app_ids, session=None) -> None:
    """Ensure ``app_ids`` are in the app-icon cache, fetching any misses.

    Looks each unknown id up via Discord's per-application RPC endpoint
    (covering non-game apps), records the URL — or ``""`` when the app has no
    icon, so we don't re-fetch it — and persists the cache. Failures are
    swallowed; the dashboard just shows a coloured tile for unresolved ids."""
    import asyncio

    pending = [str(a) for a in app_ids if a and str(a) not in _APP_ICONS]
    if not pending:
        return
    import aiohttp

    own = session is None
    session = session or aiohttp.ClientSession()
    try:
        results = await asyncio.gather(
            *(_fetch_app_icon(a, session) for a in pending)
        )
    finally:
        if own:
            await session.close()
    for app_id, url in zip(pending, results):
        _APP_ICONS[app_id] = url or ""  # "" = checked, no icon
    if _APP_CACHE_PATH is not None:
        try:
            _APP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _APP_CACHE_PATH.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(_APP_ICONS, fh)
            tmp.replace(_APP_CACHE_PATH)
        except OSError as exc:  # pragma: no cover - disk error
            LOG.warning("Game icons: could not write app-icon cache: %s", exc)


async def fetch_detectable(session=None) -> list[dict]:
    """Fetch Discord's detectable-apps list. Uses the given aiohttp session, or
    opens a short-lived one."""
    import aiohttp

    own = session is None
    session = session or aiohttp.ClientSession()
    try:
        async with session.get(DETECTABLE_URL, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return data if isinstance(data, list) else []
    finally:
        if own:
            await session.close()


async def refresh(
    cache_path: str | os.PathLike | None,
    *,
    session=None,
    max_age_days: float = 7.0,
    force: bool = False,
) -> int:
    """Refresh the icon map from Discord's live list and rewrite the cache.

    Skips the (large) download when a cache exists and is younger than
    ``max_age_days`` unless ``force``. Returns the number of entries now in the
    map (0 on a failed fetch — the existing map is left untouched).
    """
    if not force and cache_path is not None:
        age = _cache_age_days(cache_path)
        if age is not None and age < max_age_days:
            LOG.debug("Game icons: cache is %.1fd old, skipping refresh", age)
            return len(_ICONS)
    try:
        apps = await fetch_detectable(session)
    except Exception as exc:  # network/JSON/HTTP — keep whatever we already have
        LOG.warning("Game icons: refresh failed (%s); keeping current map", exc)
        return len(_ICONS)
    index = build_index(apps)
    if not index:
        LOG.warning("Game icons: live list had no usable entries; keeping map")
        return len(_ICONS)
    _apply(index)
    if cache_path is not None:
        try:
            save_cache(cache_path, index)
        except OSError as exc:  # pragma: no cover - disk error
            LOG.warning("Game icons: could not write cache: %s", exc)
    LOG.info("Game icons: refreshed %d entries from Discord", len(index))
    return len(_ICONS)
