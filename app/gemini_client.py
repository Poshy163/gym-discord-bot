"""Minimal Google Gemini (Generative Language API) client.

Used by ``/track analyze`` to summarise a user's recorded sleep data into
plain-language trends. Kept free of any Discord imports and import-safe even
when ``requests`` isn't installed — callers should check :func:`available`
(or catch :class:`GeminiError`) before relying on it.

Configuration comes from the environment:

    GEMINI_API_KEY=AIz...           # required to enable the feature
    GEMINI_MODEL=gemini-2.5-flash   # optional, defaults below
"""
from __future__ import annotations

import logging
import os
import time

LOG = logging.getLogger("gymbot.gemini")

try:  # pragma: no cover - trivial import guard
    import requests  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    requests = None  # type: ignore[assignment]

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-2.5-flash"
# Generation can be slow — 2.5 models "think" before answering, and the first
# byte often doesn't arrive for a while. Allow an env override for slow links.
REQUEST_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "120"))
# Transient server-side conditions worth a quick retry before giving up. 503 is
# the common "model is busy / high demand" spike; 429 is rate-limiting; 500/504
# are upstream blips.
RETRYABLE_STATUS = {429, 500, 503, 504}
MAX_RETRIES = max(0, int(os.getenv("GEMINI_MAX_RETRIES", "2")))


class GeminiError(RuntimeError):
    """Raised when a Gemini request can't be completed.

    Carries the HTTP ``status_code`` and Google ``status`` string (e.g.
    ``UNAVAILABLE``) when they're known, plus ``retryable`` so callers can tell
    a transient overload apart from a config/auth problem. Use
    :func:`friendly_message` to turn one into user-facing copy.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        status: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.status = status
        self.retryable = retryable


def api_key() -> str | None:
    """Return the configured API key, or None if unset/blank."""
    return os.getenv("GEMINI_API_KEY", "").strip() or None


def model_name() -> str:
    """Return the configured model, falling back to :data:`DEFAULT_MODEL`."""
    return os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODEL


def available() -> bool:
    """True if the HTTP dependency is present and an API key is configured."""
    return requests is not None and api_key() is not None


def generate(
    prompt: str,
    *,
    system: str | None = None,
    model: str | None = None,
    timeout: int = REQUEST_TIMEOUT,
    retries: int = MAX_RETRIES,
    temperature: float = 0.4,
    max_output_tokens: int | None = None,
) -> str:
    """Send ``prompt`` to Gemini and return the model's text reply.

    ``temperature`` tunes creativity (lower = more focused/precise, higher =
    warmer/more varied) and ``max_output_tokens`` caps the reply length — both
    let callers shape the response per feature. Transient failures
    (HTTP 429/500/503/504 or transport errors) are retried up to ``retries``
    times with a short backoff before giving up.

    Raises :class:`GeminiError` for missing config, transport failures, non-200
    responses, or an empty/blocked completion.
    """
    if requests is None:
        raise GeminiError("The 'requests' package isn't installed.")
    key = api_key()
    if key is None:
        raise GeminiError("GEMINI_API_KEY is not set.")
    mdl = model or model_name()
    url = f"{API_ROOT}/models/{mdl}:generateContent"
    gen_config: dict = {"temperature": temperature}
    if max_output_tokens is not None:
        gen_config["maxOutputTokens"] = max_output_tokens
    # 2.5 *flash* models default to an extended "thinking" pass that adds tens
    # of seconds of latency (the usual cause of read timeouts here). A trend
    # summary doesn't need it, so switch it off. Only flash/flash-lite accept a
    # zero budget — pro rejects it — so gate on the model name.
    if "flash" in mdl.lower():
        gen_config["thinkingConfig"] = {"thinkingBudget": 0}
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": gen_config,
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}

    last_exc: GeminiError | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                url, params={"key": key}, json=body, timeout=timeout,
            )
        except requests.RequestException as exc:  # type: ignore[union-attr]
            # Transport failures (DNS, connection reset, read timeout) are
            # usually transient too, so they share the retry path.
            last_exc = GeminiError(
                f"Couldn't reach Gemini: {exc}", retryable=True,
            )
            if attempt < retries:
                LOG.info(
                    "Gemini transport error, retrying (%d/%d): %s",
                    attempt + 1, retries, exc,
                )
                time.sleep(_backoff(attempt))
                continue
            raise last_exc from exc

        if resp.status_code == 200:
            return _parse_completion(resp)

        # Non-200: pull Google's structured error out of the body. The API key
        # is in the query string, never the body, so this is safe to surface.
        message, status = _extract_api_error(resp)
        retryable = resp.status_code in RETRYABLE_STATUS
        last_exc = GeminiError(
            message or f"Gemini API returned {resp.status_code}",
            status_code=resp.status_code,
            status=status or None,
            retryable=retryable,
        )
        if retryable and attempt < retries:
            LOG.info(
                "Gemini %s (%s), retrying (%d/%d)",
                resp.status_code, status or "?", attempt + 1, retries,
            )
            time.sleep(_backoff(attempt))
            continue
        raise last_exc

    # Loop only exits via return/raise above, but keep mypy + safety happy.
    raise last_exc or GeminiError("Gemini request failed.")  # pragma: no cover


def _backoff(attempt: int) -> float:
    """Seconds to wait before retry ``attempt`` (0-based): 1.5s, 3s, 4.5s…"""
    return 1.5 * (attempt + 1)


def _extract_api_error(resp) -> tuple[str, str]:
    """Best-effort ``(message, status)`` from a non-200 response body."""
    try:
        err = resp.json().get("error", {}) or {}
        return (
            str(err.get("message") or "").strip(),
            str(err.get("status") or "").strip(),
        )
    except (ValueError, AttributeError):
        return (resp.text or "")[:200].strip(), ""


def _parse_completion(resp) -> str:
    try:
        data = resp.json()
        candidate = data["candidates"][0]
        parts = candidate["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, ValueError) as exc:
        raise GeminiError(f"Unexpected Gemini response shape: {exc}") from exc

    if not text:
        reason = candidate.get("finishReason", "")
        raise GeminiError(
            f"Gemini returned no text (finishReason={reason!r}).",
            status=str(reason) or None,
        )
    return text


def friendly_message(exc: GeminiError) -> str:
    """User-facing copy for a :class:`GeminiError`, hiding the raw API noise.

    Maps the common failure modes (overload, rate-limit, auth, content filter)
    to a short, friendly line suitable for a Discord reply.
    """
    code = getattr(exc, "status_code", None)
    status = (getattr(exc, "status", None) or "").upper()
    if code == 503 or status == "UNAVAILABLE":
        return (
            "🤖 The AI model is swamped right now (high demand). "
            "Give it a minute and try again."
        )
    if code == 429 or status == "RESOURCE_EXHAUSTED":
        return (
            "🤖 The AI is rate-limited at the moment — too many requests. "
            "Try again in a little while."
        )
    if code in (500, 504) or status in ("INTERNAL", "DEADLINE_EXCEEDED"):
        return "🤖 The AI had a temporary hiccup on Google's end. Please try again."
    if code in (401, 403) or status in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
        return (
            "🤖 The AI isn't set up correctly (API key/permissions). "
            "Let the bot owner know."
        )
    if code == 400 or status == "INVALID_ARGUMENT":
        return "🤖 The AI couldn't process that request."
    msg = str(exc).lower()
    if "not installed" in msg or "is not set" in msg:
        return "🤖 AI features aren't configured on this bot."
    if "no text" in msg or "finishreason" in msg or "safety" in msg:
        return (
            "🤖 The AI didn't return a usable answer (it may have been "
            "filtered). Try again."
        )
    return "🤖 The AI request failed — please try again in a bit."
