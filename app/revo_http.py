"""Shared response checks for the three independent Revo sessions."""
from __future__ import annotations

import re
import logging
from functools import wraps


class _RedactQueryTokens(logging.Filter):
    """urllib3 DEBUG request lines otherwise disclose WebView query credentials."""

    def filter(self, record):
        message = record.getMessage()
        redacted = re.sub(r"([?&](?:token|access_token)=)[^&\s\"'<>]+",
                          r"\1[redacted]", message, flags=re.I)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


# Filters must attach to emitting loggers, not their ancestors: propagated
# records bypass ancestor filters. Keep useful transport diagnostics enabled.
for _logger_name in ("urllib3.connectionpool", "urllib3.util.retry"):
    logging.getLogger(_logger_name).addFilter(_RedactQueryTokens())


def serialized(fn):
    """Protect session-backed profile reads using the client's re-entrant lock."""
    @wraps(fn)
    def read(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return read


def is_login_html(response) -> bool:
    """Recognise a password form returned as HTTP 200, without reading JSON as HTML."""
    text = getattr(response, "text", "")
    if not isinstance(text, str):
        return False
    return bool(re.search(r"<input\b[^>]*\btype\s*=\s*['\"]?password\b", text, re.I))


def request(session, method: str, url: str, error_type, **kwargs):
    """Keep request URLs, signed query strings and response bodies out of errors."""
    try:
        return getattr(session, method)(url, **kwargs)
    except Exception as exc:
        raise error_type(f"Revo {method.upper()} failed ({type(exc).__name__}).") from None


def check_status(response, error_type):
    status = response.status_code
    if status == 429:
        raise error_type("Revo rate limit reached. Try again later.")
    if status >= 400:
        raise error_type(f"Revo service returned HTTP {status}.")
