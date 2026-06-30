"""Dynamic best-effort icon lookup for game/app activities.

Discord publishes a public, auth-free list of every "detectable" application it
recognises — each entry carries the icon hash needed to build a stable CDN URL
(``https://cdn.discordapp.com/app-icons/{id}/{hash}.png``). We pull that list at
runtime, build a name→icon map from it, and cache it to disk so *any* game a
tracked member plays resolves to real art — not just a hand-maintained subset.
Both the list and the images are hosted by Discord, so there's nothing to
self-host.

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
_CDN = "https://cdn.discordapp.com/app-icons/{app_id}/{icon}.png"

# Bundled offline fallback (generated from the same list) so common games still
# show art before the first successful fetch or when the network is unavailable.
_SEED_FILE = Path(__file__).with_name("game_icons_seed.json")

# In-memory normalised-name → icon URL map. Keys are lowercased with all
# non-alphanumeric characters stripped so lookups survive punctuation/spacing/
# casing drift ("PUBG: BATTLEGROUNDS", "Baldur's Gate 3", "ROBLOX").
_ICONS: dict[str, str] = {}
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


def configure(cache_path: str | os.PathLike | None) -> None:
    """Populate the in-memory map at startup: the on-disk cache if present,
    otherwise the bundled seed. Cheap and network-free — call before serving."""
    if cache_path and load_cache(cache_path):
        LOG.info("Game icons: loaded %d entries from cache", len(_ICONS))
        return
    _apply({})  # seed only
    LOG.info("Game icons: using bundled seed (%d entries)", len(_ICONS))


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
