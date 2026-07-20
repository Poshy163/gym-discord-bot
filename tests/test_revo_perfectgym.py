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
import logging
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


# ---------------------------------------------------------------------------
# Club directory + geo (Geo/GetClubList) — public, no PII. Synthetic clubs.
#   * a club with a nested City object + full geo,
#   * a club whose City arrives as a bare string (older shape),
#   * a club unknown to the state directory, with NO lat/lng (not yet geocoded).
# ---------------------------------------------------------------------------
_FAKE_CLUB_LIST = [
    {
        "Id": 25,
        "Name": "Modbury",
        "Address": "976 North East Road",
        "City": {"Id": "66731", "Name": "Modbury", "Country": "AU"},
        "ClubNumber": "404",
        "Latitude": -34.82900000,
        "Longitude": 138.69200000,
        "OpeningDate": "2022-11-01T00:00:00",
        "StateId": 3,  # opaque grouping key — must NOT be used as the state code
    },
    {
        "Id": 33,
        "Name": "Balcatta",
        "Address": "Erindale Road",
        "City": "Balcatta",  # bare-string City shape
        "ClubNumber": "122",
        "Latitude": -31.86740000,
        "Longitude": 115.80520000,
        "OpeningDate": "2023-10-23T00:00:00",
        "StateId": 6,
    },
    {
        "Id": 999,
        "Name": "Nowhere Test Club",  # not in the curated state directory
        "Address": "1 Made Up Street",
        "City": {"Name": "Nowheresville"},
        "ClubNumber": "000",
        # Latitude/Longitude deliberately absent → None
        "OpeningDate": None,
        "StateId": 9,
    },
]


def test_parse_club_list_fields():
    entries = {e.name: e for e in pg.parse_club_list(_FAKE_CLUB_LIST)}
    m = entries["Modbury"]
    assert m.id == 25
    assert m.address == "976 North East Road"
    assert m.city == "Modbury"  # from the nested City object
    assert m.club_number == "404"
    assert m.lat == -34.829 and m.lng == 138.692
    assert m.opening_date == "2022-11-01T00:00:00"
    # City as a bare string is accepted too.
    assert entries["Balcatta"].city == "Balcatta"


def test_parse_club_list_missing_latlng_preserved_as_none():
    entries = {e.name: e for e in pg.parse_club_list(_FAKE_CLUB_LIST)}
    ghost = entries["Nowhere Test Club"]
    assert ghost.lat is None and ghost.lng is None
    assert ghost.opening_date is None  # null date preserved


def test_parse_club_list_state_from_directory_not_stateid():
    """State comes from revo_client.state_for_club(name); the numeric StateId is
    an opaque grouping key and must never be surfaced as the state code."""
    entries = {e.name: e for e in pg.parse_club_list(_FAKE_CLUB_LIST)}
    assert entries["Modbury"].state == "SA"
    assert entries["Balcatta"].state == "WA"
    # Unknown club → None, NOT the raw StateId (9).
    assert entries["Nowhere Test Club"].state is None


def test_parse_club_list_accepts_json_string_and_skips_garbage():
    from_str = pg.parse_club_list(json.dumps(_FAKE_CLUB_LIST))
    assert [e.name for e in from_str] == ["Modbury", "Balcatta", "Nowhere Test Club"]
    assert pg.parse_club_list("not json") == []
    assert pg.parse_club_list(None) == []
    # Entries without a usable Name are dropped; valid ones kept.
    mixed = [42, {"Id": 1, "Name": "  "}, {"Id": 2, "Name": "Marion", "StateId": 3}]
    parsed = pg.parse_club_list(mixed)
    assert [e.name for e in parsed] == ["Marion"]


# ---------------------------------------------------------------------------
# Geo helpers — haversine + nearest_clubs.
# ---------------------------------------------------------------------------

def _dir(name, lat, lng, id_=None, state=None):
    return pg.ClubDirEntry(
        id=id_, name=name, address=None, city=None, club_number=None,
        lat=lat, lng=lng, opening_date=None, state=state,
    )


def test_haversine_known_distances():
    # One degree of longitude at the equator ≈ 111.19 km.
    assert pg.haversine_km(0.0, 0.0, 0.0, 1.0) == pytest.approx(111.19, abs=0.1)
    # One degree of latitude anywhere ≈ 111.19 km.
    assert pg.haversine_km(0.0, 0.0, 1.0, 0.0) == pytest.approx(111.19, abs=0.1)
    # Identical points → exactly zero.
    assert pg.haversine_km(-31.95, 115.86, -31.95, 115.86) == 0.0


def test_nearest_clubs_orders_by_distance_and_excludes_origin_and_uncoordinated():
    entries = [
        _dir("Origin", 0.0, 0.0),
        _dir("Near", 0.0, 1.0),   # ~111 km
        _dir("Mid", 0.0, 3.0),    # ~333 km
        _dir("Far", 0.0, 5.0),    # ~556 km
        _dir("NoCoords", None, None),
    ]
    near = pg.nearest_clubs(entries, "origin", limit=5)  # origin match is case-insensitive
    assert [e.name for e in near] == ["Near", "Mid", "Far"]  # origin + NoCoords excluded


def test_nearest_clubs_respects_limit_and_unknown_origin():
    entries = [_dir("Origin", 0.0, 0.0), _dir("A", 0.0, 1.0), _dir("B", 0.0, 2.0)]
    assert [e.name for e in pg.nearest_clubs(entries, "Origin", limit=1)] == ["A"]
    # Unknown origin, or an origin with no coordinates → empty list.
    assert pg.nearest_clubs(entries, "nonexistent", limit=5) == []
    assert pg.nearest_clubs([_dir("X", None, None), _dir("A", 0.0, 1.0)], "X", 5) == []


# ---------------------------------------------------------------------------
# join_occupancy_to_dir — matched rows gain id/geo; unmatched left as-is.
# ---------------------------------------------------------------------------

def test_join_occupancy_to_dir_matched_and_unmatched():
    occupancy = pg.parse_members_in_clubs(_FAKE_OCCUPANCY)
    directory = pg.parse_club_list(_FAKE_CLUB_LIST)
    joined = {j.occupancy.name: j for j in pg.join_occupancy_to_dir(occupancy, directory)}
    # Every occupancy row is preserved, in order.
    assert [j.occupancy.name for j in pg.join_occupancy_to_dir(occupancy, directory)] == \
        [c.name for c in occupancy]
    # Matched: id/geo/opening_date attached from the directory.
    assert joined["Modbury"].id == 25
    assert joined["Modbury"].lat == -34.829
    assert joined["Modbury"].opening_date == "2022-11-01T00:00:00"
    # "Woodcroft" occupancy row has no matching directory entry → left as-is.
    assert joined["Woodcroft"].id is None
    assert joined["Woodcroft"].lat is None
    assert joined["Woodcroft"].opening_date is None
    # The live count still rides along on the wrapped occupancy.
    assert joined["Modbury"].occupancy.count == 90


def test_join_occupancy_is_case_insensitive():
    occ = [pg.ClubOccupancy(name="MODBURY", suburb="MODBURY", state="SA",
                            count=5, capacity=None)]
    directory = pg.parse_club_list(_FAKE_CLUB_LIST)
    joined = pg.join_occupancy_to_dir(occ, directory)
    assert joined[0].id == 25  # matched despite case difference


# ---------------------------------------------------------------------------
# Membership status — non-sensitive slice of NotificationsData.
# ---------------------------------------------------------------------------

def test_parse_membership_status_payment_ok_inversion():
    profile = {"User": {"Member": {"NotificationsData": {
        "ContractStatus": "Current",
        "HasInvalidContractPaymentMethod": False,  # → payment_ok True
        "HasMemberCardAssigned": True,
    }}}}
    s = pg.parse_membership_status(profile)
    assert s.contract_status == "Current"
    assert s.payment_ok is True
    assert s.has_card is True

    bad = {"User": {"Member": {"NotificationsData": {
        "ContractStatus": "Suspended",
        "HasInvalidContractPaymentMethod": True,  # → payment_ok False
        "HasMemberCardAssigned": False,
    }}}}
    s2 = pg.parse_membership_status(bad)
    assert s2.contract_status == "Suspended"
    assert s2.payment_ok is False
    assert s2.has_card is False


def test_parse_membership_status_missing_notifications_is_all_none():
    # No NotificationsData at all → unknown (all None), never a silent "ok".
    s = pg.parse_membership_status({"User": {"Member": {"HomeClubId": 25}}})
    assert (s.contract_status, s.payment_ok, s.has_card) == (None, None, None)
    # Garbage / missing profile → same, without raising.
    for junk in ("garbage", None, {}, {"User": {}}):
        js = pg.parse_membership_status(junk)
        assert (js.contract_status, js.payment_ok, js.has_card) == (None, None, None)


def test_get_membership_status_from_client_login():
    profile = {"User": {"Member": {
        "Id": 1, "HomeClubId": 25,
        "NotificationsData": {"ContractStatus": "Current",
                              "HasInvalidContractPaymentMethod": False,
                              "HasMemberCardAssigned": True},
    }}}
    client = _client_with_profile(profile, [])
    s = client.get_membership_status()  # triggers login, reads the stashed profile
    assert s.contract_status == "Current"
    assert s.payment_ok is True
    assert s.has_card is True


# ---------------------------------------------------------------------------
# Barcode (UserNumber) — SENSITIVE. get_card_number is the ONLY exposure.
# ---------------------------------------------------------------------------

# Synthetic, obviously-fake barcode value — never a real UserNumber.
_FAKE_BARCODE = "TEST-BARCODE-0001"


class _ProfileSession(_FakeSession):
    """A fake session whose login POST returns a caller-supplied profile body."""
    def __init__(self, profile, get_responses):
        super().__init__(get_responses)
        self._profile = profile

    def post(self, url, data=None, timeout=None):
        self.login_count += 1
        self.cookies = [SimpleNamespace(name="CpAuthToken")]
        return _FakeResp(200, json_body=self._profile)


def _client_with_profile(profile, get_responses):
    client = pg.PerfectGymClient("fake@example.com", "fake-password")
    client._http = _ProfileSession(profile, get_responses)
    return client


def test_get_card_number_returns_user_number():
    profile = {"User": {"Member": {"Id": 1, "HomeClubId": 25,
                                   "UserNumber": _FAKE_BARCODE}}}
    client = _client_with_profile(profile, [])
    assert client.get_card_number() == _FAKE_BARCODE
    # Integer UserNumber is coerced to str.
    client2 = _client_with_profile(
        {"User": {"Member": {"UserNumber": 123456789}}}, [])
    assert client2.get_card_number() == "123456789"
    # Missing UserNumber → None.
    client3 = _client_with_profile({"User": {"Member": {"HomeClubId": 25}}}, [])
    assert client3.get_card_number() is None


def test_get_card_number_is_the_only_public_barcode_exposure():
    """The barcode must leak from get_card_number() and NOWHERE else — not from any
    other public method's return value, not from a public attribute/repr."""
    profile = {"User": {"Member": {
        "Id": 1, "HomeClubId": 25, "UserNumber": _FAKE_BARCODE,
        "Email": "secret@pii.example",
        "NotificationsData": {"ContractStatus": "Current",
                              "HasInvalidContractPaymentMethod": False,
                              "HasMemberCardAssigned": True},
    }}}
    client = _client_with_profile(profile, [
        _FakeResp(200, json_body={"UsersInClubList": [
            {"ClubName": "Modbury", "UsersCountCurrentlyInClub": 5}]}),
        _FakeResp(200, json_body=_FAKE_CLUB_LIST),
    ])
    # Every OTHER public method's output must be barcode-free.
    occupancy = client.get_club_occupancy()
    directory = client.get_club_list()
    status = client.get_membership_status()
    for blob in (repr(occupancy), repr(directory), repr(status)):
        assert _FAKE_BARCODE not in blob
        assert "secret@pii.example" not in blob
    # Public attributes never carry the barcode (it lives on a private field only).
    public = {k: v for k, v in vars(client).items() if not k.startswith("_")}
    assert _FAKE_BARCODE not in repr(public)
    assert set(public.keys()) <= {"email", "home_club_id"}
    # Only get_card_number surfaces it.
    assert client.get_card_number() == _FAKE_BARCODE

    # Dynamic guard: call EVERY no-arg public method on a fresh client and assert
    # the barcode appears in exactly one method's return value — get_card_number.
    fresh = _client_with_profile(profile, [
        _FakeResp(200, json_body={"UsersInClubList": []}),
        _FakeResp(200, json_body=_FAKE_CLUB_LIST),
    ])
    exposers = []
    for name in ("login", "get_club_occupancy", "get_club_list",
                 "get_membership_status", "get_card_number"):
        result = getattr(fresh, name)()
        if _FAKE_BARCODE in repr(result):
            exposers.append(name)
    assert exposers == ["get_card_number"]


def test_membership_and_dir_reprs_carry_no_secrets():
    """MembershipStatus / ClubDirEntry are safe to log/embed: no barcode, no token,
    no email, and structurally no UserNumber field at all."""
    status = pg.MembershipStatus(contract_status="Current", payment_ok=True,
                                 has_card=True)
    entry = pg.ClubDirEntry(id=25, name="Modbury", address="976 North East Road",
                            city="Modbury", club_number="404", lat=-34.829,
                            lng=138.692, opening_date="2022-11-01T00:00:00",
                            state="SA")
    for r in (repr(status), repr(entry)):
        assert _FAKE_BARCODE not in r
        assert "CpAuthToken" not in r
        assert "UserNumber" not in r
        assert "@" not in r  # no email address
    # Neither dataclass has a field that could ever hold a barcode/token.
    assert set(vars(status)) == {"contract_status", "payment_ok", "has_card"}
    assert set(vars(entry)) == {
        "id", "name", "address", "city", "club_number",
        "lat", "lng", "opening_date", "state",
    }


# ---------------------------------------------------------------------------
# Profile first name (non-secret) + photo URL (signed, short-lived capability
# URL — NEVER logged). Both read off the stashed login profile.
# ---------------------------------------------------------------------------

# Synthetic, obviously-fake signed CDN URL. The sig token is what we assert is
# never logged — it stands in for a real short-lived capability signature.
_FAKE_PHOTO_URL = (
    "https://pgaustoragev2.perfectgymcdn.com/members/fake.jpg"
    "?st=abc&e=123&sig=FAKE-SIGNATURE-NEVER-LOG-THIS"
)


def test_get_first_name_and_photo_url_from_login():
    profile = {"User": {"Member": {
        "Id": 1, "HomeClubId": 25,
        "FirstName": "  Testy  ",  # stripped by the accessor
        "PhotoUrl": _FAKE_PHOTO_URL,
    }}}
    client = _client_with_profile(profile, [])
    assert client.get_first_name() == "Testy"
    assert client.get_photo_url() == _FAKE_PHOTO_URL

    # Missing / blank fields → None, never a crash.
    bare = _client_with_profile({"User": {"Member": {"HomeClubId": 25}}}, [])
    assert bare.get_first_name() is None
    assert bare.get_photo_url() is None
    blank = _client_with_profile(
        {"User": {"Member": {"FirstName": "   ", "PhotoUrl": "  "}}}, [])
    assert blank.get_first_name() is None
    assert blank.get_photo_url() is None


class _RotatingPhotoSession(_FakeSession):
    """Login POST returns a fresh, differently-*signed* PhotoUrl each time.

    Lets a test prove ``get_photo_url(refresh=True)`` actually re-logs in (and so
    returns a currently-valid signature) rather than serving the stale stash.
    """
    def post(self, url, data=None, timeout=None):
        self.login_count += 1
        self.cookies = [SimpleNamespace(name="CpAuthToken")]
        return _FakeResp(200, json_body={"User": {"Member": {
            "HomeClubId": 7, "FirstName": "Testy",
            "PhotoUrl": (
                "https://pgaustoragev2.perfectgymcdn.com/p.jpg"
                f"?sig=SIG-{self.login_count}"
            ),
        }}})


def test_get_photo_url_refresh_forces_relogin():
    client = pg.PerfectGymClient("fake@example.com", "fake-password")
    client._http = _RotatingPhotoSession([])

    first = client.get_photo_url()  # lazy login #1
    assert first.endswith("SIG-1")
    assert client._http.login_count == 1

    # refresh=False returns the stashed (possibly-stale) URL with no new login.
    assert client.get_photo_url(refresh=False) == first
    assert client._http.login_count == 1

    # refresh=True re-logs in → a freshly-signed URL.
    refreshed = client.get_photo_url(refresh=True)
    assert refreshed.endswith("SIG-2")
    assert client._http.login_count == 2


def test_photo_url_is_never_logged(caplog):
    """The signed PhotoUrl is a capability URL: it must never reach the logs, and
    it must not be a public attribute of the client."""
    profile = {"User": {"Member": {
        "HomeClubId": 7, "FirstName": "Testy", "PhotoUrl": _FAKE_PHOTO_URL,
    }}}
    client = _client_with_profile(profile, [])
    with caplog.at_level(logging.DEBUG, logger="gymbot.revo.perfectgym"):
        # A refresh forces a login (which DOES log a line) + reads both accessors.
        assert client.get_photo_url(refresh=True) == _FAKE_PHOTO_URL
        assert client.get_first_name() == "Testy"

    # Neither the signature nor the CDN host may appear anywhere in the logs.
    assert "FAKE-SIGNATURE-NEVER-LOG-THIS" not in caplog.text
    assert "pgaustoragev2" not in caplog.text
    assert "sig=" not in caplog.text
    # The login line that IS emitted stays email + home_club_id only.
    assert "home_club_id=7" in caplog.text

    # The signed URL is not exposed as a public attribute (it lives on a private
    # field), so a repr of the client's public surface can't leak it.
    public = {k: v for k, v in vars(client).items() if not k.startswith("_")}
    assert "FAKE-SIGNATURE-NEVER-LOG-THIS" not in repr(public)
    assert set(public.keys()) <= {"email", "home_club_id"}


def test_download_photo_unauthenticated_get(monkeypatch):
    """download_photo fetches the bytes with a bare GET (the signature is the only
    credential — no CpAuthToken cookie rides along)."""
    calls = {}
    resp = _FakeResp(200)
    resp.content = b"\xff\xd8\xff-image-bytes"  # JPEG-ish

    def _get(url, timeout=None):
        calls["url"] = url
        calls["timeout"] = timeout
        return resp

    monkeypatch.setattr(pg, "requests", SimpleNamespace(get=_get))
    data = pg.download_photo(_FAKE_PHOTO_URL)
    assert data == b"\xff\xd8\xff-image-bytes"
    assert calls["url"] == _FAKE_PHOTO_URL


def test_download_photo_requires_requests(monkeypatch):
    monkeypatch.setattr(pg, "requests", None)
    with pytest.raises(pg.PerfectGymUnavailable):
        pg.download_photo(_FAKE_PHOTO_URL)
