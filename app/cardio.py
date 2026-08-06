"""Pure helpers for native cardio programs and adaptive progression.

Discord command handling and persistence live in :mod:`app.bot` and
:mod:`app.db`; keeping the parser/progression rules here makes them usable
without importing the Discord runtime.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace

MIN_MINUTES = 1
MAX_MINUTES = 600
MIN_LEVEL = 0
MAX_LEVEL = 999
MIN_INCLINE = 0
MAX_INCLINE = 90
MIN_SPEED = 0
MAX_SPEED = 100
MAX_SEGMENTS = 20

PACE_THRESHOLDS = {
    "gentle": 5,
    "standard": 3,
    "aggressive": 2,
}

DIFFICULTY_SCORES = {
    "easy": 2,
    "just_right": 1,
    "hard": -2,
}

_NUMBER = r"\d+(?:\.\d+)?"
_DURATION_FIRST_RE = re.compile(
    rf"^\s*(?P<minutes>{_NUMBER})\s*"
    r"(?:minutes?|mins?|m)\s+(?:on\s+)?(?P<body>.+?)\s*$",
    re.IGNORECASE,
)
_ON_SEGMENT_RE = re.compile(
    rf"^\s*(?P<minutes>{_NUMBER})\s+on\s+(?P<body>.+?)\s*$",
    re.IGNORECASE,
)
_REVERSED_SEGMENT_RE = re.compile(
    rf"^\s*(?P<activity>.+?)\s+"
    rf"(?P<minutes>{_NUMBER})\s*(?:minutes?|mins?|m)"
    r"(?P<tail>\s+.*?)?\s*$",
    re.IGNORECASE,
)
_ATTRIBUTE_PATTERNS = {
    "level": (
        re.compile(
            rf"\b(?:at\s+)?(?:lv|lvl|level)\s*:?\s*(?P<value>{_NUMBER})\b",
            re.IGNORECASE,
        ),
    ),
    "incline_degrees": (
        re.compile(
            rf"\b(?:incline)\s*:?\s*(?P<value>{_NUMBER})\b",
            re.IGNORECASE,
        ),
        re.compile(
            rf"\b(?P<value>{_NUMBER})\s*(?:degrees?|deg)\b",
            re.IGNORECASE,
        ),
        re.compile(rf"\b(?P<value>{_NUMBER})\s*°", re.IGNORECASE),
    ),
    "speed": (
        re.compile(
            rf"\b(?:speed)\s*:?\s*(?P<value>{_NUMBER})\b",
            re.IGNORECASE,
        ),
        re.compile(
            rf"\b(?P<value>{_NUMBER})\s*(?:speed)\b",
            re.IGNORECASE,
        ),
    ),
}
_CARDIO_ACTIVITY_RE = re.compile(
    r"\b(?:elliptical|cross\s*trainer|stair\s*master|stairmaster|"
    r"stair\s*climber|stairs?|stepper|treadmill|exercise\s*bike|"
    r"assault\s*bike|air\s*bike|spin\s*bike|bike|bicycle|cycling|cycle|"
    r"rower|rowing|run(?:ning)?|walk(?:ing)?|swim(?:ming)?|"
    r"ski\s*erg|skierg|cardio)\b",
    re.IGNORECASE,
)


class CardioParseError(ValueError):
    """A user-facing cardio program parsing error."""


@dataclass(frozen=True)
class Segment:
    activity: str
    minutes: float
    level: float | None = None
    incline_degrees: float | None = None
    speed: float | None = None


@dataclass(frozen=True)
class Progression:
    segments: list[Segment]
    score: int
    cursor: int
    changed_index: int | None = None
    before: Segment | None = None
    after: Segment | None = None
    direction: str | None = None


def _number(value: str) -> float:
    return float(value)


def _clean_activity(value: str) -> str:
    activity = re.sub(r"\s+", " ", value).strip(" \t,;.-")
    if not activity:
        raise CardioParseError("Each part needs a machine or activity name.")
    if len(activity) > 80:
        raise CardioParseError("Keep each machine or activity name under 80 characters.")
    return activity


def _extract_attributes(
    text: str,
) -> tuple[str, float | None, float | None, float | None]:
    remaining = text
    found: dict[str, float | None] = {
        "level": None,
        "incline_degrees": None,
        "speed": None,
    }
    for key, patterns in _ATTRIBUTE_PATTERNS.items():
        values: list[float] = []
        for pattern in patterns:
            matches = list(pattern.finditer(remaining))
            values.extend(_number(match.group("value")) for match in matches)
            remaining = pattern.sub(" ", remaining)
        if values:
            if any(value != values[0] for value in values[1:]):
                label = "incline" if key == "incline_degrees" else key
                raise CardioParseError(
                    f"Use only one {label} value for each cardio part."
                )
            found[key] = values[0]
    level = found["level"]
    incline = found["incline_degrees"]
    speed = found["speed"]
    if level is not None and not MIN_LEVEL <= level <= MAX_LEVEL:
        raise CardioParseError(
            f"Level must be between {MIN_LEVEL} and {MAX_LEVEL}."
        )
    if incline is not None and not MIN_INCLINE <= incline <= MAX_INCLINE:
        raise CardioParseError(
            f"Incline must be between {MIN_INCLINE} and {MAX_INCLINE} degrees."
        )
    if speed is not None and not MIN_SPEED <= speed <= MAX_SPEED:
        raise CardioParseError(
            f"Speed must be between {MIN_SPEED} and {MAX_SPEED}."
        )
    # A dangling attribute word is almost certainly a typo such as "lv high".
    if re.search(
        r"\b(?:lv|lvl|level|incline)\s+\S+\s*$",
        remaining,
        re.IGNORECASE,
    ):
        raise CardioParseError(
            f'Could not read a setting in "{text.strip()}". '
            "Use a number, e.g. `lv12` or `speed 10`."
        )
    return (
        _clean_activity(remaining),
        level,
        incline,
        speed,
    )


def parse_segment(text: str) -> Segment:
    """Parse one duration/activity pair with an optional machine level.

    Both ``15 mins elliptical level 12`` and ``elliptical 15 mins level 12``
    are accepted, though the duration-first form is preferred in help text.
    """
    match = _DURATION_FIRST_RE.fullmatch(text) or _ON_SEGMENT_RE.fullmatch(text)
    if match is None:
        reversed_match = _REVERSED_SEGMENT_RE.fullmatch(text)
        if reversed_match is None:
            raise CardioParseError(
                f'Could not read "{text.strip()}". Use e.g. '
                "`15 mins elliptical lv12`."
            )
        activity_text = (
            reversed_match.group("activity")
            + (reversed_match.group("tail") or "")
        )
        match = reversed_match
    else:
        activity_text = match.group("body")
    minutes = _number(match.group("minutes"))
    if not MIN_MINUTES <= minutes <= MAX_MINUTES:
        raise CardioParseError(
            f"Duration must be between {MIN_MINUTES} and {MAX_MINUTES} minutes."
        )
    activity, level, incline, speed = _extract_attributes(activity_text)
    return Segment(
        activity=activity,
        minutes=minutes,
        level=level,
        incline_degrees=incline,
        speed=speed,
    )


def parse_program(text: str) -> list[Segment]:
    """Parse a comma, semicolon, or newline separated cardio routine."""
    raw_parts = re.split(r"[,;\n]+", text)
    parts = [part.strip() for part in raw_parts if part.strip()]
    if not parts:
        raise CardioParseError(
            "Add at least one part, e.g. `15 mins elliptical level 12`."
        )
    if len(parts) > MAX_SEGMENTS:
        raise CardioParseError(f"A program can contain at most {MAX_SEGMENTS} parts.")
    return [parse_segment(part) for part in parts]


def parse_chat_message(text: str) -> list[Segment] | None:
    """Strict whole-message parser for passive cardio logging.

    Slash-command programs may use any activity name. Passive chat logging is
    intentionally narrower: every part must name a recognisable cardio
    activity so normal messages such as "15 minutes until dinner" are ignored.
    """
    if not text or len(text) > 2000:
        return None
    try:
        segments = parse_program(text)
    except CardioParseError:
        return None
    if not all(_CARDIO_ACTIVITY_RE.match(segment.activity) for segment in segments):
        return None
    return segments


def format_number(value: float) -> str:
    return f"{value:g}"


def format_segment(segment: Segment) -> str:
    minutes = format_number(segment.minutes)
    unit = "min" if segment.minutes == 1 else "mins"
    level = (
        f" · level {format_number(segment.level)}"
        if segment.level is not None else ""
    )
    incline = (
        f" · incline {format_number(segment.incline_degrees)}°"
        if segment.incline_degrees is not None else ""
    )
    speed = (
        f" · speed {format_number(segment.speed)}"
        if segment.speed is not None else ""
    )
    return f"{minutes} {unit} {segment.activity}{level}{incline}{speed}"


def total_minutes(segments: Iterable[Segment]) -> float:
    return sum(segment.minutes for segment in segments)


def _raise_segment(segment: Segment) -> Segment:
    if segment.level is not None:
        return replace(segment, level=min(MAX_LEVEL, segment.level + 1))
    if segment.speed is not None:
        return replace(segment, speed=min(MAX_SPEED, segment.speed + 0.5))
    if segment.incline_degrees is not None:
        return replace(
            segment,
            incline_degrees=min(MAX_INCLINE, segment.incline_degrees + 1),
        )
    return replace(segment, minutes=min(MAX_MINUTES, segment.minutes + 5))


def _lower_segment(segment: Segment) -> Segment:
    if segment.level is not None:
        return replace(segment, level=max(MIN_LEVEL, segment.level - 1))
    if segment.speed is not None:
        return replace(segment, speed=max(MIN_SPEED, segment.speed - 0.5))
    if segment.incline_degrees is not None:
        return replace(
            segment,
            incline_degrees=max(MIN_INCLINE, segment.incline_degrees - 1),
        )
    return replace(segment, minutes=max(MIN_MINUTES, segment.minutes - 5))


def apply_difficulty(
    segments: list[Segment],
    difficulty: str,
    pace: str,
    score: int = 0,
    cursor: int = 0,
) -> Progression:
    """Update an adaptive program from how the completed session felt.

    Easy/about-right sessions build toward an increase. Hard sessions build
    toward a small deload. Only one segment changes at a time and the cursor
    rotates across the program.
    """
    if not segments:
        raise ValueError("cannot progress an empty cardio program")
    if difficulty not in DIFFICULTY_SCORES:
        raise ValueError(f"unknown cardio difficulty: {difficulty}")
    if pace not in PACE_THRESHOLDS:
        raise ValueError(f"unknown cardio progression pace: {pace}")

    threshold = PACE_THRESHOLDS[pace]
    next_score = max(-threshold, min(threshold, score + DIFFICULTY_SCORES[difficulty]))
    next_cursor = cursor % len(segments)
    changed_index: int | None = None
    direction: str | None = None

    if next_score >= threshold:
        changed_index = next_cursor
        direction = "increase"
        next_cursor = (next_cursor + 1) % len(segments)
    elif next_score <= -threshold:
        # Ease the most recently progressed part. Before the first increase,
        # this naturally points at the first segment.
        changed_index = (next_cursor - 1) % len(segments) if cursor else 0
        direction = "decrease"

    if changed_index is None:
        return Progression(list(segments), next_score, next_cursor)

    before = segments[changed_index]
    after = (
        _raise_segment(before)
        if direction == "increase" else _lower_segment(before)
    )
    # A segment already at its safety bound cannot change. Resetting the score
    # would hide that, so retain it just inside the threshold.
    if after == before:
        bounded_score = threshold - 1 if direction == "increase" else -threshold + 1
        return Progression(list(segments), bounded_score, next_cursor)

    updated = list(segments)
    updated[changed_index] = after
    return Progression(
        updated,
        0,
        next_cursor,
        changed_index=changed_index,
        before=before,
        after=after,
        direction=direction,
    )


def segments_from_rows(rows: Iterable) -> list[Segment]:
    """Convert DB rows/dicts into immutable progression values."""
    return [
        Segment(
            activity=str(row["activity"]),
            minutes=float(row["minutes"]),
            level=float(row["level"]) if row["level"] is not None else None,
            incline_degrees=(
                float(row["incline_degrees"])
                if row["incline_degrees"] is not None else None
            ),
            speed=float(row["speed"]) if row["speed"] is not None else None,
        )
        for row in rows
    ]
