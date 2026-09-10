"""Sanitised contracts traced from Android 4.3 and verified with live reads."""
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from app import revo_client as revo
from app import revo_netpulse as np
from app import revo_http


class Reply:
    def __init__(self, status=200, text="feature", location=""):
        self.status_code = status
        self.text = text
        self.headers = {"Location": location}


class Mobile:
    def __init__(self, value="synthetic-private-token"):
        self.value = value
        self.calls = []

    def get_rewards_token(self, *, force_refresh=False):
        self.calls.append(force_refresh)
        return self.value


def make_client(monkeypatch, replies):
    client = revo.RevoClient("synthetic@example.test", "fake-password")
    client._mobile = Mobile()
    calls = []
    replies = iter(replies)

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return next(replies)

    monkeypatch.setattr(client._http, "get", get)
    monkeypatch.setattr(client, "login", lambda: pytest.fail("app reads must not require portal login"))
    return client, calls


@pytest.mark.parametrize("path", sorted(revo.APP_REWARDS_PATHS))
def test_app_features_send_token_only_to_verified_destination(monkeypatch, path):
    client, calls = make_client(monkeypatch, [Reply()])
    assert client._get(path) == "feature"
    url, kwargs = calls[0]
    assert url == revo.BASE_URL + path
    assert kwargs["params"] == {"token": client._mobile.value}
    assert kwargs["allow_redirects"] is False
    assert client._mobile.calls == [False]


def test_calendar_preserves_month_and_decodes_gym_and_les_mills(monkeypatch):
    cells = {str(i): "0" for i in range(1, 29)}
    cells.update({"1": "1", "2": "2", "3": "3"})
    body = json.dumps({"month_name": "February", "weeks_data": {"week1": cells, "week6": []}})
    client, calls = make_client(monkeypatch, [Reply(text=body)])
    calendar = client.get_streak_calendar(2, 2026)
    assert len(calendar) == 28
    assert [d for d, attended in calendar.items() if attended] == [1, 3]
    assert calls[0][1]["params"] == {"m": 2, "y": 2026, "token": client._mobile.value}


@pytest.mark.parametrize("rejected", [Reply(302, location="/?closePage"), Reply(401), Reply(text='<input type="password">'), Reply(text=revo.GUARD_BODY)])
def test_rejected_token_refreshes_once_without_portal_relogin(monkeypatch, rejected):
    client, calls = make_client(monkeypatch, [rejected, Reply()])
    assert client._get(revo.RAFFLE_PATH) == "feature"
    assert client._mobile.calls == [False, True]
    assert len(calls) == 2


def test_repeated_rejection_is_unavailable_and_bounded(monkeypatch):
    client, calls = make_client(monkeypatch, [Reply(302, location="/?closePage")] * 2)
    with pytest.raises(revo.RevoFeatureUnavailable):
        client.get_tickets()
    assert len(calls) == 2
    assert client._mobile.calls == [False, True]


def test_persistent_http_200_guard_keeps_its_classification_after_refresh(monkeypatch):
    client, calls = make_client(monkeypatch, [Reply(text=revo.GUARD_BODY)] * 2)
    with pytest.raises(revo.RevoAccessGuarded):
        client.get_streak_calendar(9, 2026)
    assert len(calls) == 2
    assert client._mobile.calls == [False, True]


@pytest.mark.parametrize("reply", [Reply(429), Reply(503), Reply(403), Reply(302, location="https://untrusted.test/?token=private")])
def test_other_failures_do_not_refresh_or_follow_redirect(monkeypatch, reply):
    client, calls = make_client(monkeypatch, [reply])
    with pytest.raises(revo.RevoUnavailable) as caught:
        client._get(revo.RAFFLE_PATH)
    assert len(calls) == 1
    assert client._mobile.calls == [False]
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("path,params", [("https://untrusted.test", None), (revo.RAFFLE_PATH, {"optval": 1}), (revo.STREAKS_PATH, {"token": "override"})])
def test_bridge_rejects_unknown_destinations_and_mutating_parameters(monkeypatch, path, params):
    client, calls = make_client(monkeypatch, [])
    with pytest.raises(revo.RevoUnavailable):
        client._get_app_feature_locked(path, params=params)
    assert calls == []
    assert client._mobile.calls == []


@pytest.mark.parametrize("error,expected", [(np.NetpulseAuthError, revo.RevoAuthError), (np.NetpulseUnavailable, revo.RevoUnavailable)])
def test_mobile_failure_is_classified_without_secret_detail(monkeypatch, error, expected):
    client, calls = make_client(monkeypatch, [])

    def fail(**kwargs):
        raise error("private-token private@example.test")

    monkeypatch.setattr(client._mobile, "get_rewards_token", fail)
    with pytest.raises(expected) as caught:
        client.get_tickets()
    assert "private" not in str(caught.value)
    assert calls == []


def test_transport_exception_and_log_do_not_expose_token(monkeypatch, caplog):
    client, _ = make_client(monkeypatch, [])

    def fail(*args, **kwargs):
        raise requests.RequestException("https://example.test?token=" + kwargs["params"]["token"])

    monkeypatch.setattr(client._http, "get", fail)
    with pytest.raises(revo.RevoUnavailable) as caught:
        client.get_raffle()
    assert client._mobile.value not in str(caught.value) + caplog.text


def test_concurrent_accounts_keep_their_own_mobile_context(monkeypatch):
    clients = [make_client(monkeypatch, [Reply()] * 3) for _ in range(2)]
    for index, (client, _) in enumerate(clients):
        client._mobile.value = f"account-token-{index}"
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(client._get, revo.RAFFLE_PATH) for client, _ in clients for _ in range(3)]
        assert [f.result() for f in futures] == ["feature"] * 6
    for client, calls in clients:
        assert all(call[1]["params"]["token"] == client._mobile.value for call in calls)


@pytest.mark.parametrize("value", [True, False, "4", 4, -1, "changed"])
def test_unknown_calendar_code_still_rejects_whole_month(value):
    body = json.dumps({"weeks_data": {"week1": {"1": "1", "2": value, "3": "1"}}})
    assert revo.parse_streak_calendar(body) == {}


def test_real_transport_debug_logs_redact_query_token(caplog):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token = "synthetic-secret-jwt"
    try:
        with requests.Session() as session, caplog.at_level(logging.DEBUG, logger="urllib3.connectionpool"):
            response = revo_http.request(
                session, "get", f"http://127.0.0.1:{server.server_port}/feature",
                revo.RevoUnavailable, params={"token": token, "m": 9}, timeout=3,
            )
        assert response.status_code == 200
        assert token in seen[0]
        assert token not in caplog.text
        assert "token=[redacted]" in caplog.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
