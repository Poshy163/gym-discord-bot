"""Tests for app.game_icons (bundled seed + dynamic fetch/cache)."""

from __future__ import annotations

import asyncio
import json

from app import game_icons
from app.game_icons import build_index, icon_for


# --- bundled seed lookups (network-free) ----------------------------------

def test_icon_for_known_game():
    url = icon_for("tModLoader")
    assert url and url.startswith("https://cdn.discordapp.com/app-icons/")


def test_icon_for_is_punctuation_and_case_insensitive():
    # "ROBLOX" / "Roblox" / "roblox" all normalise to the same key.
    assert icon_for("ROBLOX") == icon_for("Roblox") == icon_for("roblox")
    # Subtitled names resolve via their full mapped key.
    assert icon_for("PUBG: BATTLEGROUNDS") is not None


def test_icon_for_strips_medal_capture_suffix():
    # Medal renames the activity ("Rust with Medal") — resolve the base game.
    assert icon_for("Rust with Medal") == icon_for("Rust")


def test_icon_for_unknown_returns_none():
    assert icon_for("Some Totally Unknown App 9000") is None
    assert icon_for(None) is None
    assert icon_for("") is None


# --- dynamic index building -----------------------------------------------

def test_build_index_from_detectable_shape():
    apps = [
        {"id": "1", "name": "Faketopia", "icon_hash": "abc",
         "aliases": ["Faketopia HD"]},
        {"id": "2", "name": "No Icon Game"},          # skipped (no icon)
        {"id": "3", "name": "Faketopia", "icon_hash": "zzz"},  # dup key, ignored
    ]
    index = build_index(apps)
    assert index["faketopia"] == "https://cdn.discordapp.com/app-icons/1/abc.png"
    # Alias resolves to the same app; first-seen URL wins over the later dup.
    assert index["faketopiahd"].endswith("/1/abc.png")
    assert "noicongame" not in index


# --- cache round-trip + refresh -------------------------------------------

def test_cache_save_load_makes_new_games_resolve(tmp_path):
    cache = tmp_path / "game_icons.json"
    game_icons.save_cache(cache, {"faketopia": "https://cdn/x/1/a.png"})
    assert game_icons.load_cache(cache) is True
    # A game only present in the cache now resolves...
    assert icon_for("Faketopia") == "https://cdn/x/1/a.png"
    # ...while bundled-seed games still work (seed is merged underneath).
    assert icon_for("tModLoader")


def test_refresh_fetches_and_writes_cache(tmp_path, monkeypatch):
    cache = tmp_path / "game_icons.json"

    async def fake_fetch(session=None):
        return [{"id": "42", "name": "Brand New Game", "icon_hash": "deadbeef"}]

    monkeypatch.setattr(game_icons, "fetch_detectable", fake_fetch)
    n = asyncio.run(game_icons.refresh(cache, force=True))
    assert n >= 1
    assert icon_for("Brand New Game") == \
        "https://cdn.discordapp.com/app-icons/42/deadbeef.png"
    # Cache persisted with the fetched entry.
    blob = json.loads(cache.read_text(encoding="utf-8"))
    assert blob["icons"]["brandnewgame"].endswith("/42/deadbeef.png")
    assert "built_at" in blob


def test_refresh_skips_when_cache_is_fresh(tmp_path, monkeypatch):
    cache = tmp_path / "game_icons.json"
    game_icons.save_cache(cache, {"faketopia": "https://cdn/x/1/a.png"})

    calls = []

    async def fake_fetch(session=None):
        calls.append(1)
        return [{"id": "9", "name": "Nope", "icon_hash": "h"}]

    monkeypatch.setattr(game_icons, "fetch_detectable", fake_fetch)
    # Fresh cache (just written) + non-zero max age → no download.
    asyncio.run(game_icons.refresh(cache, max_age_days=7))
    assert calls == []


def test_refresh_keeps_map_when_fetch_fails(tmp_path, monkeypatch):
    cache = tmp_path / "game_icons.json"

    async def boom(session=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(game_icons, "fetch_detectable", boom)
    n = asyncio.run(game_icons.refresh(cache, force=True))
    # Failure is swallowed; seed remains usable and no cache is written.
    assert n >= 1
    assert icon_for("tModLoader")
    assert not cache.exists()


# --- per-application icon resolution (non-game apps) ----------------------

def test_resolve_app_icons_caches_and_marks_misses(monkeypatch):
    game_icons._APP_ICONS.clear()
    monkeypatch.setattr(game_icons, "_APP_CACHE_PATH", None)  # no disk in tests

    calls = []

    async def fake_fetch(app_id, session):
        calls.append(app_id)
        # "111" has an icon, "222" has none.
        return "https://cdn/app/111.png" if app_id == "111" else None

    monkeypatch.setattr(game_icons, "_fetch_app_icon", fake_fetch)
    asyncio.run(game_icons.resolve_app_icons([111, 222]))

    assert game_icons.app_icon(111) == "https://cdn/app/111.png"
    assert game_icons.app_icon(222) is None  # known-no-icon → None, not refetch
    assert game_icons.app_icon(None) is None

    # A second resolve doesn't re-hit the endpoint for already-known ids.
    asyncio.run(game_icons.resolve_app_icons([111, 222]))
    assert calls == ["111", "222"]


def test_app_icon_is_cache_only_before_resolve(monkeypatch):
    game_icons._APP_ICONS.clear()
    # Nothing resolved yet → no icon (the dashboard draws a coloured tile).
    assert game_icons.app_icon(999) is None
