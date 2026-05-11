"""One-shot probe of the Revo portal looking for undocumented endpoints.

Goals:
  * Dump the full streaks.php HTML so we can find per-day check-in markers.
  * Grep every fetched page for fetch()/XHR/url-like strings → candidate routes.
  * Probe a list of plausible endpoints (calendar/json/ajax variants).
  * Try a few HTTP verbs on the rewards namespace.

Run from the repo root with REVO_USER / REVO_PASS set in env. Output goes to
``scripts/_revo_probe_out/`` (gitignored — add to .gitignore if not already).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app import revo_client  # noqa: E402

OUT = _REPO_ROOT / "scripts" / "_revo_probe_out"
OUT.mkdir(exist_ok=True)

# Endpoints to probe. Anything that 200s with a non-trivial body is interesting.
CANDIDATES = [
    # streak / calendar variants
    "/portal/rewards/streaks.php?month=2026-04",
    "/portal/rewards/streaks.php?month=04&year=2026",
    "/portal/rewards/streaks.php?date=2026-04-01",
    "/portal/rewards/streaks-data.php",
    "/portal/rewards/streaks.json",
    "/portal/rewards/calendar.php",
    "/portal/rewards/calendar.json",
    "/portal/rewards/streak-calendar.php",
    "/portal/rewards/check-ins.php",
    "/portal/rewards/checkins.php",
    "/portal/rewards/visits.php",
    "/portal/rewards/history.php",
    "/portal/rewards/attendance.php",
    "/portal/rewards/streak-history.php",
    # ticket pagination
    "/portal/rewards/ticket-tally.php?page=2",
    "/portal/rewards/ticket-tally.php?offset=20",
    "/portal/rewards/ticket-tally.php?all=1",
    "/portal/rewards/ticket-tally-data.php",
    "/portal/rewards/tickets.json",
    # member / profile shapes
    "/portal/rewards/member.php",
    "/portal/rewards/profile.php",
    "/portal/rewards/me.php",
    "/portal/me.php",
    "/portal/member.php",
    "/portal/user.php",
    # club-counter variants we haven't tried yet
    "/portal/club-counter.php?club=25",
    "/portal/club-counter.php?id=25",
    "/portal/club-counter-history.php",
    "/portal/club-counter-week.php",
    "/portal/club-counter-trends.php",
    # generic
    "/portal/rewards/index.json",
    "/portal/rewards/dashboard.php",
    "/portal/rewards/summary.php",
    "/robots.txt",
    "/sitemap.xml",
    "/portal/sitemap.xml",
    "/portal/api/v1/streaks",
    "/portal/api/v1/tickets",
    "/portal/api/v1/me",
]


def _save(name: str, body: str) -> None:
    (OUT / name).write_text(body, encoding="utf-8", errors="replace")


def _interesting(body: str) -> bool:
    if not body:
        return False
    low = body.lower()
    # Login form / upgrade page → not interesting.
    if "level-two-feature" in low and len(body) < 5000:
        return False
    if "<form" in low and "password" in low and len(body) < 5000:
        return False
    return True


def _grep_urls(html: str) -> set[str]:
    """Pull URL-like strings out of inline JS / attributes."""
    out: set[str] = set()
    # quoted paths
    for m in re.finditer(r"""['"]((?:/portal/|/api/|/rewards/)[^'"\s>]{2,200})['"]""", html):
        out.add(m.group(1))
    # fetch/ajax calls
    for m in re.finditer(r"""(?:fetch|\.get|\.post|\$\.ajax|XMLHttpRequest)[^;]{0,200}?['"]([^'"\s]{2,200})['"]""", html):
        out.add(m.group(1))
    return out


def main() -> int:
    email = os.environ.get("REVO_USER", "").strip()
    password = os.environ.get("REVO_PASS", "").strip()
    if not email or not password:
        print("Set REVO_USER and REVO_PASS.", file=sys.stderr)
        return 2

    client = revo_client.RevoClient(email, password)
    client.login()
    print(f"Logged in: member_id={client.member_id} level={client.membership_level}")
    sess = client._http  # noqa: SLF001 — research script

    # 1. Dump the known pages in full.
    base = "https://revocentral.revofitness.com.au"
    known = {
        "streaks.html": "/portal/rewards/streaks.php",
        "ticket-tally.html": "/portal/rewards/ticket-tally.php",
        "rewards-index.html": "/portal/rewards/",
        "club-counter.html": "/portal/club-counter.php",
        "raffle.html": "/portal/rewards/raffle.php",
        "prize-pool.html": "/portal/rewards/prize-pool.php",
    }
    found_urls: set[str] = set()
    for name, path in known.items():
        r = sess.get(base + path, timeout=20, allow_redirects=False)
        print(f"GET {path:50s} -> {r.status_code} ({len(r.text)} bytes)")
        if r.status_code == 200:
            _save(name, r.text)
            found_urls |= _grep_urls(r.text)

    print(f"\nURL-like strings found in known pages ({len(found_urls)}):")
    for u in sorted(found_urls):
        print(f"  {u}")

    # 2. Probe candidate endpoints.
    print("\n--- candidate probe ---")
    interesting_paths: list[str] = []
    for path in CANDIDATES:
        try:
            r = sess.get(base + path, timeout=15, allow_redirects=False)
        except Exception as exc:
            print(f"GET {path:60s} -> ERR {exc}")
            continue
        loc = r.headers.get("Location", "")
        marker = ""
        if r.status_code == 200 and _interesting(r.text):
            marker = "  *INTERESTING*"
            interesting_paths.append(path)
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", path).strip("_")[:80]
            _save(f"probe_{safe}.html", r.text)
        print(f"GET {path:60s} -> {r.status_code} loc={loc[:60]} bytes={len(r.text)}{marker}")

    # 3. Also probe the same set with a XHR-flavoured Accept header (some
    # backends gate JSON behind it).
    print("\n--- candidate probe (Accept: application/json, X-Requested-With) ---")
    headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
    for path in CANDIDATES:
        try:
            r = sess.get(base + path, headers=headers, timeout=15, allow_redirects=False)
        except Exception:
            continue
        ctype = r.headers.get("Content-Type", "")
        if r.status_code == 200 and ("json" in ctype.lower() or r.text.strip().startswith(("{", "["))):
            print(f"  *JSON?* {path:55s} ct={ctype} bytes={len(r.text)}")
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", path).strip("_")[:80]
            _save(f"json_{safe}.txt", r.text)

    print("\nDone. HTML/JSON dumps in", OUT)
    if interesting_paths:
        print("Interesting candidates:")
        for p in interesting_paths:
            print(f"  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
