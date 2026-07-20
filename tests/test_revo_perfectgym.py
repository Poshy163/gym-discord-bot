"""Tests for the Revo PerfectGym (ClientPortal2) live-occupancy client.

Everything here is **offline** and uses **SMALL, SYNTHETIC** fixtures — fake club
names/counts and a fake member id — so no live ``CpAuthToken`` cookie or real
member PII lands in the repo. The properties under test:

* :func:`parse_members_in_clubs` is the secret-scrubbing boundary — it emits only
  public club fields (name / suburb / state / live count / capacity), never any
  token or profile field.
* the client re-logins exactly once and retries when the occupancy GET reports an
  expired session (401 / redirect), driven by a fake ``requests``-style session.
* the "busiest right now" ordering helper is stable and state-scopable.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import revo_perfectgym as pg


# ---------------------------------------------------------------------------
# Synthetic occupancy payload — fake clubs, fake counts. Deliberately mixes:
#   * a club with a "<Suburb> STATE postcode" address tail (suburb from address),
#   * a club whose address is a bare street line (suburb falls back to name),
#   * a club with a non-null UsersLimit (capacity → percentage),
#   * a zero-count club (real: closed/overnight), and
#   * a club unknown to the curated state directory with no address state (None).
# ---------------------------------------------------------------------------
_FAKE_OCCUPANCY = {
    "UsersInClubList": [
        {
            "ClubName": "Modbury",
            "ClubAddress": "Westfield Tea Tree Plus, 976 North East Road, Modbury SA 5092",
            "UsersLimit": None,
            "UsersCountCurrentlyInClub": 90,
        },
        {
            "ClubName": "Balcatta",  # bare street line, no state token
            "ClubAddress": "Erindale Road",
            "UsersLimit": 400,  # synthetic capacity → percentage path
            "UsersCountCurrentlyInClub": 200,
        },
        {
            "ClubName": "Woodcroft",  # address suburb differs from the club name
            "ClubAddress": "Cnr Bains & Panalatinga Rds, Morphett Vale SA",
            "UsersLimit": None,
            "UsersCountCurrentlyInClub": 116,
        },
        {
            "ClubName": "Nowhere Test Club",  # not in the curated directory
            "ClubAddress": "1 Made Up Street",
            "UsersLimit": None,
            "UsersCountCurrentlyInClub": 0,  # real zero, not "missing"
        },
    ]
}


def test_parse_members_in_clubs_all_clubs():
    clubs = pg.parse_members_in_clubs(_FAKE_OCCUPANCY)
    assert [c.name for c in clubs] == [
        "Modbury",
        "Balcatta",
        "Woodcroft",
        "Nowhere Test Club",
    ]
    assert [c.count for c in clubs] == [90, 200, 116, 0]


def test_parse_state_primary_from_directory():
    """State comes from revo_client.state_for_club(name) even when the address
    has no state token (Balcatta's address is a bare street line)."""
    clubs = {c.name: c for c in pg.parse_members_in_clubs(_FAKE_OCCUPANCY)}
    assert clubs["Balcatta"].state == "WA"  # from the curated directory
    assert clubs["Modbury"].state == "SA"


def test_parse_suburb_derivation():
    clubs = {c.name: c for c in pg.parse_members_in_clubs(_FAKE_OCCUPANCY)}
    # Suburb from the address tail (last clean comma segment before the state).
    assert clubs["Modbury"].suburb == "Modbury"
    assert clubs["Woodcroft"].suburb == "Morphett Vale"
    # Bare street line → no clean suburb → falls back to the club name.
    assert clubs["Balcatta"].suburb == "Balcatta"


def test_parse_null_capacity_and_zero_count():
    clubs = {c.name: c for c in pg.parse_members_in_clubs(_FAKE_OCCUPANCY)}
    assert clubs["Modbury"].capacity is None  # UsersLimit null preserved
    assert clubs["Balcatta"].capacity == 400  # non-null capacity kept
    # A zero-count club is real, not dropped.
    assert clubs["Nowhere Test Club"].count == 0
    assert clubs["Nowhere Test Club"].state is None  # unknown club, no tail state


def test_parse_accepts_json_string_and_bare_list():
    from_str = pg.parse_members_in_clubs(json.dumps(_FAKE_OCCUPANCY))
    from_list = pg.parse_members_in_clubs(_FAKE_OCCUPANCY["UsersInClubList"])
    assert [c.name for c in from_str] == [c.name for c in from_list]
    assert len(from_str) == 4


def test_parse_garbage_is_empty():
    assert pg.parse_members_in_clubs("not json") == []
    assert pg.parse_members_in_clubs({"wrong": "shape"}) == []
    assert pg.parse_members_in_clubs(None) == []
    # Malformed entries are skipped, valid ones kept.
    mixed = {"UsersInClubList": [{"no": "name"}, {"ClubName": "  "}, 42,
                                 {"ClubName": "Marion", "UsersCountCurrentlyInClub": 5}]}
    parsed = pg.parse_members_in_clubs(mixed)
    assert [c.name for c in parsed] == ["Marion"]
    assert parsed[0].count == 5


# ---------------------------------------------------------------------------
# top_busiest ordering helper
# ---------------------------------------------------------------------------

def _occ(name, count, state):
    return pg.ClubOccupancy(name=name, suburb=name, state=state, count=count, capacity=None)


def test_top_busiest_orders_by_count_then_name():
    clubs = [
        _occ("Alpha", 50, "SA"),
        _occ("Bravo", 231, "WA"),
        _occ("Charlie", 187, "VIC"),
        _occ("Delta", 187, "VIC"),  # ties with Charlie → name breaks the tie
        _occ("Echo", 0, "SA"),
    ]
    top = pg.top_busiest(clubs, limit=3)
    assert [c.name for c in top] == ["Bravo", "Charlie", "Delta"]
    assert [c.count for c in top] == [231, 187, 187]


def test_top_busiest_scoped_to_state():
    clubs = [
        _occ("Alpha", 50, "SA"),
        _occ("Bravo", 231, "WA"),
        _occ("Charlie", 187, "VIC"),
        _occ("Parafield", 189, "SA"),
    ]
    top = pg.top_busiest(clubs, limit=5, state="sa")  # case-insensitive
    assert [c.name for c in top] == ["Parafield", "Alpha"]
    assert all(c.state == "SA" for c in top)


def test_find_club_case_insensitive_over_name_and_suburb():
    clubs = pg.parse_members_in_clubs(_FAKE_OCCUPANCY)
    assert pg.find_club(clubs, "modbury").name == "Modbury"
    assert pg.find_club(clubs, "BALC").name == "Balcatta"  # prefix
    # Match a club via its derived suburb even though the name differs.
    assert pg.find_club(clubs, "Morphett Vale").name == "Woodcroft"
    assert pg.find_club(clubs, "nonexistent") is None
    assert pg.find_club(clubs, "") is None


# ---------------------------------------------------------------------------
# Re-login-on-401 retry — driven by a fake requests-style session (offline).
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status, json_body=None):
        self.status_code = status
        self._json = json_body

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    """Minimal stand-in for requests.Session that scripts the GET responses.

    ``post`` (login) always succeeds and sets a synthetic ``CpAuthToken`` cookie
    plus a fake profile carrying only a fake ``HomeClubId``. ``get`` pops from a
    scripted list so a test can make the first occupancy GET look expired.
    """
    def __init__(self, get_responses):
        self.headers = {}
        self.cookies = []
        self._get_responses = list(get_responses)
        self.login_count = 0
        self.get_calls = []

    def post(self, url, data=None, timeout=None):
        self.login_count += 1
        self.cookies = [SimpleNamespace(name="CpAuthToken")]  # synthetic cookie
        # Fake profile: only HomeClubId is read; no real PII.
        return _FakeResp(200, json_body={"User": {"Member": {"Id": 999, "HomeClubId": 7}}})

    def get(self, url, timeout=None, allow_redirects=True):
        self.get_calls.append(url)
        return self._get_responses.pop(0)


def _client_with_fake(get_responses):
    client = pg.PerfectGymClient("fake@example.com", "fake-password")
    client._http = _FakeSession(get_responses)
    return client


def test_relogin_on_401_then_retry():
    occ_body = {"UsersInClubList": [
        {"ClubName": "Marion", "ClubAddress": "1 St, Marion SA",
         "UsersLimit": None, "UsersCountCurrentlyInClub": 42},
    ]}
    client = _client_with_fake([_FakeResp(401), _FakeResp(200, json_body=occ_body)])
    clubs = client.get_club_occupancy()
    # Two logins: the initial one + one re-login after the 401.
    assert client._http.login_count == 2
    assert len(client._http.get_calls) == 2
    assert [c.name for c in clubs] == ["Marion"]
    assert clubs[0].count == 42
    # Non-secret HomeClubId stashed from the (fake) profile.
    assert client.home_club_id == 7


def test_relogin_on_redirect_then_retry():
    occ_body = {"UsersInClubList": []}
    client = _client_with_fake([_FakeResp(302), _FakeResp(200, json_body=occ_body)])
    client.get_club_occupancy()
    assert client._http.login_count == 2  # redirect treated as expired session


def test_second_expiry_raises_auth_error():
    """If the session still looks expired after one re-login, surface an auth error
    instead of trying to parse a redirect body."""
    client = _client_with_fake([_FakeResp(401), _FakeResp(401)])
    with pytest.raises(pg.PerfectGymAuthError):
        client.get_club_occupancy()


def test_no_relogin_when_first_get_succeeds():
    occ_body = {"UsersInClubList": [
        {"ClubName": "Cannington", "UsersCountCurrentlyInClub": 10},
    ]}
    client = _client_with_fake([_FakeResp(200, json_body=occ_body)])
    client.get_club_occupancy()
    assert client._http.login_count == 1  # one login, no retry
    assert len(client._http.get_calls) == 1


def test_login_failure_raises_auth_error():
    class _FailSession(_FakeSession):
        def post(self, url, data=None, timeout=None):
            self.login_count += 1
            return _FakeResp(401)  # no cookie, no profile

    client = pg.PerfectGymClient("fake@example.com", "fake-password")
    client._http = _FailSession([])
    with pytest.raises(pg.PerfectGymAuthError):
        client.login()


# ---------------------------------------------------------------------------
# Secret hygiene — no CpAuthToken / password / profile PII in any output.
# ---------------------------------------------------------------------------

def test_dataclass_has_only_public_fields():
    clubs = pg.parse_members_in_clubs(_FAKE_OCCUPANCY)
    assert set(vars(clubs[0]).keys()) == {"name", "suburb", "state", "count", "capacity"}


def test_no_secrets_in_parsed_reprs():
    """A login-style payload's secrets must never survive into occupancy data.

    We feed a payload polluted with fake secret-shaped keys and assert the parsed
    dataclasses carry none of them (the parser only reads the whitelisted club
    fields, so pollution is structurally dropped)."""
    polluted = {
        "CpAuthToken": "fake-token-VALUE-should-never-appear",
        "User": {"Member": {"Email": "secret@pii.example", "Barcode": "qr_fake"}},
        "UsersInClubList": [
            {
                "ClubName": "Modbury",
                "ClubAddress": "…, Modbury SA 5092",
                "UsersLimit": None,
                "UsersCountCurrentlyInClub": 5,
                "CpAuthToken": "fake-token-VALUE-should-never-appear",  # noise
            }
        ],
    }
    clubs = pg.parse_members_in_clubs(polluted)
    blob = " ".join(repr(c) for c in clubs)
    assert "fake-token-VALUE-should-never-appear" not in blob
    assert "secret@pii.example" not in blob
    assert "qr_fake" not in blob
    assert "CpAuthToken" not in blob


def test_client_does_not_expose_token_or_password():
    client = _client_with_fake([_FakeResp(200, json_body={"UsersInClubList": []})])
    client.get_club_occupancy()
    # The password isn't a public attribute; only the non-secret home_club_id is.
    public = {k: v for k, v in vars(client).items() if not k.startswith("_")}
    assert "fake-password" not in repr(public)
    assert set(public.keys()) <= {"email", "home_club_id"}


def test_fixture_contains_no_real_credentials():
    """Guard: the synthetic fixtures must not accidentally embed a real token/PII."""
    blob = json.dumps(_FAKE_OCCUPANCY)
    assert "CpAuthToken" not in blob
    assert "Barcode" not in blob
    assert "Password" not in blob
