"""Report which Revo data sources are currently working.

Why this exists: the Revo portal fails **silently**. Its ``Invalid Access! B``
access guard answers with HTTP 200 (see ``docs/REVO_PORTAL.md`` §1.2), so a page
can stop working while every request still looks like a success — which is
exactly how the attendance tracker went quiet in 2026-08. Nothing raised; the
check-in calendar simply parsed to empty.

Run this to see, in one pass, whether check-in tracking is healthy, degraded
(running on the ticket-tally fallback) or down — and to tell when Revo lifts a
block so normal per-day tracking resumes.

Usage (from the repo root, with credentials in the environment)::

    REVO_USER=you@example.com REVO_PASS=... python scripts/revo_health.py

Exit codes: ``0`` check-in tracking healthy · ``1`` degraded (fallback in use) ·
``2`` down · ``3`` couldn't log in / no credentials. That makes it usable as a
cron canary, not just something a human reads.

It costs one request per source, so run it on demand — the portal notes ask for
gentle traffic (§6). This is a diagnostic, not a poll.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app import revo_client  # noqa: E402

# Plain ASCII markers: this often runs over SSH / in a container where the
# console encoding mangles emoji (Windows cp1252 raises outright).
_MARK = {
    revo_client.HEALTH_OK: "[ OK ]",
    revo_client.HEALTH_EMPTY: "[EMPTY]",
    revo_client.HEALTH_GUARDED: "[BLOCKED]",
    revo_client.HEALTH_ERROR: "[FAIL]",
}
_EXIT = {"ok": 0, "degraded": 1, "down": 2}


def main() -> int:
    email = os.environ.get("REVO_USER", "").strip()
    password = os.environ.get("REVO_PASS", "").strip()
    if not email or not password:
        print("Set REVO_USER and REVO_PASS.", file=sys.stderr)
        return 3
    if not revo_client.available():
        print("Install 'requests' first (pip install requests).", file=sys.stderr)
        return 3

    client = revo_client.RevoClient(email, password)
    try:
        client.login()
    except Exception as exc:
        print(f"Login FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    print(f"Logged in: member_id={client.member_id} level={client.membership_level}\n")

    now = datetime.now()
    sources = revo_client.probe_sources(client, now.month, now.year)
    state, explanation = revo_client.attendance_feed_state(sources)

    print(f"Check-in tracking: {state.upper()} - {explanation}\n")
    width = max(len(s.label) for s in sources)
    for s in sources:
        detail = f"  {s.detail}" if s.detail else ""
        print(f"  {_MARK.get(s.status, '[????]'):9s} {s.label:{width}s}{detail}")

    if any(s.status == revo_client.HEALTH_GUARDED for s in sources):
        print(
            "\nNote: [BLOCKED] is Revo refusing the page server-side, not a "
            "problem with the account, the network or this bot. It is not "
            "client-settable and there is nothing to retry — see "
            "docs/REVO_PORTAL.md section 1.2."
        )
    return _EXIT.get(state, 2)


if __name__ == "__main__":
    raise SystemExit(main())
