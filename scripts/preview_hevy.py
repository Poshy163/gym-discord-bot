"""Render the real Hevy embeds for a live account, as a Discord-styled page.

The Hevy cards are the hardest ones in the bot to review from source: they are
assembled from four live payloads (workouts, routines, the template catalogue
and the workout count) and the interesting cases — an exercise with seven
assisting muscles, a routine whose notes run long, a milestone workout — only
appear against real data. This fetches an account's own data and lays every card
out using the same renderer as ``scripts/preview_ui.py``.

    python scripts/preview_hevy.py <api-key>          # writes previews/hevy.html
    python scripts/preview_hevy.py <api-key> --open   # ...and opens it
    HEVY_API_KEY=... python scripts/preview_hevy.py

Read-only: every call is a GET, so this cannot touch the account's training log.
Unlike ``preview_ui.py`` it does import ``app.bot`` (that is where the embed
builders live), which wants a token and a database path present — dummies are
enough, nothing connects.
"""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# app.bot reads these at import time. Neither is used: no client is started and
# the database is never opened, the module just refuses to import without them.
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("DISCORD_TOKEN", "preview-not-used")

import app.bot as bot  # noqa: E402
from app import hevy_client  # noqa: E402

from preview_ui import build_page  # noqa: E402

OUT = ROOT / "previews" / "hevy.html"


def _cases(api_key: str) -> list[tuple[str, list]]:
    templates = hevy_client.index_templates(
        hevy_client.fetch_exercise_templates(api_key),
    )
    routines_raw = hevy_client.fetch_routines(api_key)
    folders = hevy_client.fetch_routine_folders(api_key)
    routines = hevy_client.index_routines(routines_raw, folders)
    total = hevy_client.fetch_workout_count(api_key)
    workouts = hevy_client.fetch_workouts(api_key, page_size=10)
    print(
        f"{len(templates)} templates · {len(routines_raw)} routines · "
        f"{len(workouts)} recent workouts · {total} lifetime",
    )

    cases: list[tuple[str, list]] = []

    # Feed embeds, oldest-first so the numbering counts up the way the poll
    # assigns it: the newest workout takes the lifetime total.
    feed = []
    for offset, workout in enumerate(reversed(workouts)):
        summary = hevy_client.summarize_workout(workout)
        number = None
        if total is not None:
            number = total - (len(workouts) - 1 - offset)
            if number < 1:
                number = None
        feed.append(bot._hevy_workout_embed(
            "you", summary, None, templates, None, routines, number,
        ))
    if feed:
        cases.append(("Workout feed — what auto-posts after a session", feed))

    # The same newest workout renumbered onto a milestone, because a real
    # account only crosses one of those every few months.
    if workouts and total is not None:
        summary = hevy_client.summarize_workout(workouts[0])
        milestone = next(
            n for n in range(max(total, 10), max(total, 10) + 400)
            if bot._hevy_milestone(n)
        )
        cases.append((
            f"The same workout, renumbered #{milestone} — the milestone line",
            [bot._hevy_workout_embed(
                "you", summary, None, templates, None, routines, milestone,
            )],
        ))

    detail = []
    for raw in routines_raw:
        summary = hevy_client.summarize_routine(raw)
        info = routines.get(summary["id"]) or {}
        detail.append(bot._hevy_routine_embed(summary, info.get("folder")))
    if detail:
        cases.append(("/hevy routine — rest timers and notes in full", detail))
    return cases


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "api_key", nargs="?", default=os.environ.get("HEVY_API_KEY"),
        help="Hevy API key (Hevy app -> Settings -> API), or $HEVY_API_KEY",
    )
    ap.add_argument("--open", action="store_true", help="open in a browser")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    if not args.api_key:
        ap.error("no API key: pass one as an argument or set HEVY_API_KEY")

    page = build_page(_cases(args.api_key))
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(page, encoding="utf-8")
    print(f"wrote {dest}")
    if args.open:
        webbrowser.open(dest.resolve().as_uri())


if __name__ == "__main__":
    main()
