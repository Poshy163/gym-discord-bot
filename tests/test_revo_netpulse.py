"""Tests for the Revo Netpulse (EGYM mobile-backend) parsers.

Only the pure, secret-scrubbing parsers are exercised — never the live backend.
All fixtures are SMALL and SYNTHETIC (fake uuids / dates / a fake door barcode)
so no live token or real access credential lands in the repo. The key property
under test is that :func:`parse_membership` drops every secret the raw payload
carries.
"""
from __future__ import annotations

from datetime import datetime, time

from app import revo_netpulse as np


# A synthetic membership payload shaped like exerciser/{uuid}/membership. The
# barcode / agreementNumber / expiry fields are the sensitive door-access bits —
# fabricated here, and asserted to NEVER appear in the parsed result.
_FAKE_MEMBERSHIP = {
    "membershipType": "Basic",
    "membershipSubtype": "Level 2",
    "remainingDays": None,
    "expired": False,
    "barcode": "qr_00000000-0000-4000-8000-000000000000",  # fake secret
    "agreementNumber": "FAKE-AGREEMENT-123",                # fake secret
    "contractSignedDate": "2025-01-02T00:00:00",
    "contractEndDate": None,
    "createdAt": "2025-01-02T03:04:05",
    "barcodeExpiresAt": "2025-01-02T04:00:00Z",             # fake secret
}


def test_parse_membership_extracts_non_secret_fields():
    m = np.parse_membership(_FAKE_MEMBERSHIP)
    assert m.membership_type == "Basic"
    assert m.membership_subtype == "Level 2"
    assert m.join_date == "2025-01-02"  # date-only slice of contractSignedDate
    assert m.expired is False


def test_parse_membership_drops_secrets():
    """The parsed dataclass must not carry the barcode / agreement / expiry."""
    m = np.parse_membership(_FAKE_MEMBERSHIP)
    blob = repr(m)
    assert "qr_00000000" not in blob
    assert "FAKE-AGREEMENT-123" not in blob
    assert "barcodeExpiresAt" not in blob
    # Structurally: only the four whitelisted fields exist.
    assert set(vars(m).keys()) == {
        "membership_type",
        "membership_subtype",
        "join_date",
        "expired",
    }


def test_parse_membership_join_date_falls_back_to_created():
    payload = dict(_FAKE_MEMBERSHIP)
    payload["contractSignedDate"] = None
    m = np.parse_membership(payload)
    assert m.join_date == "2025-01-02"  # from createdAt


def test_parse_membership_accepts_json_string():
    import json

    m = np.parse_membership(json.dumps(_FAKE_MEMBERSHIP))
    assert m.membership_type == "Basic"


def test_parse_membership_garbage_is_all_none():
    m = np.parse_membership("not json")
    assert (m.membership_type, m.membership_subtype, m.join_date, m.expired) == (
        None,
        None,
        None,
        None,
    )


# A synthetic two-club directory. Every real Revo club reports mms=perfectgym —
# the signal that occupancy/check-ins live on PerfectGym, not Netpulse.
_FAKE_CLUBS = [
    {
        "uuid": "11111111-1111-4111-8111-111111111111",
        "name": "Angle Vale",
        "mms": "perfectgym",
        "url": "https://revofitness.com.au/gyms/angle-vale/",
        "address": {"city": "Angle Vale", "stateOrProvince": "SA"},
    },
    {
        "uuid": "22222222-2222-4222-8222-222222222222",
        "name": "Modbury",
        "mms": "perfectgym",
        "url": "https://revofitness.com.au/gyms/modbury/",
        "address": {"city": "Modbury", "stateOrProvince": "SA"},
    },
]


def test_parse_club_directory():
    clubs = np.parse_club_directory(_FAKE_CLUBS)
    assert [c.name for c in clubs] == ["Angle Vale", "Modbury"]
    assert clubs[1].state == "SA"
    assert clubs[1].city == "Modbury"
    # Confirms the "Revo runs on PerfectGym" note that explains the dark
    # occupancy/check-in endpoints.
    assert all(c.mms == "perfectgym" for c in clubs)


def test_parse_club_directory_garbage_is_empty():
    assert np.parse_club_directory({"not": "a list"}) == []
    assert np.parse_club_directory("nonsense") == []


# ---------------------------------------------------------------------------
# Opening hours. Revo publishes these as human copy, not structured data, so the
# fixtures below are the *actual shapes* seen across all 77 clubs (539 day-cells)
# — including the one that packs two different facts into a single cell.
# ---------------------------------------------------------------------------

def test_parse_day_hours_24h_with_staffed_window():
    """The dominant shape (420 of 539 cells). The staffed window must NOT be
    mistaken for the opening window — the club is open all night either way."""
    h = np.parse_day_hours("Open 24 Hours\nStaffed from 9am - 8pm")
    assert h.always_open is True
    assert h.open_from is None and h.open_to is None
    assert (h.staffed_from, h.staffed_to) == (time(9, 0), time(20, 0))


def test_parse_day_hours_machine_readable_24h():
    h = np.parse_day_hours("00:00-23:59")
    assert h.always_open is True
    assert h.staffed_from is None


def test_parse_day_hours_limited_range_variants():
    """Three spellings of a limited-hours club, all live in the payload."""
    for cell, want in (
        ("Open from 5am - 10pm", (time(5, 0), time(22, 0))),
        ("Open from 5:30am - 10pm", (time(5, 30), time(22, 0))),
        ("Open 7:00am-7:00pm", (time(7, 0), time(19, 0))),
        ("Open from 6am - 9:30pm", (time(6, 0), time(21, 30))),
    ):
        h = np.parse_day_hours(cell)
        assert h.always_open is False, cell
        assert (h.open_from, h.open_to) == want, cell


def test_parse_day_hours_unknown_rather_than_guessed():
    """Unrecognised copy must yield None so callers say "unknown", never "closed"."""
    assert np.parse_day_hours("") is None
    assert np.parse_day_hours(None) is None
    assert np.parse_day_hours("By appointment") is None
    assert np.parse_day_hours("Open from 25am - 99pm") is None


def test_parse_hours_falls_back_to_the_html_free_text():
    """10 clubs have workingHours=null; 7 of them still describe hours in the
    HTML workingHoursFreeText field, which recovers them."""
    hours = np.parse_hours({
        "workingHours": None,
        "workingHoursFreeText":
            "<div>Open 24 Hours</div><div>Staffed from 9am - 8pm</div>",
    })
    assert set(hours) == set(np.WEEKDAYS)
    assert hours["Mon"].always_open is True
    assert hours["Sun"].staffed_to == time(20, 0)
    # Neither source usable → no hours at all, not a fabricated week.
    assert np.parse_hours({"workingHours": None, "workingHoursFreeText": None}) == {}


def test_parse_club_directory_keeps_hours_geo_and_contact():
    (club,) = np.parse_club_directory([{
        "uuid": "u", "name": "Cannington", "mms": "perfectgym",
        "timezone": "Australia/Perth", "phone": "1300738638",
        "address": {"city": "Cannington", "postalCode": "6107",
                    "addressLine1": "1 Test Rd", "lat": -32.0, "lng": 115.9},
        "workingHours": {d: "Open 24 Hours\nStaffed from 9am - 8pm"
                         for d in np.WEEKDAYS},
    }])
    assert club.timezone == "Australia/Perth"
    assert club.phone == "1300738638"
    assert club.postal_code == "6107"
    assert (club.lat, club.lng) == (-32.0, 115.9)
    assert club.hours["Wed"].always_open is True


# ---------------------------------------------------------------------------
# club_status — evaluated in the CLUB's timezone, since Revo spans four of them.
# ---------------------------------------------------------------------------

def _club(**kw):
    base = dict(uuid="u", name="Test", city=None, state=None, mms="perfectgym",
                url=None, timezone="Australia/Perth", phone=None,
                postal_code=None, street=None, lat=None, lng=None, hours={})
    base.update(kw)
    return np.Club(**base)


def test_club_status_limited_hours_open_and_closed():
    hours = {d: np.parse_day_hours("Open from 6am - 7pm") for d in np.WEEKDAYS}
    club = _club(hours=hours)
    # 2026-07-31 is a Friday.
    assert np.club_status(club, datetime(2026, 7, 31, 3, 0)).open_now is False
    assert np.club_status(club, datetime(2026, 7, 31, 12, 0)).open_now is True
    assert np.club_status(club, datetime(2026, 7, 31, 23, 0)).open_now is False


def test_club_status_24h_is_open_at_3am_but_unstaffed():
    hours = {d: np.parse_day_hours("Open 24 Hours\nStaffed from 9am - 8pm")
             for d in np.WEEKDAYS}
    s = np.club_status(_club(hours=hours), datetime(2026, 7, 31, 3, 0))
    assert s.open_now is True and s.always_open is True
    assert s.staffed_now is False
    s2 = np.club_status(_club(hours=hours), datetime(2026, 7, 31, 10, 0))
    assert s2.staffed_now is True


def test_club_status_unknown_when_hours_or_timezone_missing():
    """Both degrade to None — a guessed "closed" is the one costly wrong answer."""
    s = np.club_status(_club(hours={}), datetime(2026, 7, 31, 12, 0))
    assert s.open_now is None and s.staffed_now is None
    # No timezone and no explicit time → nothing to evaluate against.
    assert np.club_status(_club(timezone=None)).open_now is None


def test_club_status_uses_the_days_actual_hours():
    """Saturday is staffed 9am-1pm where a weekday runs to 8pm; 2026-08-01 is a Sat."""
    hours = {d: np.parse_day_hours("Open 24 Hours\nStaffed from 9am - 8pm")
             for d in np.WEEKDAYS}
    hours["Sat"] = np.parse_day_hours("Open 24 Hours\nStaffed from 9am - 1pm")
    club = _club(hours=hours)
    assert np.club_status(club, datetime(2026, 8, 1, 15, 0)).staffed_now is False
    assert np.club_status(club, datetime(2026, 7, 31, 15, 0)).staffed_now is True


# ---------------------------------------------------------------------------
# Cross-backend name join. Netpulse and PerfectGym disagree on four club names.
# ---------------------------------------------------------------------------

def test_normalise_club_name_reconciles_the_two_backends():
    n = np.normalise_club_name
    assert n("Knoxfield Vic") == n("Knoxfield")
    assert n("Rivervale WA") == n("Rivervale")
    assert n("Pitt St Sydney") == n("Pitt St")
    # The PerfectGym FullName is what bridges this one.
    assert n("Nunawading (Original)") == n("Nunawading - (Original)")
    # ...and it must NOT collide with the other, separate Nunawading club.
    assert n("Nunawading (Original)") != n("Nunawading")
    assert n("") == "" and n(None) == ""


def test_index_clubs_by_name_is_lookupable_by_either_spelling():
    clubs = np.parse_club_directory([
        {"uuid": "a", "name": "Rivervale WA", "mms": "perfectgym"},
        {"uuid": "b", "name": "Nunawading (Original)", "mms": "perfectgym"},
    ])
    index = np.index_clubs_by_name(clubs)
    assert index[np.normalise_club_name("Rivervale")].uuid == "a"
    assert index[np.normalise_club_name("Nunawading - (Original)")].uuid == "b"
    assert np.normalise_club_name("Nunawading") not in index


def test_postcode_is_normalised_from_a_dirty_field():
    """`postalCode` is hand-entered and dirty in 33 of 77 rows: a state prefix
    ("WA 6021"), a state with no code at all ("SA "), even a 3-digit "WA 615".
    Anything that isn't a clean 4-digit AU postcode must come back None, or a
    caller renders "Modbury SA 5092 SA"."""
    def pc(value):
        (club,) = np.parse_club_directory(
            [{"uuid": "u", "name": "X", "address": {"postalCode": value}}]
        )
        return club.postal_code
    assert pc("5117") == "5117"
    assert pc("WA 6021") == "6021"
    assert pc("VIC  3174") == "3174"
    assert pc("SA ") is None
    assert pc("WA 615") is None   # malformed, not a real postcode
    assert pc(None) is None
