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
        "s%3A2%3A%22id%22%3Bi%3A1234567%3B"
        "s%3A15%3A%22membershipLevel%22%3Bi%3A2%3B%7D"
    )
    member_id, level = revo_client.parse_member_cookie(raw)
    assert member_id == 1234567
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


# A faithful (synthetic) slice of ticket-tally.php in the current DOM shape:
# a headline "Tickets Available" counter built from single-digit <span> cells,
# then history rows as three-column grid blocks in DATE -> DELTA -> SOURCE
# order. Recent grants are +2, older ones +1.
_TICKET_TALLY_HTML = """
<div id="tallyCounter">
    <div class="flex text-[77px]/[.8] pt-2 font-extrabold">
        <span class="font-gray-bold">0</span>
        <span class="font-gray-bold">0</span>
        <span class="font-gray-bold">0</span>
        <span class="font-yellow-black">3</span>
        <span class="font-yellow-black">1</span>
        <span class="mx-auto text-[15px] mt-7">Tickets<br />Available</span>
    </div>
</div>
<div class="pt-9 px-4 pb-3 ticket-tally-list text-sm">
    <div class="list py-1 px-2 grid grid-cols-3 gap-0 mb-2">
        <div class="font-thin">17/07/2026</div>
        <div class="font-bold">+2 Tickets</div>
<div class="font-thin">Attendance</div>            </div>
    <div class="list py-1 px-2 grid grid-cols-3 gap-0 mb-2">
        <div class="font-thin">07/07/2026</div>
        <div class="font-bold">+2 Tickets</div>
<div class="font-thin">Monthiversary</div>            </div>
    <div class="list py-1 px-2 grid grid-cols-3 gap-0 mb-2">
        <div class="font-thin">08/05/2026</div>
        <div class="font-bold">+1 Tickets</div>
<div class="font-thin">Attendance</div>            </div>
    <div class="list py-1 px-2 grid grid-cols-3 gap-0 mb-2">
        <div class="font-thin">07/04/2026</div>
        <div class="font-bold">+1 Tickets</div>
<div class="font-thin">Welcome</div>            </div>
</div>
"""


def test_parse_tickets_new_row_order_dates_and_deltas():
    """Regression: the row DOM was reordered to DATE -> DELTA -> SOURCE.

    The old flat regex assumed DELTA -> SOURCE -> DATE, so it paired each source
    with the *next-older* row's date and dropped the newest row entirely. The
    per-block parse must keep each row's own three fields together — the newest
    row is 17/07/2026 Attendance +2.
    """
    avail, rows = revo_client.parse_tickets(_TICKET_TALLY_HTML)
    assert avail == 31
    assert len(rows) == 4
    # Newest row's date is correct (the bug mangled exactly this).
    assert rows[0].date == "17/07/2026"
    assert rows[0].source == "Attendance"
    assert rows[0].delta == 2
    # Every source stays glued to its own date.
    assert [(r.date, r.source, r.delta) for r in rows] == [
        ("17/07/2026", "Attendance", 2),
        ("07/07/2026", "Monthiversary", 2),
        ("08/05/2026", "Attendance", 1),
        ("07/04/2026", "Welcome", 1),
    ]
    # Both the doubled (+2) and legacy (+1) deltas parse.
    assert {r.delta for r in rows} == {1, 2}


def test_parse_tickets_filters_available_pseudo_row():
    """An 'Available' source (should it ever render as a row) is dropped."""
    html = """
        <div class="list grid grid-cols-3">
            <div>12/01/2025</div><div>+1 Tickets</div><div>Available</div>
        </div>
        <div class="list grid grid-cols-3">
            <div>11/01/2025</div><div>+2 Tickets</div><div>Attendance</div>
        </div>
    """
    _avail, rows = revo_client.parse_tickets(html)
    assert [r.source for r in rows] == ["Attendance"]
    assert rows[0].delta == 2
    assert rows[0].date == "11/01/2025"


def test_parse_tickets_empty_when_no_rows():
    avail, rows = revo_client.parse_tickets("<html>nothing here</html>")
    assert avail is None
    assert rows == []


def test_parse_rewards_landing_extracts_fav_club_and_count():
    """The rewards landing renders the fav-club tile (id, name, live count)."""
    html = """
    <div class="live-from-box">
        <a href="https://revocentral.revofitness.com.au/portal/club-counter.php?id=25">
            <img src="live-counter-logo.png">
            <div class="relative grid grid-cols-3 gap-0">
                <hr/>
                <div class="border text-center bg-white rounded-l-lg rounded-r-lg">
                    <span class="">0</span>
                </div>
                <div class="border text-center bg-white rounded-lg">
                    <span class="">0</span>
                </div>
                <div class="border text-center bg-white rounded-l-lg rounded-r-lg">
                    <span class="">2</span>
                </div>
            </div>
            <div class="font-black-bold text-center border-[2px] rounded-full bg-white w-full mt-1">
                Modbury                    </div>
        </a>
    </div>
    """
    fav_id, name, in_club = revo_client.parse_rewards_landing(html)
    assert fav_id == 25
    assert name == "Modbury"
    assert in_club == 2


def test_parse_rewards_landing_missing_tile():
    fav_id, name, in_club = revo_client.parse_rewards_landing("<html>no tile</html>")
    assert (fav_id, name, in_club) == (None, None, None)


def test_parse_prize_pool_monthly_then_major():
    """Two blurbs in DOM order [monthly, major]; tags stripped, T&C line ignored."""
    html = """
    <div class="border rounded-2 my-6">
        <h3><span>Monthly</span> Draw</h3>
        <div class="py-3 px-1">
            <p><strong>EVERY GYM HAS A WINNER!</strong><br />Win Revo merch and 3 months free membership!</p>
            <h3 class="text-center mt-3">Find T&C's on the FAQ page</h3>
        </div>
    </div>
    <div class="border rounded-2 my-6">
        <h3><strong class="font-black italic">Major</strong> Draw</h3>
        <div class="py-3 px-1">
            <p>One lucky Revo member will get the choice between $50,000 cash or a brand new BYD SEALION 7 car!</p>
            <h3 class="text-center mt-3">Find T&C's on the FAQ page</h3>
        </div>
    </div>
    """
    out = revo_client.parse_prize_pool(html)
    assert out["monthly"] == (
        "EVERY GYM HAS A WINNER! Win Revo merch and 3 months free membership!"
    )
    assert out["major"] == (
        "One lucky Revo member will get the choice between "
        "$50,000 cash or a brand new BYD SEALION 7 car!"
    )
    # The T&C <h3> in the same block must not leak into the blurb.
    assert "FAQ" not in out["monthly"]


def test_parse_prize_pool_degrades_to_none():
    out = revo_client.parse_prize_pool("<html>no prizes here</html>")
    assert out == {"monthly": None, "major": None}


def test_parse_raffle_extracts_countdowns():
    html = "<p>Monthly Draw 12 Days</p><p>Major Draw 145 Days</p>"
    out = revo_client.parse_raffle(html)
    assert out == {"monthly_draw_days": 12, "major_draw_days": 145}


# ---------------------------------------------------------------------------
# Raffle opt-in state. parse_raffle scrapes the countdown block even when the
# portal HIDES it, which is what it does for an opted-out member — so without
# this the bot promises a draw to someone whose tickets aren't entered.
# ---------------------------------------------------------------------------

def _opt_buttons(*, hide_in: bool, hide_out: bool) -> str:
    def btn(which: str, hidden: bool) -> str:
        style = ' style="display: none"' if hidden else " "
        return (
            f'<button id="opt{which}" type="button" class="btn-opt" '
            f'data-opt="{which}" data-opt-val="1"{style}>Opt {which}</button>'
        )
    return btn("In", hide_in) + btn("Out", hide_out)


def test_parse_raffle_optin_reads_which_button_is_hidden():
    # The portal shows only the action available: a visible "Opt In" button means
    # the member is currently OUT.
    assert revo_client.parse_raffle_optin(_opt_buttons(hide_in=False, hide_out=True)) is False
    assert revo_client.parse_raffle_optin(_opt_buttons(hide_in=True, hide_out=False)) is True


def test_parse_raffle_optin_unknown_rather_than_a_wrong_answer():
    """An unreadable page must yield None, never a silent False."""
    assert revo_client.parse_raffle_optin("<html>no buttons at all</html>") is None
    # Only one button rendered → can't infer the state.
    assert revo_client.parse_raffle_optin('<button id="optIn"></button>') is None
    # Both shown or both hidden → a shape we don't understand.
    assert revo_client.parse_raffle_optin(_opt_buttons(hide_in=False, hide_out=False)) is None
    assert revo_client.parse_raffle_optin(_opt_buttons(hide_in=True, hide_out=True)) is None


def test_parse_raffle_optin_matches_the_live_opted_out_shape():
    """The exact markup the live portal served for an opted-out member: the
    countdown wrapper is hidden, yet parse_raffle still reports numbers from it."""
    html = (
        '<div id="nextDrawWrapper" class="show-hide grid grid-cols-2" '
        'style="display: none" >'
        "<span>Monthly</span> Draw <span>0</span><span>6</span><span>Days</span>"
        "<span>Major</span> Draw <span>0</span><span>6</span><span>Days</span>"
        "</div>"
        '<button id="optOut" type="button" class="btn-opt" data-opt="Out" '
        'data-opt-val="1" style="display: none">Opt Out</button>'
        '<button id="optIn" type="button" class="btn-opt" data-opt="In" '
        'data-opt-val="1" >Opt In</button>'
    )
    assert revo_client.parse_raffle_optin(html) is False
    # parse_raffle is happy to read the hidden block — that's exactly why the
    # opt-in flag has to be carried alongside it.
    assert revo_client.parse_raffle(html)["monthly_draw_days"] == 6


def test_parse_streak_weeks_handles_padded_digit_spans():
    """Every other counter on this portal is split into zero-padded one-digit
    spans. If this one is ever rendered that way, a plain (\\d+) capture would read
    "1 3 WEEKS" as 3 — a wrong-but-plausible streak nothing would flag."""
    single = '<div id="tallyCounter"><span class="text-8xl">3</span></div><h2>WEEKS</h2>'
    assert revo_client.parse_streak_weeks(single) == 3
    padded = "<span>1</span><span>3</span><h2>WEEKS</h2>"
    assert revo_client.parse_streak_weeks(padded) == 13
    zero_padded = "<span>0</span><span>7</span><h2>WEEKS</h2>"
    assert revo_client.parse_streak_weeks(zero_padded) == 7
    assert revo_client.parse_streak_weeks("<p>1 WEEK</p>") == 1
    assert revo_client.parse_streak_weeks("<p>nothing here</p>") is None


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


# ---------------------------------------------------------------------------
# Access guard (`Invalid Access! B`) + attendance fallback
# ---------------------------------------------------------------------------

class _FakeResp:
    """Minimal stand-in for a ``requests.Response`` in client tests."""

    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self.status_code = status
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:  # pragma: no cover - trivial
        pass


def test_is_access_guarded():
    assert revo_client.is_access_guarded("Invalid Access! B")
    # The portal returns it verbatim, but tolerate incidental whitespace.
    assert revo_client.is_access_guarded("  Invalid Access! B  ")
    assert not revo_client.is_access_guarded("")
    assert not revo_client.is_access_guarded(None)
    assert not revo_client.is_access_guarded("<html>a real page</html>")


def test_latest_attendance_ticket_date_picks_newest_attendance_only():
    rows = [
        revo_client.TicketRow(2, "Monthiversary", "07/08/2026"),
        revo_client.TicketRow(2, "Attendance", "12/08/2026"),
        revo_client.TicketRow(2, "Attendance", "06/08/2026"),
        revo_client.TicketRow(1, "Welcome", "07/04/2026"),
        revo_client.TicketRow(1, "BONUSDAILY", "17/04/2026"),
    ]
    # Only 'Attendance' rows count; Monthiversary/Welcome/BONUSDAILY are ignored.
    assert revo_client.latest_attendance_ticket_date(rows) == "2026-08-12"


def test_latest_attendance_ticket_date_none_when_no_attendance_rows():
    rows = [
        revo_client.TicketRow(2, "Monthiversary", "07/08/2026"),
        revo_client.TicketRow(1, "Welcome", "07/04/2026"),
    ]
    assert revo_client.latest_attendance_ticket_date(rows) is None
    assert revo_client.latest_attendance_ticket_date([]) is None


@pytest.mark.skipif(not revo_client.available(), reason="requests not installed")
def test_get_streak_weeks_raises_on_guard(monkeypatch):
    c = revo_client.RevoClient("e@x", "pw")
    monkeypatch.setattr(c, "_get", lambda path: revo_client.GUARD_BODY)
    with pytest.raises(revo_client.RevoAccessGuarded):
        c.get_streak_weeks()


@pytest.mark.skipif(not revo_client.available(), reason="requests not installed")
def test_get_streak_weeks_parses_when_not_guarded(monkeypatch):
    c = revo_client.RevoClient("e@x", "pw")
    monkeypatch.setattr(c, "_get", lambda path: "<span>4</span> WEEKS streak")
    assert c.get_streak_weeks() == 4


@pytest.mark.skipif(not revo_client.available(), reason="requests not installed")
def test_get_raffle_raises_on_guard(monkeypatch):
    c = revo_client.RevoClient("e@x", "pw")
    monkeypatch.setattr(c, "_get", lambda path: revo_client.GUARD_BODY)
    with pytest.raises(revo_client.RevoAccessGuarded):
        c.get_raffle()


@pytest.mark.skipif(not revo_client.available(), reason="requests not installed")
def test_get_streak_calendar_raises_on_guard(monkeypatch):
    c = revo_client.RevoClient("e@x", "pw")
    c._logged_in = True
    monkeypatch.setattr(
        c._http, "get", lambda *a, **k: _FakeResp(revo_client.GUARD_BODY),
    )
    with pytest.raises(revo_client.RevoAccessGuarded):
        c.get_streak_calendar(8, 2026)


@pytest.mark.skipif(not revo_client.available(), reason="requests not installed")
def test_get_latest_attendance_prefers_calendar(monkeypatch):
    c = revo_client.RevoClient("e@x", "pw")
    monkeypatch.setattr(c, "get_streak_calendar", lambda m, y: {10: True, 12: True, 15: False})
    monkeypatch.setattr(c, "get_streak_weeks", lambda: 6)
    info = c.get_latest_attendance(8, 2026)
    assert info.date == "2026-08-12"  # highest attended day
    assert info.source == "calendar"
    assert info.streak_weeks == 6


@pytest.mark.skipif(not revo_client.available(), reason="requests not installed")
def test_get_latest_attendance_none_when_calendar_empty(monkeypatch):
    c = revo_client.RevoClient("e@x", "pw")
    monkeypatch.setattr(c, "get_streak_calendar", lambda m, y: {1: False, 2: False})
    monkeypatch.setattr(c, "get_streak_weeks", lambda: 3)
    info = c.get_latest_attendance(8, 2026)
    assert info.date is None
    assert info.source is None


@pytest.mark.skipif(not revo_client.available(), reason="requests not installed")
def test_get_latest_attendance_falls_back_to_tickets_when_guarded(monkeypatch):
    c = revo_client.RevoClient("e@x", "pw")

    def _guarded(m, y):
        raise revo_client.RevoAccessGuarded("streaks guarded")

    rows = [
        revo_client.TicketRow(2, "Monthiversary", "07/08/2026"),
        revo_client.TicketRow(2, "Attendance", "12/08/2026"),
        revo_client.TicketRow(2, "Attendance", "06/08/2026"),
    ]
    monkeypatch.setattr(c, "get_streak_calendar", _guarded)
    monkeypatch.setattr(c, "get_tickets", lambda: (41, rows))
    info = c.get_latest_attendance(8, 2026)
    assert info.date == "2026-08-12"
    assert info.source == "tickets"
    assert info.streak_weeks is None  # streak page is guarded too


@pytest.mark.skipif(not revo_client.available(), reason="requests not installed")
def test_get_latest_attendance_tickets_fallback_empty(monkeypatch):
    c = revo_client.RevoClient("e@x", "pw")

    def _guarded(m, y):
        raise revo_client.RevoAccessGuarded("streaks guarded")

    monkeypatch.setattr(c, "get_streak_calendar", _guarded)
    monkeypatch.setattr(c, "get_tickets", lambda: (41, []))
    info = c.get_latest_attendance(8, 2026)
    assert info.date is None
    assert info.source is None


@pytest.mark.skipif(not revo_client.available(), reason="requests not installed")
def test_get_latest_attendance_marks_streak_readable(monkeypatch):
    """A successful streak read is flagged readable so callers may cache it."""
    c = revo_client.RevoClient("e@x", "pw")
    monkeypatch.setattr(c, "get_streak_calendar", lambda m, y: {12: True})
    monkeypatch.setattr(c, "get_streak_weeks", lambda: 6)
    info = c.get_latest_attendance(8, 2026)
    assert info.streak_weeks == 6
    assert info.streak_readable is True


@pytest.mark.skipif(not revo_client.available(), reason="requests not installed")
def test_get_latest_attendance_reads_streak_even_with_no_checkin(monkeypatch):
    """The streak must be read between check-ins too.

    Skipping it when the month holds no visit would let a cached streak freeze
    for anyone mid-gap (and never clear for someone who has churned).
    """
    c = revo_client.RevoClient("e@x", "pw")
    calls = []
    monkeypatch.setattr(c, "get_streak_calendar", lambda m, y: {1: False, 2: False})
    monkeypatch.setattr(c, "get_streak_weeks", lambda: calls.append(1) or 0)
    info = c.get_latest_attendance(8, 2026)
    assert info.date is None and info.source is None
    assert calls, "streak must still be fetched when no visit is recorded"
    assert info.streak_weeks == 0
    assert info.streak_readable is True


@pytest.mark.skipif(not revo_client.available(), reason="requests not installed")
def test_get_latest_attendance_survives_transient_streak_error(monkeypatch):
    """A non-guard streak failure must not sink the attendance result.

    The streak is only a tail on the announcement; letting a 5xx/timeout escape
    aborted the whole poll and silently dropped a real check-in ping.
    """
    c = revo_client.RevoClient("e@x", "pw")

    def _boom():
        raise RuntimeError("streaks.php 500")

    monkeypatch.setattr(c, "get_streak_calendar", lambda m, y: {12: True})
    monkeypatch.setattr(c, "get_streak_weeks", _boom)
    info = c.get_latest_attendance(8, 2026)
    assert info.date == "2026-08-12"   # attendance survived
    assert info.source == "calendar"
    assert info.streak_weeks is None
    assert info.streak_readable is False  # unreadable ⇒ don't overwrite a cache


@pytest.mark.skipif(not revo_client.available(), reason="requests not installed")
def test_get_latest_attendance_streak_guarded_but_calendar_ok(monkeypatch):
    """Mixed state: calendar renders, streaks HTML guarded."""
    c = revo_client.RevoClient("e@x", "pw")

    def _guarded():
        raise revo_client.RevoAccessGuarded("streaks guarded")

    monkeypatch.setattr(c, "get_streak_calendar", lambda m, y: {12: True})
    monkeypatch.setattr(c, "get_streak_weeks", _guarded)
    info = c.get_latest_attendance(8, 2026)
    assert info.date == "2026-08-12"
    assert info.source == "calendar"
    assert info.streak_readable is False


# ---------------------------------------------------------------------------
# Source health probe (/revo_health)
# ---------------------------------------------------------------------------

class _HealthClient:
    """Fake client whose each getter can be told to succeed, guard, or blow up."""

    def __init__(self, **behaviour):
        self.b = behaviour

    def _act(self, key, ok_value):
        v = self.b.get(key, "ok")
        if v == "guard":
            raise revo_client.RevoAccessGuarded("guarded")
        if v == "boom":
            raise RuntimeError("network")
        if v == "empty":
            return None if not isinstance(ok_value, (dict, list)) else type(ok_value)()
        return ok_value

    def get_streak_calendar(self, m, y):
        return self._act("calendar", {1: True})

    def get_streak_weeks(self):
        return self._act("streak", 5)

    def get_tickets(self):
        return (self._act("tickets", 41), [])

    def get_raffle(self):
        return self._act("raffle", revo_client.RaffleInfo(1, 2, True))

    def get_prize_pool(self):
        return {"monthly": self._act("prize", "win stuff"), "major": None}

    def get_rewards_landing(self):
        return revo_client.RewardsLanding(
            fav_club_id=self._act("landing", 25), fav_club_name="X", in_club=3,
        )


def test_probe_sources_all_healthy():
    sources = revo_client.probe_sources(_HealthClient(), 8, 2026)
    assert {s.label for s in sources} == {
        "Check-in calendar", "Weekly streak", "Tickets",
        "Raffle", "Prize pool", "Rewards landing",
    }
    assert all(s.ok for s in sources), [(s.label, s.status) for s in sources]


def test_probe_sources_classifies_guard_error_and_empty():
    client = _HealthClient(calendar="guard", streak="guard", tickets="boom", raffle="empty")
    by = {s.label: s for s in revo_client.probe_sources(client, 8, 2026)}
    assert by["Check-in calendar"].status == revo_client.HEALTH_GUARDED
    assert by["Weekly streak"].status == revo_client.HEALTH_GUARDED
    assert by["Tickets"].status == revo_client.HEALTH_ERROR
    assert by["Raffle"].status == revo_client.HEALTH_EMPTY
    # A guard must never be reported as a generic error — the operator needs to
    # know it's Revo blocking the page, not our credentials or network.
    assert "Invalid Access" in (by["Check-in calendar"].detail or "")


def test_attendance_feed_state_ok_degraded_down():
    healthy = revo_client.probe_sources(_HealthClient(), 8, 2026)
    assert revo_client.attendance_feed_state(healthy)[0] == "ok"

    # Calendar guarded but tickets fine → the fallback carries the feed.
    degraded = revo_client.probe_sources(
        _HealthClient(calendar="guard", streak="guard", raffle="guard"), 8, 2026,
    )
    state, why = revo_client.attendance_feed_state(degraded)
    assert state == "degraded"
    assert "ticket" in why.lower()

    # Neither source usable → nothing can detect a check-in.
    down = revo_client.probe_sources(
        _HealthClient(calendar="guard", streak="guard", tickets="boom"), 8, 2026,
    )
    assert revo_client.attendance_feed_state(down)[0] == "down"


def test_probe_sources_survives_a_totally_broken_client():
    """Every probe is isolated: one dead source must not hide the others."""
    client = _HealthClient(
        calendar="boom", streak="boom", tickets="boom",
        raffle="boom", prize="boom", landing="boom",
    )
    sources = revo_client.probe_sources(client, 8, 2026)
    assert len(sources) == 6
    assert all(s.status == revo_client.HEALTH_ERROR for s in sources)
