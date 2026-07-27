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
        self.last_json = None
        self.urls = []

    def post(self, *args, **kwargs):
        self.calls += 1
        self.last_json = kwargs.get("json")
        if args:
            self.urls.append(args[0])
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ok(text="hello"):
    return _Resp(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


def _finish(reason, text=""):
    """A 200 whose candidate carries an explicit finishReason (e.g. a
    truncated MAX_TOKENS reply that may still hold a partial ``text``)."""
    cand: dict = {"finishReason": reason}
    if text:
        cand["content"] = {"parts": [{"text": text}]}
    return _Resp(200, {"candidates": [cand]})


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
    """A client error must not go through the transient-retry loop. With no
    thinkingConfig in play (an explicit budget of None omits it) that means
    exactly one request."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    fake = _FakeRequests([_err(400, "INVALID_ARGUMENT", "bad request")])
    monkeypatch.setattr(gemini_client, "requests", fake)
    with pytest.raises(gemini_client.GeminiError) as ei:
        gemini_client.generate("hi", retries=2, thinking_budget=None,
                               model="gemini-9.9-unknown")
    assert ei.value.status_code == 400 and ei.value.retryable is False
    assert fake.calls == 1  # no retry on a client error


def test_generate_400_drops_thinking_config_and_retries_once(monkeypatch):
    """Model families disagree about whether thinking can be capped, and
    Google answers a bad budget with a bare "invalid argument" naming no field.
    gemini-3.6-flash rejected thinkingBudget=0 and took every AI feature down,
    so a 400 while we're pinning that field earns exactly one retry without
    it."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    fake = _FakeRequests([
        _err(400, "INVALID_ARGUMENT", "Request contains an invalid argument."),
        _ok("recovered"),
    ])
    monkeypatch.setattr(gemini_client, "requests", fake)
    assert gemini_client.generate(
        "hi", retries=0, model="gemini-2.5-flash",
    ) == "recovered"
    assert fake.calls == 2
    assert "thinkingConfig" not in fake.last_json["generationConfig"]


def test_generate_400_reduction_retry_happens_at_most_once(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    fake = _FakeRequests([
        _err(400, "INVALID_ARGUMENT", "Request contains an invalid argument."),
        _err(400, "INVALID_ARGUMENT", "Request contains an invalid argument."),
    ])
    monkeypatch.setattr(gemini_client, "requests", fake)
    with pytest.raises(gemini_client.GeminiError):
        gemini_client.generate("hi", retries=0, model="gemini-2.5-flash")
    assert fake.calls == 2  # not an infinite reduction loop


def test_generate_400_on_a_bad_key_skips_the_reduction_retry(monkeypatch):
    """A rejected key fails identically without thinkingConfig, so spending a
    second request to prove it is pure waste."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    fake = _FakeRequests([
        _err(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."),
    ])
    monkeypatch.setattr(gemini_client, "requests", fake)
    with pytest.raises(gemini_client.GeminiError):
        gemini_client.generate("hi", retries=0, model="gemini-2.5-flash")
    assert fake.calls == 1


def test_generate_retries_transport_error(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    fake = _FakeRequests([_FakeRequests.RequestException("conn reset"), _ok("ok")])
    monkeypatch.setattr(gemini_client, "requests", fake)
    assert gemini_client.generate("hi", retries=1) == "ok"
    assert fake.calls == 2


def test_generate_passes_temperature_and_token_cap(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    fake = _FakeRequests([_ok("hi")])
    monkeypatch.setattr(gemini_client, "requests", fake)
    gemini_client.generate(
        "p", temperature=0.6, max_output_tokens=400, retries=0,
    )
    cfg = fake.last_json["generationConfig"]
    assert cfg["temperature"] == 0.6
    assert cfg["maxOutputTokens"] == 400
    # flash still gets thinking disabled for latency.
    assert cfg["thinkingConfig"]["thinkingBudget"] == 0


def test_generate_omits_token_cap_when_unset(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    fake = _FakeRequests([_ok("hi")])
    monkeypatch.setattr(gemini_client, "requests", fake)
    gemini_client.generate("p", retries=0)
    assert "maxOutputTokens" not in fake.last_json["generationConfig"]


def test_generate_thinking_budget_opt_in(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    fake = _FakeRequests([_ok("hi")])
    monkeypatch.setattr(gemini_client, "requests", fake)
    gemini_client.generate("p", thinking_budget=768, retries=0)
    assert fake.last_json["generationConfig"]["thinkingConfig"][
        "thinkingBudget"
    ] == 768


def test_generate_flash_defaults_thinking_off(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    fake = _FakeRequests([_ok("hi")])
    monkeypatch.setattr(gemini_client, "requests", fake)
    gemini_client.generate("p", retries=0)
    assert fake.last_json["generationConfig"]["thinkingConfig"][
        "thinkingBudget"
    ] == 0


def test_generate_non_flash_gets_thinking_floor(monkeypatch):
    # Non-flash models (e.g. pro) can't disable thinking; without a floor the
    # default pass eats the token cap and truncates the reply. We pin the
    # 128-token minimum so a caller-set max_output_tokens keeps real headroom.
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-pro")
    fake = _FakeRequests([_ok("hi")])
    monkeypatch.setattr(gemini_client, "requests", fake)
    gemini_client.generate("p", retries=0)
    assert fake.last_json["generationConfig"]["thinkingConfig"][
        "thinkingBudget"
    ] == 128


def test_generate_non_flash_respects_explicit_budget(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-pro")
    fake = _FakeRequests([_ok("hi")])
    monkeypatch.setattr(gemini_client, "requests", fake)
    gemini_client.generate("p", thinking_budget=256, retries=0)
    assert fake.last_json["generationConfig"]["thinkingConfig"][
        "thinkingBudget"
    ] == 256


def test_thinking_budget_for():
    # flash/flash-lite default OFF; everything else floors at pro's minimum;
    # an explicit request always wins.
    assert gemini_client._thinking_budget_for("gemini-2.5-flash", None) == 0
    assert gemini_client._thinking_budget_for("gemini-2.5-flash-lite", None) == 0
    assert gemini_client._thinking_budget_for("gemini-2.5-pro", None) == 128
    assert gemini_client._thinking_budget_for("gemini-2.5-pro", 768) == 768
    assert gemini_client._thinking_budget_for("gemini-2.5-flash", 512) == 512


def test_generate_json_mime_passthrough(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    fake = _FakeRequests([_ok("{}")])
    monkeypatch.setattr(gemini_client, "requests", fake)
    gemini_client.generate("p", response_mime_type="application/json", retries=0)
    assert fake.last_json["generationConfig"][
        "responseMimeType"
    ] == "application/json"


def test_generate_falls_back_to_backup_model(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("BACKUP_GEMINI_MODEL", "gemini-2.5-flash-lite")
    # Primary is overloaded (no retries), backup answers.
    fake = _FakeRequests([_err(503, "UNAVAILABLE"), _ok("from backup")])
    monkeypatch.setattr(gemini_client, "requests", fake)
    assert gemini_client.generate("hi", retries=0) == "from backup"
    assert fake.calls == 2
    assert "gemini-2.5-flash:" in fake.urls[0]
    assert "gemini-2.5-flash-lite:" in fake.urls[1]


def test_generate_no_backup_fallback_on_client_error(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("BACKUP_GEMINI_MODEL", "gemini-2.5-flash-lite")
    # A 400 is a client error — the backup model must NOT be tried. Use an
    # explicit budget of None so no thinkingConfig is sent and the
    # drop-and-retry path can't add a request here.
    fake = _FakeRequests([_err(400, "INVALID_ARGUMENT"), _ok("unused")])
    monkeypatch.setattr(gemini_client, "requests", fake)
    with pytest.raises(gemini_client.GeminiError):
        gemini_client.generate("hi", retries=0, thinking_budget=None,
                               model="gemini-9.9-unknown")
    assert fake.calls == 1


def test_generate_backup_ignored_when_same_as_primary(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("BACKUP_GEMINI_MODEL", "gemini-2.5-flash")
    fake = _FakeRequests([_err(503, "UNAVAILABLE")])
    monkeypatch.setattr(gemini_client, "requests", fake)
    with pytest.raises(gemini_client.GeminiError):
        gemini_client.generate("hi", retries=0)
    assert fake.calls == 1  # no duplicate model attempt


def test_retry_delay_fixed_overrides_backoff(monkeypatch):
    monkeypatch.setenv("GEMINI_RETRY_DELAY", "2.5")
    assert gemini_client._retry_delay(0) == 2.5
    assert gemini_client._retry_delay(9) == 2.5  # fixed, ignores attempt
    monkeypatch.delenv("GEMINI_RETRY_DELAY", raising=False)
    assert gemini_client._retry_delay(0) == gemini_client._backoff(0)


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


# --- finishReason handling (truncated / cut-off replies) -------------------

def test_generate_max_tokens_partial_text_raises(monkeypatch):
    # A truncated reply (finishReason=MAX_TOKENS) that still carries a partial
    # JSON fragment must NOT be returned as-is — it would parse as garbage
    # downstream. Fail with a MAX_TOKENS GeminiError instead.
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    fake = _FakeRequests([_finish("MAX_TOKENS", '{"kcal": 90, "name": "flat wh')])
    monkeypatch.setattr(gemini_client, "requests", fake)
    with pytest.raises(gemini_client.GeminiError) as ei:
        gemini_client.generate("hi", retries=0)
    assert ei.value.status == "MAX_TOKENS"
    assert ei.value.retryable is True


def test_generate_max_tokens_empty_content_raises(monkeypatch):
    # Thinking-exhausted case: candidate has a finishReason but no content/parts.
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    fake = _FakeRequests([_finish("MAX_TOKENS")])
    monkeypatch.setattr(gemini_client, "requests", fake)
    with pytest.raises(gemini_client.GeminiError) as ei:
        gemini_client.generate("hi", retries=0)
    assert ei.value.status == "MAX_TOKENS"


def test_generate_max_tokens_falls_back_to_backup(monkeypatch):
    # A cut-off primary is retryable, so a configured backup model gets a shot.
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-pro")
    monkeypatch.setenv("BACKUP_GEMINI_MODEL", "gemini-2.5-flash")
    fake = _FakeRequests([_finish("MAX_TOKENS", "{partial"), _ok("from backup")])
    monkeypatch.setattr(gemini_client, "requests", fake)
    assert gemini_client.generate("hi", retries=0) == "from backup"
    assert fake.calls == 2


def test_friendly_message_maps_max_tokens():
    msg = gemini_client.friendly_message(
        gemini_client.GeminiError("cut off", status="MAX_TOKENS")
    ).lower()
    assert "cut off" in msg and "try again" in msg


# --- diagnosability: the operator must be able to see what actually failed ---

def test_failure_is_logged_with_googles_own_wording(monkeypatch, caplog):
    """Every call site swaps a GeminiError for short friendly copy and drops
    the exception, so without a log here the real diagnosis never surfaces and
    the operator sees only "the AI couldn't process that request"."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_MAX_RETRIES", "0")
    monkeypatch.setattr(gemini_client, "MAX_RETRIES", 0)
    fake = _FakeRequests([
        _err(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."),
    ])
    monkeypatch.setattr(gemini_client, "requests", fake)

    with caplog.at_level("WARNING", logger="gymbot.gemini"):
        with pytest.raises(gemini_client.GeminiError):
            gemini_client.generate("hi")

    logged = caplog.text
    assert "API key not valid" in logged      # Google's message, verbatim
    assert "INVALID_ARGUMENT" in logged
    assert "400" in logged


def test_failure_log_never_contains_the_api_key(monkeypatch, caplog):
    """The key rides in the query string, never the body — and must never end
    up in a log line either."""
    secret = "AIzaSuperSecret12345"
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    monkeypatch.setattr(gemini_client, "MAX_RETRIES", 0)
    monkeypatch.setattr(
        gemini_client, "requests",
        _FakeRequests([_err(403, "PERMISSION_DENIED", "nope")]),
    )
    with caplog.at_level("INFO", logger="gymbot.gemini"):
        with pytest.raises(gemini_client.GeminiError):
            gemini_client.generate("hi")
    assert secret not in caplog.text


def test_friendly_message_distinguishes_a_rejected_key_from_a_bad_request():
    """Google reports a rejected key as 400 INVALID_ARGUMENT, not 401, so the
    old blanket "couldn't process that request" sent operators hunting for a
    bad prompt when the fault was configuration."""
    rejected = gemini_client.GeminiError(
        "API key not valid. Please pass a valid API key.",
        status_code=400, status="INVALID_ARGUMENT",
    )
    other = gemini_client.GeminiError(
        "Unknown name 'thinkingConfig'", status_code=400,
        status="INVALID_ARGUMENT",
    )
    assert "API key was rejected" in gemini_client.friendly_message(rejected)
    assert "API key was rejected" not in gemini_client.friendly_message(other)
    assert "configuration problem" in gemini_client.friendly_message(other)


def test_friendly_message_names_a_retired_model():
    exc = gemini_client.GeminiError(
        "models/gemini-2.5-flash is not found for API version v1beta",
        status_code=404, status="NOT_FOUND",
    )
    assert "doesn't exist" in gemini_client.friendly_message(exc)


def test_every_friendly_message_leads_with_the_ai_icon():
    """The callers prefix their own context, so a message that dropped the icon
    (or carried two) would render inconsistently."""
    for exc in (
        gemini_client.GeminiError("x", status_code=503, status="UNAVAILABLE"),
        gemini_client.GeminiError("x", status_code=429),
        gemini_client.GeminiError("x", status_code=400, status="INVALID_ARGUMENT"),
        gemini_client.GeminiError("x", status_code=404),
        gemini_client.GeminiError("GEMINI_API_KEY is not set."),
        gemini_client.GeminiError("something else entirely"),
    ):
        msg = gemini_client.friendly_message(exc)
        assert msg.startswith("🤖 "), msg
        assert msg.count("🤖") == 1, msg
