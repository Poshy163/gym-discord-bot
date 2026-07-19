"""Tests for the Revo Netpulse (EGYM mobile-backend) parsers.

Only the pure, secret-scrubbing parsers are exercised — never the live backend.
All fixtures are SMALL and SYNTHETIC (fake uuids / dates / a fake door barcode)
so no live token or real access credential lands in the repo. The key property
under test is that :func:`parse_membership` drops every secret the raw payload
carries.
"""
from __future__ import annotations

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
