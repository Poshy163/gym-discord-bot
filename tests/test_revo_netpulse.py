"""Tests for the Revo Netpulse (EGYM mobile-backend) parsers.

Only the pure, secret-scrubbing parsers are exercised — never the live backend.
All fixtures are SMALL and SYNTHETIC (fake uuids / dates / a fake door barcode)
so no live token or real access credential lands in the repo. The key property
under test is that :func:`parse_membership` drops every secret the raw payload
carries.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time

import pytest

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


class _Response:
    def __init__(self, status=200, body=None, text="", headers=None):
        self.status_code = status
        self._body = body
        self.text = text
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Cookies:
    def __init__(self):
        self.clear_calls = 0

    def clear(self):
        self.clear_calls += 1


class _Session:
    def __init__(self, posts, gets):
        self.posts = list(posts)
        self.gets = list(gets)
        self.post_calls = []
        self.get_calls = []
        self.headers = {}
        self.cookies = _Cookies()

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        item = self.posts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        item = self.gets.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(monkeypatch, session):
    class Requests:
        Session = staticmethod(lambda: session)

    monkeypatch.setattr(np, "requests", Requests)
    return np.NetpulseClient("private@example.test", "private-password")


_TOKEN_A = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJhIn0.signature_a"
_TOKEN_B = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJiIn0.signature_b"


def _token_body(token=_TOKEN_A, *, expiry="2099-09-07T11:42:45", partner="BMA"):
    return {
        "provider": "revo-rewards",
        "partner": partner,
        "accessToken": token,
        "accessTokenExpiresAt": expiry,
    }


def test_concurrent_initial_requests_share_one_login(monkeypatch):
    session = _Session(
        [_Response(body={"uuid": "member-a"})],
        [_Response(body=_FAKE_MEMBERSHIP), _Response(body=_FAKE_MEMBERSHIP)],
    )
    client = _client(monkeypatch, session)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _n: client.get_membership(), range(2)))
    assert [r.membership_type for r in results] == ["Basic", "Basic"]
    assert len(session.post_calls) == 1


def test_authenticated_club_directory_success_path_is_preserved(monkeypatch):
    session = _Session(
        [_Response(body={"uuid": "member-a"})],
        [_Response(body=_FAKE_CLUBS)],
    )
    client = _client(monkeypatch, session)
    clubs = client.get_clubs()
    assert [club.name for club in clubs] == ["Angle Vale", "Modbury"]
    assert len(session.post_calls) == 1


def test_expired_session_reauthenticates_once_and_rebuilds_uuid_url(monkeypatch):
    session = _Session(
        [_Response(body={"uuid": "old-uuid"}), _Response(body={"uuid": "new-uuid"})],
        [_Response(status=401), _Response(body=_FAKE_MEMBERSHIP)],
    )
    client = _client(monkeypatch, session)
    assert client.get_membership().membership_subtype == "Level 2"
    assert "old-uuid" in session.get_calls[0][0]
    assert "new-uuid" in session.get_calls[1][0]
    assert len(session.post_calls) == 2


def test_concurrent_expired_requests_share_one_reauthentication(monkeypatch):
    session = _Session(
        [_Response(body={"uuid": "old"}), _Response(body={"uuid": "fresh"})],
        [
            _Response(status=401),
            _Response(body=_FAKE_MEMBERSHIP),
            _Response(body=_FAKE_MEMBERSHIP),
        ],
    )
    client = _client(monkeypatch, session)
    client.login()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _n: client.get_membership(), range(2)))
    assert [r.membership_type for r in results] == ["Basic", "Basic"]
    assert len(session.post_calls) == 2
    assert all("fresh" in call[0] for call in session.get_calls[1:])


def test_login_html_200_is_expiry_and_second_expiry_stops(monkeypatch):
    password_form = '<html><input name="password" type="password"></html>'
    session = _Session(
        [_Response(body={"uuid": "first"}), _Response(body={"uuid": "second"})],
        [
            _Response(text=password_form),
            _Response(status=302, headers={"Location": "/np/exerciser/login"}),
        ],
    )
    client = _client(monkeypatch, session)
    with pytest.raises(np.NetpulseAuthError, match="expired again"):
        client.get_membership()
    assert len(session.post_calls) == 2
    assert client._uuid is None
    assert client._logged_in is False


def test_429_does_not_trigger_login_retry(monkeypatch):
    session = _Session(
        [_Response(body={"uuid": "member"})],
        [_Response(status=429)],
    )
    client = _client(monkeypatch, session)
    with pytest.raises(np.NetpulseUnavailable, match="rate limit"):
        client.get_membership()
    assert len(session.post_calls) == 1


def test_unrelated_redirect_does_not_trigger_login_retry(monkeypatch):
    session = _Session(
        [_Response(body={"uuid": "member"})],
        [_Response(status=302, headers={"Location": "/maintenance"})],
    )
    client = _client(monkeypatch, session)
    with pytest.raises(np.NetpulseUnavailable, match="unexpected redirect"):
        client.get_membership()
    assert len(session.post_calls) == 1


@pytest.mark.parametrize(
    "body",
    [[], {}, {"membershipType": []}, None, ValueError("bad json")],
)
def test_membership_rejects_malformed_json_shapes(monkeypatch, body):
    session = _Session(
        [_Response(body={"uuid": "member"})],
        [_Response(body=body)],
    )
    client = _client(monkeypatch, session)
    with pytest.raises(np.NetpulseUnavailable):
        client.get_membership()


@pytest.mark.parametrize("body", [{}, [{"uuid": "club-without-name"}], ["bad"]])
def test_clubs_reject_malformed_json_shapes(monkeypatch, body):
    session = _Session(
        [_Response(body={"uuid": "member"})],
        [_Response(body=body)],
    )
    client = _client(monkeypatch, session)
    with pytest.raises(np.NetpulseUnavailable, match="unexpected response"):
        client.get_clubs()


def test_failed_login_clears_identity_and_keeps_secrets_out_of_error(monkeypatch):
    leaked = (
        "https://private@example.test:private-password@host/"
        "?uuid=secret-uuid&token=secret-token"
    )
    session = _Session([RuntimeError(leaked)], [])
    client = _client(monkeypatch, session)
    client._uuid = "prior-uuid"
    client._logged_in = True
    with pytest.raises(np.NetpulseUnavailable) as raised:
        client.login()
    message = str(raised.value)
    for secret in (
        "private@example.test", "private-password", "prior-uuid",
        "secret-uuid", "secret-token", leaked,
    ):
        assert secret not in message
    assert client._uuid is None
    assert client._logged_in is False
    assert session.cookies.clear_calls == 1


def test_login_requires_nonempty_uuid(monkeypatch):
    session = _Session([_Response(body={"uuid": "  "})], [])
    client = _client(monkeypatch, session)
    with pytest.raises(np.NetpulseAuthError, match="invalid account response"):
        client.login()


def test_login_transport_and_upstream_failures_are_not_auth_errors(monkeypatch):
    for result in (RuntimeError("network failed"), _Response(status=429), _Response(status=503)):
        session = _Session([result], [])
        client = _client(monkeypatch, session)
        with pytest.raises(np.NetpulseUnavailable):
            client.login()


def test_login_401_is_an_auth_error(monkeypatch):
    client = _client(monkeypatch, _Session([_Response(status=401)], []))
    with pytest.raises(np.NetpulseAuthError, match="rejected"):
        client.login()


def test_requests_disable_automatic_redirects(monkeypatch):
    session = _Session(
        [_Response(body={"uuid": "member"})],
        [_Response(body=_FAKE_MEMBERSHIP)],
    )
    client = _client(monkeypatch, session)
    client.get_membership()
    assert session.post_calls[0][1]["allow_redirects"] is False
    assert session.get_calls[0][1]["allow_redirects"] is False


def test_rewards_token_is_cached_and_force_refreshes(monkeypatch):
    session = _Session(
        [_Response(body={"uuid": "member/a"})],
        [_Response(body=_token_body()), _Response(body=_token_body(_TOKEN_B))],
    )
    client = _client(monkeypatch, session)
    assert client.get_rewards_token() == _TOKEN_A
    assert client.get_rewards_token() == _TOKEN_A
    assert client.get_rewards_token(force_refresh=True) == _TOKEN_B
    assert len(session.get_calls) == 2
    assert "member%2Fa/tokens/BMA" in session.get_calls[0][0]


def test_rewards_token_refreshes_inside_margin(monkeypatch):
    session = _Session(
        [_Response(body={"uuid": "member"})],
        [
            _Response(body=_token_body(expiry="2000-01-01T00:00:00")),
        ],
    )
    client = _client(monkeypatch, session)
    with pytest.raises(np.NetpulseUnavailable, match="invalid expiry"):
        client.get_rewards_token()
    assert client._rewards_token is None


def test_concurrent_rewards_token_reads_share_one_fetch(monkeypatch):
    session = _Session(
        [_Response(body={"uuid": "member"})],
        [_Response(body=_token_body())],
    )
    client = _client(monkeypatch, session)
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(lambda _n: client.get_rewards_token(), range(4)))
    assert values == [_TOKEN_A] * 4
    assert len(session.get_calls) == 1


def test_rewards_tokens_are_isolated_per_client(monkeypatch):
    first = _client(
        monkeypatch,
        _Session([_Response(body={"uuid": "one"})], [_Response(body=_token_body())]),
    )
    second = _client(
        monkeypatch,
        _Session(
            [_Response(body={"uuid": "two"})],
            [_Response(body=_token_body(_TOKEN_B))],
        ),
    )
    assert first.get_rewards_token() == _TOKEN_A
    assert second.get_rewards_token() == _TOKEN_B


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        _token_body(token="not-a-jwt"),
        _token_body(partner="OTHER"),
        _token_body(expiry="not-a-date"),
        {**_token_body(), "provider": ""},
    ],
)
def test_rewards_token_rejects_bad_schema_without_caching_or_leaking(monkeypatch, body):
    session = _Session(
        [_Response(body={"uuid": "member"})],
        [_Response(body=body)],
    )
    client = _client(monkeypatch, session)
    with pytest.raises(np.NetpulseUnavailable) as raised:
        client.get_rewards_token()
    assert client._rewards_token is None
    assert _TOKEN_A not in str(raised.value)
    assert _TOKEN_B not in str(raised.value)


@pytest.mark.parametrize(
    "failed_refresh",
    [RuntimeError("transport contained token=old-secret"), _Response(body={})],
)
def test_failed_force_refresh_discards_old_token(monkeypatch, failed_refresh):
    session = _Session(
        [],
        [failed_refresh, _Response(body=_token_body(_TOKEN_B))],
    )
    client = _client(monkeypatch, session)
    client._logged_in = True
    client._uuid = "member"
    client._rewards_token = _TOKEN_A
    client._rewards_token_expires_at = np._parse_utc_expiry("2099-01-01T00:00:00")

    with pytest.raises(np.NetpulseUnavailable) as raised:
        client.get_rewards_token(force_refresh=True)
    assert _TOKEN_A not in str(raised.value)
    assert client._rewards_token is None
    assert client._rewards_token_expires_at is None

    # A normal follow-up must fetch rather than reuse the rejected old cache.
    assert client.get_rewards_token() == _TOKEN_B
    assert len(session.get_calls) == 2


def test_login_and_failed_session_refresh_clear_rewards_token(monkeypatch):
    session = _Session(
        [
            _Response(body={"uuid": "first"}),
            _Response(body={"uuid": "second"}),
        ],
        [_Response(body=_token_body()), _Response(status=401), _Response(status=401)],
    )
    client = _client(monkeypatch, session)
    assert client.get_rewards_token() == _TOKEN_A
    client._rewards_token_expires_at = None
    with pytest.raises(np.NetpulseAuthError, match="expired again"):
        client.get_rewards_token()
    assert client._rewards_token is None
    assert client._rewards_token_expires_at is None
