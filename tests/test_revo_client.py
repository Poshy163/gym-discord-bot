"""Tests for the Revo Fitness portal client (parsers + DB linkage).

The HTTP client itself isn't exercised against the live portal — we only
verify the pure parsers (HTML → structured data) and the DB helpers that
back ``/revo_link`` / ``/revo_unlink`` / the attendance poller.
"""
from __future__ import annotations

import os

import pytest

from app import revo_client
from app.db import Database


# ---------------------------------------------------------------------------
# Cookie + HTML parsers
# ---------------------------------------------------------------------------

def test_parse_member_cookie_extracts_id_and_level():
    raw = (
        "O%3A8%3A%22stdClass%22%3A2%3A%7B"
        "s%3A2%3A%22id%22%3Bi%3A2298462%3B"
        "s%3A15%3A%22membershipLevel%22%3Bi%3A2%3B%7D"
    )
    member_id, level = revo_client.parse_member_cookie(raw)
    assert member_id == 2298462
    assert level == 2


def test_parse_member_cookie_handles_missing():
    assert revo_client.parse_member_cookie(None) == (None, None)
    assert revo_client.parse_member_cookie("") == (None, None)
    # Cookie present but in unexpected shape — we tolerate it.
    assert revo_client.parse_member_cookie("garbage") == (None, None)


def test_parse_club_counter_basic():
    html = """
        var clubCounterLists = {"Modbury":{"id":25,"in_club":42},
                                "Nunawading":{"id":17,"in_club":130}};
        var barGraphData = [{"6":3,"7":12}, {"6":5,"7":40}];
        var favoriteClubId = 25;
    """
    clubs, fav = revo_client.parse_club_counter(html)
    assert fav == 25
    assert clubs["Modbury"].in_club == 42
    assert clubs["Modbury"].club_id == 25
    assert clubs["Modbury"].hourly == {6: 3, 7: 12}
    assert clubs["Nunawading"].in_club == 130


def test_parse_club_counter_missing_fields():
    clubs, fav = revo_client.parse_club_counter("<html>nothing here</html>")
    assert clubs == {}
    assert fav is None


def test_parse_streak_weeks():
    html = "<div class='hero'><span>6</span> <em>WEEKS</em> streak!</div>"
    assert revo_client.parse_streak_weeks(html) == 6
    assert revo_client.parse_streak_weeks("no streak text") is None


def test_parse_tickets_filters_available_pseudo_row():
    html = """
        <h1>10 Tickets Available</h1>
        <ul>
          <li>+1 Tickets Modbury 12/01/2025</li>
          <li>+1 Tickets Nunawading 11/01/2025</li>
          <li>+2 Tickets Modbury 09/01/2025</li>
        </ul>
    """
    avail, rows = revo_client.parse_tickets(html)
    assert avail == 10
    assert [r.source for r in rows] == ["Modbury", "Nunawading", "Modbury"]
    assert rows[0].delta == 1
    assert rows[2].delta == 2
    assert rows[0].date == "12/01/2025"


def test_parse_raffle_extracts_countdowns():
    html = "<p>Monthly Draw 12 Days</p><p>Major Draw 145 Days</p>"
    out = revo_client.parse_raffle(html)
    assert out == {"monthly_draw_days": 12, "major_draw_days": 145}


def test_find_club_substring_match():
    clubs, _ = revo_client.parse_club_counter(
        'var clubCounterLists = {"Modbury":{"id":25,"in_club":42},'
        '"Nunawading":{"id":17,"in_club":130}};'
        'var barGraphData = []; var favoriteClubId = 25;'
    )
    assert revo_client.find_club(clubs, "modbury").club_id == 25
    assert revo_client.find_club(clubs, "nuna").club_id == 17
    assert revo_client.find_club(clubs, "wadi").club_id == 17  # substring
    assert revo_client.find_club(clubs, "atlantis") is None


# ---------------------------------------------------------------------------
# Fernet encryption helpers (skipped if cryptography isn't installed)
# ---------------------------------------------------------------------------

def test_encrypt_decrypt_roundtrip(monkeypatch):
    pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet
    monkeypatch.setenv("REVO_FERNET_KEY", Fernet.generate_key().decode())
    token = revo_client.encrypt_password("hunter2")
    assert token != "hunter2"
    assert revo_client.decrypt_password(token) == "hunter2"


def test_encrypt_requires_key(monkeypatch):
    pytest.importorskip("cryptography")
    monkeypatch.delenv("REVO_FERNET_KEY", raising=False)
    with pytest.raises(revo_client.RevoUnavailable):
        revo_client.encrypt_password("hunter2")


# ---------------------------------------------------------------------------
# Database linkage
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "gym.sqlite3")
    yield d
    d.close()


def test_link_get_unlink_roundtrip(db):
    db.link_revo_account(
        user_id=42, email="x@y.test", password_enc="enc",
        member_id=999, membership_level=2, favorite_club_id=25,
        notify_guild_id=None, notify_channel_id=12345,
    )
    row = db.get_revo_account(42)
    assert row is not None
    assert row["email"] == "x@y.test"
    assert row["member_id"] == 999
    assert row["membership_level"] == 2
    assert row["favorite_club_id"] == 25
    assert row["notify_channel_id"] == 12345
    assert row["last_ticket_signature"] is None

    assert db.unlink_revo_account(42) is True
    assert db.get_revo_account(42) is None
    assert db.unlink_revo_account(42) is False


def test_relink_replaces_and_resets_cursor(db):
    db.link_revo_account(
        user_id=1, email="a@b.test", password_enc="enc1",
        member_id=1, membership_level=1, favorite_club_id=None,
        notify_guild_id=None, notify_channel_id=None,
    )
    db.update_revo_polling_state(1, "sig1", 5)
    row = db.get_revo_account(1)
    assert row["last_ticket_signature"] == "sig1"
    assert row["last_streak_weeks"] == 5

    # Re-linking must wipe the cursor (re-auth = fresh baseline).
    db.link_revo_account(
        user_id=1, email="a@b.test", password_enc="enc2",
        member_id=1, membership_level=2, favorite_club_id=None,
        notify_guild_id=None, notify_channel_id=None,
    )
    row = db.get_revo_account(1)
    assert row["password_enc"] == "enc2"
    assert row["membership_level"] == 2
    assert row["last_ticket_signature"] is None
    assert row["last_streak_weeks"] is None


def test_list_revo_accounts(db):
    assert db.list_revo_accounts() == []
    for uid in (10, 20, 30):
        db.link_revo_account(
            user_id=uid, email=f"u{uid}@x", password_enc="e",
            member_id=None, membership_level=None, favorite_club_id=None,
            notify_guild_id=None, notify_channel_id=None,
        )
    rows = db.list_revo_accounts()
    assert sorted(r["user_id"] for r in rows) == [10, 20, 30]
