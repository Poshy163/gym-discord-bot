"""Offline regression coverage for independent Revo session and response contracts."""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

from app import revo_client as revo
from app import revo_perfectgym as pg


class Response:
    def __init__(self, status=200, text="", body=None, location="", url=None):
        self.status_code = status
        self.text = text
        self.body = body
        self.headers = {"Location": location}
        self.url = url or revo.BASE_URL + revo.REWARDS_PATH

    def json(self):
        if self.body is None:
            raise ValueError("private response not JSON")
        return self.body


class Session:
    def __init__(self, responses, portal=False):
        self.cookies = requests.cookies.RequestsCookieJar()
        self.headers = {}
        self.responses = list(responses)
        self.portal = portal
        self.logins = 0
        self.reads = 0
        self.fail_login = False

    def post(self, *args, **kwargs):
        self.logins += 1
        time.sleep(0.005)
        if self.fail_login:
            return Response(body={"Errors": ["bad credentials"]}, url=revo.BASE_URL + revo.LOGIN_PATH)
        self.cookies.set("Member" if self.portal else "CpAuthToken", "synthetic-secret")
        return Response(body={"User": {"Member": {"HomeClubId": 1}}})

    def get(self, *args, **kwargs):
        self.reads += 1
        return self.responses.pop(0)


@pytest.mark.parametrize("kind", ["portal", "perfectgym"])
@pytest.mark.parametrize("expired", [False, True])
def test_parallel_requests_share_single_login(kind, expired):
    portal = kind == "portal"
    client = revo.RevoClient("fake@example.test", "fake") if portal else pg.PerfectGymClient("fake@example.test", "fake")
    replies = ([Response(401)] if expired else []) + [Response(text="valid", body={"UsersInClubList": []}) for _ in range(6)]
    client._http = Session(replies, portal=portal)
    client._logged_in = expired
    barrier = threading.Barrier(6)
    def read():
        barrier.wait(timeout=5)
        return client._get(revo.REWARDS_PATH) if portal else client.get_club_occupancy()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: read(), range(6)))
    assert len(results) == 6
    assert client._http.logins == 1


@pytest.mark.parametrize("kind", ["portal", "perfectgym"])
def test_login_html_refreshes_once_then_invalidates(kind):
    portal = kind == "portal"
    client = revo.RevoClient("fake@example.test", "fake") if portal else pg.PerfectGymClient("fake@example.test", "fake")
    client._logged_in = True
    client._http = Session([Response(text='<form><input type="password"></form>')] * 2, portal=portal)
    error = revo.RevoAuthError if portal else pg.PerfectGymAuthError
    with pytest.raises(error):
        client._get(revo.REWARDS_PATH) if portal else client.get_club_occupancy()
    assert client._http.logins == 1
    assert not client._logged_in


def test_portal_app_close_redirect_is_not_expiry_or_empty_success():
    client = revo.RevoClient("fake@example.test", "fake")
    client._logged_in = True
    client._http = Session([Response(302, location="/?closePage")], portal=True)
    with pytest.raises(revo.RevoFeatureUnavailable, match="Revo app"):
        client._get(revo.REWARDS_PATH)
    assert client._http.logins == 0


@pytest.mark.parametrize("kind", ["portal", "perfectgym"])
@pytest.mark.parametrize("status", [429, 503])
def test_rate_limit_and_upstream_errors_do_not_retry_login(kind, status):
    portal = kind == "portal"
    client = revo.RevoClient("fake@example.test", "fake") if portal else pg.PerfectGymClient("fake@example.test", "fake")
    client._logged_in = True
    client._http = Session([Response(status)], portal=portal)
    with pytest.raises(revo.RevoUnavailable if portal else pg.PerfectGymUnavailable):
        client._get(revo.REWARDS_PATH) if portal else client.get_club_occupancy()
    assert client._http.logins == 0
    assert client._http.reads == 1


@pytest.mark.parametrize("kind", ["portal", "perfectgym"])
def test_failed_login_clears_previous_session_and_private_context(kind):
    portal = kind == "portal"
    client = revo.RevoClient("fake@example.test", "fake") if portal else pg.PerfectGymClient("fake@example.test", "fake")
    client._http = Session([], portal=portal)
    client.login()
    client._http.fail_login = True
    with pytest.raises(revo.RevoAuthError if portal else pg.PerfectGymAuthError):
        client.login()
    assert not client._logged_in
    assert not client._http.cookies
    assert client.member_id is None if portal else client._user_number is None


@pytest.mark.parametrize("value", [None, "oops", True, -1, 1.5])
def test_occupancy_missing_count_never_becomes_zero(value):
    body = {"UsersInClubList": [{"ClubName": "Synthetic", "UsersCountCurrentlyInClub": value}]}
    assert pg.parse_members_in_clubs(body) == []
    client = pg.PerfectGymClient("fake@example.test", "fake")
    client._logged_in = True
    client._http = Session([Response(body=body)])
    with pytest.raises(pg.PerfectGymUnavailable, match="unreadable counts"):
        client.get_club_occupancy()


def test_occupancy_valid_zero_is_preserved():
    body = {"UsersInClubList": [{"ClubName": "Synthetic", "UsersCountCurrentlyInClub": 0}]}
    assert pg.parse_members_in_clubs(body)[0].count == 0


@pytest.mark.parametrize("body", [None, {"unexpected": []}])
def test_malformed_occupancy_does_not_report_empty_success(body):
    client = pg.PerfectGymClient("fake@example.test", "fake")
    client._logged_in = True
    client._http = Session([Response(text="<html>service unavailable</html>", body=body)])
    with pytest.raises(pg.PerfectGymUnavailable):
        client.get_club_occupancy()


def test_request_exception_does_not_disclose_url_or_credentials(caplog, monkeypatch):
    monkeypatch.setattr(revo.revo_netpulse.NetpulseClient, "get_rewards_token", lambda *a, **k: "synthetic-token")
    client = revo.RevoClient("private@example.test", "password-secret")
    client._logged_in = True
    def fail(*args, **kwargs):
        raise requests.RequestException("https://example.test/?token=secret private@example.test")
    client._http.get = fail
    with caplog.at_level(logging.INFO), pytest.raises(revo.RevoUnavailable) as caught:
        client.get_tickets()
    output = str(caught.value) + caplog.text
    assert "private@example.test" not in output
    assert "token=" not in output
    assert "password-secret" not in output


@pytest.mark.parametrize("count", ["0", "49", "123"])
def test_ticket_balance_uses_only_landing_raffle_anchor(count):
    html = '<a href="/portal/rewards/streaks.php"><span>999</span></a>'
    html += '<a href="/portal/rewards/raffle.php">' + ''.join('<span>' + digit + '</span>' for digit in count) + '</a>'
    assert revo.parse_rewards_landing_tickets(html) == int(count)
    client = revo.RevoClient("fake@example.test", "fake")
    client._get = lambda path: html
    assert client.get_ticket_balance() == int(count)
    assert client.get_rewards_landing().streak_weeks == 999


def test_ticket_balance_never_fabricates_ledger_or_masks_auth(monkeypatch):
    client = revo.RevoClient("fake@example.test", "fake")
    def fail():
        raise revo.RevoAuthError("expired")
    monkeypatch.setattr(client, "get_rewards_landing", fail)
    monkeypatch.setattr(client, "get_tickets", lambda: pytest.fail("auth must propagate"))
    with pytest.raises(revo.RevoAuthError):
        client.get_ticket_balance()


def test_rewards_and_prizes_fail_closed_on_error_html():
    client = revo.RevoClient("fake@example.test", "fake")
    client._get = lambda path: "<html>service error</html>"
    for read in [client.get_rewards_landing, client.get_prize_pool]:
        with pytest.raises(revo.RevoPageUnreadable):
            read()


def test_account_landing_reads_do_not_share_cache():
    clients = [revo.RevoClient("a@example.test", "fake"), revo.RevoClient("b@example.test", "fake")]
    for number, client in enumerate(clients):
        client._get = lambda path, number=number: '<a href="/portal/rewards/raffle.php"><span>' + str(number) + '</span></a>'
    assert [revo.rewards_landing_with_client(c).tickets_available for c in clients] == [0, 1]


def test_perfectgym_unrelated_redirect_does_not_retry_credentials():
    client = pg.PerfectGymClient("fake@example.test", "fake")
    client._logged_in = True
    client._http = Session([Response(302, location="/maintenance")])
    with pytest.raises(pg.PerfectGymUnavailable, match="redirect"):
        client.get_club_occupancy()
    assert client._http.logins == 0


def test_ticket_total_does_not_restore_attendance_health():
    sources = [revo.SourceHealth("Tickets", revo.HEALTH_ERROR),
               revo.SourceHealth("Check-in calendar", revo.HEALTH_ERROR),
               revo.SourceHealth("Ticket balance", revo.HEALTH_OK)]
    assert revo.attendance_feed_state(sources)[0] == "down"


def test_legacy_counter_does_not_share_account_favorite():
    clients = [revo.RevoClient("a@example.test", "fake"), revo.RevoClient("b@example.test", "fake")]
    for favorite, client in enumerate(clients):
        client.get_club_counter = lambda favorite=favorite: ({"Synthetic": revo.ClubInfo("Synthetic", 1, 5, None)}, favorite)
    assert [revo.club_counter_with_client(c)[1] for c in clients] == [0, 1]
