"""Tests for the Revo Fitness portal client (parsers + DB linkage).

The HTTP client itself isn't exercised against the live portal — we only
verify the pure parsers (HTML → structured data) and the DB helpers that
back ``/revo_link`` / ``/revo_unlink`` / the attendance poller.
"""
from __future__ import annotations

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


def test_parse_streak_calendar_april_2026_real_payload():
    """Real wire payload captured from streaks.php?m=4&y=2026.

    The slot keys in the JSON (``"1".."35"``) are grid positions, not days
    of the month — April 2026 starts on a Wednesday so slots 1 and 2 are
    leading-padding ``null`` cells for Mon/Tue 30-31 March. Day-of-month
    is the position of each non-null cell when read left-to-right, top-to-
    bottom: real attended days were 7, 9, 14, 17, 23, 27.
    """
    body = (
        '{"month_name":"April","weeks_data":{'
        '"week1":{"1":null,"2":null,"3":"0","4":"0","5":"0","6":"0","7":"0"},'
        '"week2":{"8":"0","9":"1","10":"0","11":"1","12":"0","13":"0","14":"0"},'
        '"week3":{"15":"0","16":"1","17":"0","18":"0","19":"1","20":"0","21":"0"},'
        '"week4":{"22":"0","23":"0","24":"0","25":"1","26":"0","27":"0","28":"0"},'
        '"week5":{"29":"1","30":"0","31":"0","32":"0"},'
        '"week6":[]}}'
    )
    cal = revo_client.parse_streak_calendar(body)
    # April has 30 days; counter should produce exactly 30 entries.
    assert len(cal) == 30
    assert sorted(d for d, hit in cal.items() if hit) == [7, 9, 14, 17, 23, 27]
    assert cal[1] is False
    assert cal[10] is False
    assert cal[30] is False


def test_parse_streak_calendar_handles_short_february():
    body = (
        '{"month_name":"February","weeks_data":{'
        '"week1":{"1":"0","2":"0","3":"1","4":"0","5":"0","6":"0","7":"0"},'
        '"week2":{"8":"0","9":"0","10":"1","11":"0","12":"0","13":"0","14":"0"},'
        '"week3":{"15":"0","16":"0","17":"0","18":"0","19":"0","20":"0","21":"0"},'
        '"week4":{"22":"0","23":"0","24":"0","25":"0","26":"0","27":"0","28":"0"},'
        '"week5":[],"week6":[]}}'
    )
    cal = revo_client.parse_streak_calendar(body)
    assert len(cal) == 28
    assert sorted(d for d, hit in cal.items() if hit) == [3, 10]


def test_parse_streak_calendar_empty_or_garbage():
    assert revo_client.parse_streak_calendar("") == {}
    assert revo_client.parse_streak_calendar("not json") == {}
    assert revo_client.parse_streak_calendar("{}") == {}
    assert revo_client.parse_streak_calendar('{"weeks_data":"oops"}') == {}


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


def test_latest_attended_day():
    # Picks the highest attended day, ignoring missed days.
    assert revo_client.latest_attended_day({1: True, 10: True, 11: True, 12: False}) == 11
    assert revo_client.latest_attended_day({1: False, 2: False}) is None
    assert revo_client.latest_attended_day({}) is None
    assert revo_client.latest_attended_day({5: True}) == 5


def test_streak_milestone_crossing():
    # No previous streak recorded yet → never celebrate (avoids backfill spam).
    assert revo_client.streak_milestone(None, 8) is None
    # No movement past a milestone.
    assert revo_client.streak_milestone(4, 5) is None
    assert revo_client.streak_milestone(8, 8) is None
    # Crossing exactly onto a milestone.
    assert revo_client.streak_milestone(3, 4) == 4
    assert revo_client.streak_milestone(11, 12) == 12
    # A jump that skips several only celebrates the highest reached.
    assert revo_client.streak_milestone(2, 13) == 12
    # Beyond the top milestone, nothing new to celebrate.
    assert revo_client.streak_milestone(52, 60) is None
    assert revo_client.streak_milestone(51, 52) == 52
    # Defensive: a None current streak yields nothing.
    assert revo_client.streak_milestone(4, None) is None


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
