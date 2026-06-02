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

LOG = logging.getLogger("gymbot.gemini")

try:  # pragma: no cover - trivial import guard
    import requests  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    requests = None  # type: ignore[assignment]

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-2.5-flash"
REQUEST_TIMEOUT = 60


class GeminiError(RuntimeError):
    """Raised when a Gemini request can't be completed."""


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
) -> str:
    """Send ``prompt`` to Gemini and return the model's text reply.

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
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}

    try:
        resp = requests.post(
            url, params={"key": key}, json=body, timeout=timeout,
        )
    except requests.RequestException as exc:  # type: ignore[union-attr]
        raise GeminiError(f"Request to Gemini failed: {exc}") from exc

    if resp.status_code != 200:
        # The key itself is in the query string; keep it out of the surfaced
        # message by only echoing the body snippet.
        raise GeminiError(
            f"Gemini API returned {resp.status_code}: {resp.text[:300]}"
        )

    try:
        data = resp.json()
        candidate = data["candidates"][0]
        parts = candidate["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, ValueError) as exc:
        raise GeminiError(f"Unexpected Gemini response shape: {exc}") from exc

    if not text:
        reason = candidate.get("finishReason", "")
        raise GeminiError(f"Gemini returned no text (finishReason={reason!r}).")
    return text
