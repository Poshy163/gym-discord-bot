import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from app import strava_client, strava_maps
from app.db import Database


def message(message_id=100, author_id=999, image="attachment://route.png", user=7):
    embed = discord.Embed(title="Morning run", url="https://www.strava.com/activities/42",
                          description=f"New **Run** by <@{user}> · yesterday")
    embed.add_field(name="Distance", value="5 km")
    embed.set_image(url=image)
    return SimpleNamespace(id=message_id, author=SimpleNamespace(id=author_id),
                           embeds=[embed, discord.Embed(title="Preserved")],
                           attachments=[SimpleNamespace(filename="route.png", url="https://cdn.discordapp.com/route.png"),
                                        SimpleNamespace(filename="notes.txt", url="https://example.com/notes")],
                           edit=AsyncMock())


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db = Database(tmp_path / "test.sqlite3")
    db.link_strava_account(7, 77, "a", "r", 1, None, "Athlete")
    activity = strava_client.parse_activity({
        "id": 42, "athlete": {"id": 77}, "map": {"summary_polyline": "_p~iF~ps|U_ulLnnqC_mqNvxq`@"},
    })
    fetch = AsyncMock(return_value=activity)
    download = lambda url: b"\x89PNG\r\n\x1a\nimage"
    monkeypatch.setattr(strava_client, "download_mapbox_route_png", download)

    def run(messages, **kwargs):
        async def history(*, limit, before, oldest_first):
            eligible = [m for m in messages if before is None or m.id < before.id]
            for msg in eligible[:limit]:
                yield msg
        return asyncio.run(strava_maps.refresh_maps(
            channel=SimpleNamespace(id=55, history=history), bot_id=999,
            db=db, fetch_activity=fetch, token="test", style="satellite-streets-v12",
            **kwargs))
    yield db, fetch, run
    db.close()


def test_legacy_refresh_preserves_message_and_is_idempotent(setup):
    db, fetch, run = setup
    msg = message()
    original = msg.embeds[0].to_dict()
    result = run([msg])
    assert result["updated"] == 1 and result["done"]
    args = msg.edit.call_args.kwargs
    assert set(args) == {"embeds", "attachments", "allowed_mentions"}
    assert args["embeds"][0].to_dict() == original
    assert args["embeds"][1].title == "Preserved"
    assert args["attachments"][0] is msg.attachments[1]
    assert args["attachments"][1].filename == "route.png"
    assert not args["allowed_mentions"].users
    assert db.list_strava_activity_imports(7) == []
    assert db.get_strava_account(7)["last_activity_id"] is None
    assert run([msg])["already_current"] == 1
    assert fetch.await_count == 1 and msg.edit.await_count == 1


@pytest.mark.parametrize("image", [
    "https://cdn.discordapp.com/route.png",
    strava_client.MAPBOX_STATIC_BASE + "/outdoors-v12/static/route",
])
def test_recognizes_discord_cdn_and_mapbox_maps(setup, image):
    _, _, run = setup
    assert run([message(image=image)])["updated"] == 1


def test_skips_foreign_messages_photos_indoor_unlinked_and_current(setup):
    _, fetch, run = setup
    messages = [message(author_id=1), message(image="https://photo.example/p.jpg"),
                message(image=""), message(user=8),
                message(image=strava_client.MAPBOX_STATIC_BASE + "/satellite-streets-v12/static/route")]
    result = run(messages)
    assert result["updated"] == 0 and result["already_current"] == 1
    fetch.assert_not_awaited()
    for msg in messages:
        msg.edit.assert_not_awaited()


def test_ledger_can_identify_posts_without_old_mention(setup):
    db, _, run = setup
    db.claim_strava_activity(7, 42, "test")
    db.finish_strava_activity(7, 42, message_id=100, channel_id=55)
    msg = message()
    msg.embeds[0].description = "Legacy description"
    assert run([msg])["updated"] == 1


def test_dry_run_and_bounded_resume(setup):
    _, fetch, run = setup
    messages = [message(100), message(90), message(80)]
    preview = run(messages, dry_run=True, limit=2)
    assert preview["candidates"] == 2 and preview["next_before"] == "90"
    assert not preview["done"] and preview["updated"] == 0
    fetch.assert_not_awaited()
    first = run(messages, limit=1)
    assert first["updated"] == 1 and first["next_before"] == "100"
    rest = run(messages, before=first["next_before"])
    assert rest["updated"] == 2 and rest["done"]


@pytest.mark.parametrize("failure", ["fetch", "download", "edit"])
def test_failure_keeps_original_and_retry_cursor(setup, monkeypatch, failure):
    db, fetch, run = setup
    msg = message(90)
    if failure == "fetch":
        fetch.return_value = "error: must not expose credentials"
    elif failure == "download":
        monkeypatch.setattr(strava_client, "download_mapbox_route_png", lambda _: None)
    else:
        msg.edit.side_effect = RuntimeError("secret URL")
    result = run([message(100, author_id=1), msg, message(80)])
    assert result["failed"] == 1 and result["next_before"] == "100"
    assert result["updated"] == 0 and not result["done"]
    assert db.meta_get("strava_map_refresh:55:90") is None
    assert "secret" not in str(result) and "credentials" not in str(result)
    if failure != "edit":
        msg.edit.assert_not_awaited()


@pytest.mark.parametrize("change", [{"private": True}, {"athlete_id": 88},
                                    {"map_polyline": ""}, {"photo_url": "https://photo"}])
def test_private_wrong_athlete_no_route_or_new_photo_are_preserved(setup, change):
    from dataclasses import replace
    _, fetch, run = setup
    fetch.return_value = replace(fetch.return_value, **change)
    msg = message()
    assert run([msg])["skipped"] == 1
    msg.edit.assert_not_awaited()


def test_scan_cap_returns_continuation_even_without_candidates(setup):
    _, _, run = setup
    result = run([message(100, author_id=1), message(90)], scan_limit=1)
    assert result["next_before"] == "100" and not result["done"]
