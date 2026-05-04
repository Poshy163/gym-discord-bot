"""Tests for small pure helpers inside app.bot.

We avoid importing discord runtime state by only touching pure functions.
"""

from __future__ import annotations

import os

# Ensure the bot doesn't try to connect on import — DISCORD_TOKEN isn't
# read until run() so we mainly need a stable DB path.
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("DISCORD_TOKEN", "test-token-not-used")

from app.bot import _rejected_lifts_note, _safe_label  # noqa: E402
from app.parser import Lift  # noqa: E402


def test_safe_label_escapes_mentions():
    out = _safe_label("@everyone bench")
    assert "@everyone" not in out
    # discord.utils.escape_mentions inserts a zero-width space between '@'
    # and the keyword; the literal trigger string must not survive.


def test_safe_label_escapes_markdown():
    out = _safe_label("**bench** _press_")
    # Asterisks/underscores should be backslash-escaped so they don't bold.
    assert "\\*" in out
    assert "\\_" in out


def test_safe_label_truncates_long_input():
    out = _safe_label("a" * 500, limit=20)
    assert len(out) <= 20
    assert out.endswith("…")


def test_safe_label_handles_empty():
    assert _safe_label("") == "(unknown)"
    assert _safe_label("   ") == "(unknown)"


def test_safe_label_strips_newlines():
    out = _safe_label("bench\npress")
    assert "\n" not in out


def test_rejected_lifts_note_sanitizes_equipment():
    rejected = [
        Lift(
            equipment="@everyone bench **boom**",
            weight_kg=9999.0,
            raw="@everyone bench **boom**: 9999kg",
            confident=True,
        ),
    ]
    note = _rejected_lifts_note(rejected)
    assert "@everyone" not in note
    # Markdown bold from the label must be escaped, but our own ** wrapper
    # around the label remains.
    assert "\\*\\*boom\\*\\*" in note


def test_rejected_lifts_note_empty_returns_blank():
    assert _rejected_lifts_note([]) == ""
