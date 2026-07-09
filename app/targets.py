"""Which nutrition target applies on a given day.

Everyone starts with one calorie and one protein target that stand for all
seven days. On top of that a user can layer a **weekend** override — bigger
Saturdays, same protein — and the resolver picks whichever rule applies to the
date being asked about.

Resolution is a short priority walk: of the rules that match the date, the most
specific one wins, and if it sets no value the next rule down gets a turn. A
rule that sets no value is how an override is *cleared* without deleting it,
which matters because deleting it would silently re-score every weekend the
user had already lived through.

Calories and protein resolve independently — one row per macro — because the
two trackers are independent everywhere else in the bot: separate setup,
separate removal, separate weekly report. Nobody should be able to turn off
calorie tracking and lose their protein ceiling as a side effect.

Targets are effective-dated: a row applies to days on or after its
``effective_from``. Editing a goal writes a *new* row instead of overwriting the
old one, so a week that has already happened keeps resolving against the numbers
that were live at the time. Reports never silently rewrite themselves because
someone bumped their goal on Thursday.

Adding a new kind of rule (a single weekday, a training day, a public holiday, a
block of a seasonal plan) means adding a ``scope`` string and teaching
:func:`scope_matches` and :func:`scope_priority` about it — no schema change and
no new column. Scopes this version doesn't recognise are skipped rather than
raising, so a database written by a newer build stays readable by an older one.

Pure and Discord-free so it can be unit-tested directly; storage lives in
:mod:`app.db` and all this module wants from it is a sequence of rows.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone

try:  # pragma: no cover - stdlib on 3.9+, absent on some slim builds
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

LOG = logging.getLogger(__name__)

# The bot thinks in the community's local day, not UTC — a 23:30 snack belongs
# to the day the user experienced, and Saturday has to start at local midnight
# for a weekend target to mean anything. Sole owner of this setting; app.bot
# imports DISPLAY_TZ from here.
TZ_NAME = os.getenv("DISPLAY_TIMEZONE", "Australia/Adelaide").strip() or "UTC"
DISPLAY_TZ: timezone | object = timezone.utc
if ZoneInfo is not None:
    try:
        DISPLAY_TZ = ZoneInfo(TZ_NAME)
    except Exception:  # noqa: BLE001 - unknown zone name, keep running on UTC
        LOG.warning("Unknown DISPLAY_TIMEZONE=%r, falling back to UTC", TZ_NAME)


def local_today() -> date:
    """Today's date in DISPLAY_TIMEZONE."""
    return datetime.now(DISPLAY_TZ).date()


def local_day_of(dt: datetime | None) -> date:
    """The local calendar day a timestamp falls on (today when None).

    A backdated entry carries the timestamp of the day it was logged *for*, so
    this is what decides which day's target it gets scored against.
    """
    if dt is None:
        return local_today()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DISPLAY_TZ).date()


# ---------------------------------------------------------------------------
# Macros and scopes
# ---------------------------------------------------------------------------

MACRO_KCAL = "kcal"
MACRO_PROTEIN = "protein_g"

# ``default`` is the all-week target: the number a user hands to
# ``/calories setup``. It matches every day and sits at the bottom of the
# priority order, so any more specific rule simply overrides it. A user who
# never sets anything else gets that one target seven days a week — which is
# exactly how the tracker behaved before per-day targets existed.
SCOPE_DEFAULT = "default"
SCOPE_WEEKEND = "weekend"
# Reserved. Mon-Fri is the exact complement of the weekend, so the setup flow
# stores its all-week number as ``default`` rather than writing this. It exists
# for a future rule that wants to say "weekdays only" *without* also becoming
# the fallback for days no other rule covers.
SCOPE_WEEKDAY = "weekday"

# Scopes with a parameter. ``dow:2`` is every Wednesday; ``date:2026-12-25`` is
# one specific day.
_DOW_PREFIX = "dow:"
_DATE_PREFIX = "date:"

# Higher wins. The gaps leave room to slot new rule kinds in without
# renumbering: a training-day tag would sit around 15, a seasonal plan block
# around 5.
_FIXED_PRIORITY = {
    SCOPE_DEFAULT: 0,
    SCOPE_WEEKDAY: 10,
    SCOPE_WEEKEND: 10,
}
_DOW_PRIORITY = 20
_DATE_PRIORITY = 30

# Rows carry a real ``effective_from``, but a user's first targets have to cover
# the entries they logged *before* setting them — chat backfill writes history.
# Filing those at the start of the calendar is simpler than a nullable column
# and keeps the comparison a plain string compare.
BEGINNING_OF_TIME = "0001-01-01"

WEEKEND_LABEL = "Using Weekend Targets"
WEEKDAY_LABEL = "Using Weekday Targets"


def is_weekend(day: date) -> bool:
    """Saturday or Sunday."""
    return day.weekday() >= 5


def band(day: date) -> str:
    """``"weekend"`` or ``"weekday"`` — the bucket analytics average within."""
    return "weekend" if is_weekend(day) else "weekday"


def scope_priority(scope: str) -> int:
    """How specific a scope is; higher overrides lower. ``-1`` means
    unrecognised (a scope from a newer build) and callers skip those rows."""
    if scope in _FIXED_PRIORITY:
        return _FIXED_PRIORITY[scope]
    if scope.startswith(_DOW_PREFIX):
        return _DOW_PRIORITY
    if scope.startswith(_DATE_PREFIX):
        return _DATE_PRIORITY
    return -1


def scope_matches(scope: str, day: date) -> bool:
    """Whether a rule with this scope applies on ``day``."""
    if scope == SCOPE_DEFAULT:
        return True
    if scope == SCOPE_WEEKDAY:
        return not is_weekend(day)
    if scope == SCOPE_WEEKEND:
        return is_weekend(day)
    if scope.startswith(_DOW_PREFIX):
        try:
            weekday = int(scope[len(_DOW_PREFIX):])
        except ValueError:
            return False
        return 0 <= weekday <= 6 and day.weekday() == weekday
    if scope.startswith(_DATE_PREFIX):
        return scope[len(_DATE_PREFIX):] == day.isoformat()
    return False


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MacroTarget:
    """One macro's target on one day.

    ``value`` is None when no live rule sets one — the user either never set
    this tracker up or has since turned it off. ``split`` says whether some
    non-default rule is in force, which is what decides if the UI mentions
    weekday/weekend at all.
    """
    value: float | None = None
    scope: str | None = None
    split: bool = False


@dataclass(frozen=True)
class Resolved:
    """The calorie and protein targets in force on ``day``."""
    day: date
    kcal: MacroTarget = MacroTarget()
    protein: MacroTarget = MacroTarget()

    def macro(self, name: str) -> MacroTarget:
        """Either macro by its :data:`MACRO_KCAL`/:data:`MACRO_PROTEIN` name, so
        analytics can loop over both instead of duplicating themselves."""
        return self.kcal if name == MACRO_KCAL else self.protein

    @property
    def split(self) -> bool:
        return self.kcal.split or self.protein.split

    @property
    def is_weekend(self) -> bool:
        return is_weekend(self.day)

    @property
    def label(self) -> str | None:
        """"Using Weekend Targets" / "Using Weekday Targets", or None.

        Stays None for someone with a single all-week goal — there is no second
        set to be using instead, and saying so would just be noise. Use this
        where both macros are on show; use :meth:`label_for` on a line about one.
        """
        if not self.split:
            return None
        return WEEKEND_LABEL if self.is_weekend else WEEKDAY_LABEL

    def label_for(self, macro: str) -> str | None:
        """The banner for a line about ``macro`` alone.

        None unless *that* macro splits: telling someone their protein is "Using
        Weekend Targets" when the ceiling is 180 g every day, purely because
        their calories differ at weekends, is noise on a protein-only reply.
        """
        if not self.macro(macro).split:
            return None
        return WEEKEND_LABEL if self.is_weekend else WEEKDAY_LABEL


def _newest_per_scope(
    rows: Iterable[Mapping], macro: str, iso_day: str,
) -> dict[str, Mapping]:
    """The version of each scope that was in force for ``macro`` on ``iso_day``.

    A scope accumulates one row per edit; the winner is the latest whose
    ``effective_from`` has already arrived. Rows dated in the future are
    ignored, which is what a scheduled plan change will lean on.
    """
    newest: dict[str, Mapping] = {}
    for row in rows:
        if row["macro"] != macro:
            continue
        scope = row["scope"]
        if scope_priority(scope) < 0:
            continue  # written by a newer build; ignore rather than guess
        if row["effective_from"] > iso_day:
            continue
        current = newest.get(scope)
        key = (row["effective_from"], row["set_at"] or "")
        if current is None or key >= (
            current["effective_from"], current["set_at"] or "",
        ):
            newest[scope] = row
    return newest


def _resolve_macro(
    rows: Iterable[Mapping], macro: str, day: date,
) -> MacroTarget:
    newest = _newest_per_scope(rows, macro, day.isoformat())

    # A weekday/weekend split exists once some non-default rule is live *and*
    # actually sets a number. A cleared override — a row with a NULL value —
    # leaves no split behind, so the UI goes quiet again.
    split = any(
        scope != SCOPE_DEFAULT and row["value"] is not None
        for scope, row in newest.items()
    )

    # Most specific first; a later effective_from breaks a priority tie.
    applicable = sorted(
        (row for scope, row in newest.items() if scope_matches(scope, day)),
        key=lambda r: (scope_priority(r["scope"]), r["effective_from"]),
        reverse=True,
    )
    for row in applicable:
        if row["value"] is not None:
            return MacroTarget(float(row["value"]), row["scope"], split)
    return MacroTarget(None, None, split)


def resolve(rows: Iterable[Mapping], day: date) -> Resolved:
    """Work out the targets in force on ``day``.

    ``rows`` is every target row for one user — a handful, so this stays cheap
    enough for the status line the bot renders on every logged message. Each row
    needs ``macro``, ``scope``, ``value``, ``effective_from`` and ``set_at``.
    """
    rows = list(rows)
    return Resolved(
        day=day,
        kcal=_resolve_macro(rows, MACRO_KCAL, day),
        protein=_resolve_macro(rows, MACRO_PROTEIN, day),
    )


def resolve_days(
    rows: Sequence[Mapping], days: Iterable[date],
) -> dict[date, Resolved]:
    """Resolve a range in one pass — a week grid needs seven answers from the
    same rows, and re-querying per day would be silly."""
    rows = list(rows)
    return {day: resolve(rows, day) for day in days}


@dataclass(frozen=True)
class BandStats:
    """One band's (weekday or weekend) intake against its own targets."""
    days: int
    avg_intake: float
    avg_target: float | None
    #: Mean of ``intake / target`` over the days that had a target. 1.0 means
    #: they landed exactly on it; for protein — a ceiling — over 1.0 is the bad
    #: direction. Averaging the daily ratios rather than dividing the averages
    #: keeps one huge day from dominating.
    adherence: float | None


def band_stats(
    day_intake: Mapping[date, float],
    day_targets: Mapping[date, Resolved],
    macro: str = MACRO_KCAL,
) -> dict[str, BandStats]:
    """Split logged days into weekday/weekend buckets and score each against the
    targets that were actually in force on those days.

    Only days present in ``day_intake`` count — a missing day is a tracking gap,
    not a zero. Bands with no logged days are absent from the result.
    """
    acc: dict[str, dict] = {}
    for day, resolved in day_targets.items():
        if day not in day_intake:
            continue
        bucket = acc.setdefault(
            band(day), {"days": 0, "intake": 0.0, "target": 0.0,
                        "target_days": 0, "ratios": []},
        )
        intake = day_intake[day]
        bucket["days"] += 1
        bucket["intake"] += intake
        target = resolved.macro(macro).value
        if target:
            bucket["target"] += target
            bucket["target_days"] += 1
            bucket["ratios"].append(intake / target)
    return {
        name: BandStats(
            days=b["days"],
            avg_intake=b["intake"] / b["days"],
            avg_target=(
                b["target"] / b["target_days"] if b["target_days"] else None
            ),
            adherence=(
                sum(b["ratios"]) / len(b["ratios"]) if b["ratios"] else None
            ),
        )
        for name, b in acc.items()
    }


def mean_target(
    rows: Sequence[Mapping], days: Iterable[date], macro: str = MACRO_KCAL,
) -> float | None:
    """Average daily target across ``days``, or None if none of them have one.

    A weekday/weekend split makes "your target" ambiguous for anything reasoning
    over a whole week: ``/calories tdee`` projects weight change from
    ``(target - maintenance) * 7``, which only means something against the mean
    of the seven targets actually in force.
    """
    present = [
        r.macro(macro).value for r in resolve_days(rows, days).values()
        if r.macro(macro).value is not None
    ]
    if not present:
        return None
    return sum(present) / len(present)
