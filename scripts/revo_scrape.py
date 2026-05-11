"""Revo portal smoke-test CLI (uses :mod:`app.revo_client`).

Usage:
    set REVO_USER=you@example.com
    set REVO_PASS=secret
    py scripts/revo_scrape.py

Prints the live club counter, the logged-in member's streak weeks, and the
ticket-tally history. Useful when reverse-engineering changes to the portal —
exercise the parsers against the live HTML without spinning up the bot.

This is a research helper, not part of the bot's runtime path.
"""
from __future__ import annotations

import os
import sys

# Allow `py scripts/revo_scrape.py` from the repo root by inserting the
# project root onto sys.path before importing app.*.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app import revo_client  # noqa: E402


def main() -> int:
    email = os.environ.get("REVO_USER", "").strip()
    password = os.environ.get("REVO_PASS", "").strip()
    if not email or not password:
        print("Set REVO_USER and REVO_PASS env vars.", file=sys.stderr)
        return 2

    client = revo_client.RevoClient(email, password)
    try:
        client.login()
    except revo_client.RevoAuthError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1

    print(f"Logged in: member_id={client.member_id} level={client.membership_level}")

    clubs, favorite = client.get_club_counter()
    print(f"\nFavorite club id: {favorite}")
    print("Top 10 busiest right now:")
    for c in sorted(clubs.values(), key=lambda x: x.in_club, reverse=True)[:10]:
        print(f"  {c.name:<25} id={c.club_id:<3} in_club={c.in_club}")

    streak = client.get_streak_weeks()
    print(f"\nStreak: {streak} weeks")

    avail, rows = client.get_tickets()
    print(f"\nTickets available: {avail}")
    print("Last 5 attendance entries:")
    for r in rows[:5]:
        print(f"  {r.date}  +{r.delta}  {r.source}")

    raffle = client.get_raffle()
    print(f"\nRaffle: {raffle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
