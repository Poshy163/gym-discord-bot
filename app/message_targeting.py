"""Helpers for Discord message text that points a lift at another user."""

from __future__ import annotations

import re

_LEADING_USER_MENTION_RE = re.compile(r"^\s*<@!?(\d+)>\s*")


def strip_leading_user_mention(text: str) -> tuple[int | None, str]:
    """Return (mentioned_user_id, text_without_prefix) for '<@id> lift'.

    Discord stores typed @mentions in message.content as '<@123>' or
    '<@!123>'. Only a leading user mention counts as a log target; mentions
    later in the sentence are left alone because they are too ambiguous.
    """
    match = _LEADING_USER_MENTION_RE.match(text or "")
    if not match:
        return None, text
    return int(match.group(1)), text[match.end():].lstrip()
