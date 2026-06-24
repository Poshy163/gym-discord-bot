"""Tests for app.gemini_client config helpers (no network)."""

from __future__ import annotations

import pytest

from app import gemini_client


def test_model_name_defaults(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert gemini_client.model_name() == gemini_client.DEFAULT_MODEL


def test_model_name_from_env(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-pro")
    assert gemini_client.model_name() == "gemini-2.5-pro"


def test_model_name_blank_falls_back(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "   ")
    assert gemini_client.model_name() == gemini_client.DEFAULT_MODEL


def test_api_key_unset(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert gemini_client.api_key() is None


def test_api_key_strips(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "  AIzTEST  ")
    assert gemini_client.api_key() == "AIzTEST"


def test_available_requires_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert gemini_client.available() is False


def test_generate_without_key_raises(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # Only meaningful when requests is installed; otherwise the dep guard fires
    # first, which is still a GeminiError.
    with pytest.raises(gemini_client.GeminiError):
        gemini_client.generate("hi")


# --- error handling, retries, and friendly copy ---------------------------

class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class _FakeRequests:
    """Stand-in for the ``requests`` module: returns canned responses (or
    raises) in order, and counts calls."""

    class RequestException(Exception):
        pass

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ok(text="hello"):
    return _Resp(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


def _err(code, status, msg="boom"):
    return _Resp(code, {"error": {"code": code, "message": msg, "status": status}})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(gemini_client.time, "sleep", lambda _s: None)


def test_generate_retries_503_then_succeeds(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    fake = _FakeRequests([_err(503, "UNAVAILABLE", "high demand"), _ok("done")])
    monkeypatch.setattr(gemini_client, "requests", fake)
    assert gemini_client.generate("hi", retries=2) == "done"
    assert fake.calls == 2  # one retry


def test_generate_503_exhausts_retries(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    fake = _FakeRequests([_err(503, "UNAVAILABLE") for _ in range(3)])
    monkeypatch.setattr(gemini_client, "requests", fake)
    with pytest.raises(gemini_client.GeminiError) as ei:
        gemini_client.generate("hi", retries=2)
    assert ei.value.status_code == 503
    assert ei.value.status == "UNAVAILABLE"
    assert ei.value.retryable is True
    assert fake.calls == 3  # initial + 2 retries


def test_generate_400_not_retried(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    fake = _FakeRequests([_err(400, "INVALID_ARGUMENT", "bad request")])
    monkeypatch.setattr(gemini_client, "requests", fake)
    with pytest.raises(gemini_client.GeminiError) as ei:
        gemini_client.generate("hi", retries=2)
    assert ei.value.status_code == 400 and ei.value.retryable is False
    assert fake.calls == 1  # no retry on a client error


def test_generate_retries_transport_error(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    fake = _FakeRequests([_FakeRequests.RequestException("conn reset"), _ok("ok")])
    monkeypatch.setattr(gemini_client, "requests", fake)
    assert gemini_client.generate("hi", retries=1) == "ok"
    assert fake.calls == 2


def test_friendly_message_maps_known_failures():
    fm = gemini_client.friendly_message
    assert "demand" in fm(
        gemini_client.GeminiError("x", status_code=503, status="UNAVAILABLE")
    ).lower()
    assert "rate-limit" in fm(
        gemini_client.GeminiError("x", status_code=429, status="RESOURCE_EXHAUSTED")
    ).lower()
    assert "owner" in fm(
        gemini_client.GeminiError("x", status_code=403, status="PERMISSION_DENIED")
    ).lower()
    assert "configured" in fm(
        gemini_client.GeminiError("GEMINI_API_KEY is not set.")
    ).lower()
    # Unknown errors get the safe generic line, never the raw text.
    assert "failed" in fm(gemini_client.GeminiError("weird internal detail")).lower()
