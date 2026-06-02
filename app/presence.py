"""Pure helpers for presence event aggregation.

Kept free of any Discord or DB imports so they can be unit-tested in
isolation. The DB layer hands us a list of (status, at) tuples; we turn
that into time-online totals and a weekday/hour breakdown for /track
schedule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


# Statuses Discord exposes via on_presence_update. We collapse the three
# "active" flavours into one bucket because for "is this person around?"
# the distinction rarely matters.
ONLINE_STATUSES: frozenset[str] = frozenset({"online", "idle", "dnd"})
OFFLINE_STATUSES: frozenset[str] = frozenset({"offline", "invisible"})


def is_online(status: str) -> bool:
    return status in ONLINE_STATUSES


@dataclass
class PresenceSummary:
    """Result of summarising a presence-event window."""
    online_seconds: float = 0.0
    offline_seconds: float = 0.0
    # weekday: 0=Mon ... 6=Sun
    by_weekday: dict[int, float] = field(default_factory=dict)
    # hour of day in the supplied display timezone, 0..23
    by_hour: dict[int, float] = field(default_factory=dict)
    # Last time we saw the user enter an "online" status, if any.
    last_online_at: datetime | None = None
    # Status at the end of the window (after applying all events).
    final_status: str | None = None
    transitions: int = 0


def _parse_iso(value: str) -> datetime:
    """Parse our stored ISO-8601 timestamps; always returns a tz-aware UTC dt."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def summarize_presence(
    events: list[tuple[str, str]],
    window_start: datetime,
    window_end: datetime,
    *,
    display_tz=timezone.utc,
) -> PresenceSummary:
    """Aggregate ``events`` into time-online totals across a window.

    ``events`` is an ordered list of ``(status, iso_timestamp)`` tuples.
    The first event whose timestamp is strictly before ``window_start`` is
    treated as the user's status at the start of the window (i.e. the
    "carry-in" state). Events at or after ``window_end`` are ignored.

    The breakdown buckets attribute each second of online time to the
    weekday/hour it falls in, computed in ``display_tz`` so the heatmap
    matches the user's local sense of time.
    """
    summary = PresenceSummary()
    if window_end <= window_start:
        return summary

    # Normalize window bounds to UTC for arithmetic.
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=timezone.utc)
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=timezone.utc)
    window_start = window_start.astimezone(timezone.utc)
    window_end = window_end.astimezone(timezone.utc)

    # Build a list of (timestamp, status) segments clamped to the window.
    # We track the "active" status as we walk events, then emit a segment
    # from the previous boundary to the next event boundary.
    current_status: str | None = None
    current_start = window_start

    for status, ts_str in events:
        ts = _parse_iso(ts_str)
        if ts < window_start:
            # Carry-in: just update the starting status.
            current_status = status
            continue
        if ts >= window_end:
            break
        # Close out the previous segment (current_start -> ts) at
        # current_status before applying this transition.
        if current_status is not None and ts > current_start:
            _apply_segment(
                summary, current_status, current_start, ts, display_tz,
            )
        # Count this as a transition only if the status actually changed.
        if current_status != status:
            summary.transitions += 1
        if is_online(status) and not is_online(current_status or "offline"):
            summary.last_online_at = ts
        current_status = status
        current_start = ts

    # Final tail segment to the end of the window.
    if current_status is not None and window_end > current_start:
        _apply_segment(
            summary, current_status, current_start, window_end, display_tz,
        )

    summary.final_status = current_status
    if (
        summary.last_online_at is None
        and current_status is not None
        and is_online(current_status)
    ):
        # User was already online when the window opened — record that as
        # the "last seen online" anchor.
        summary.last_online_at = current_start
    return summary


def _apply_segment(
    summary: PresenceSummary,
    status: str,
    start: datetime,
    end: datetime,
    display_tz,
) -> None:
    """Add a single contiguous (start, end, status) segment to ``summary``."""
    duration = (end - start).total_seconds()
    if duration <= 0:
        return
    if is_online(status):
        summary.online_seconds += duration
        _bucket_segment(summary, start, end, display_tz)
    else:
        summary.offline_seconds += duration


def _bucket_segment(
    summary: PresenceSummary,
    start: datetime,
    end: datetime,
    display_tz,
) -> None:
    """Distribute an online segment across weekday/hour buckets in display_tz.

    Walks the segment one hour at a time, splitting at hour boundaries so
    each bucket gets the exact number of seconds it owns (no double
    counting at midnight or the daylight-saving boundary).
    """
    cursor = start.astimezone(display_tz)
    end_local = end.astimezone(display_tz)
    while cursor < end_local:
        next_hour = _next_hour_boundary(cursor)
        slice_end = min(next_hour, end_local)
        seconds = (slice_end - cursor).total_seconds()
        if seconds <= 0:  # pragma: no cover - DST guard
            break
        wd = cursor.weekday()
        hr = cursor.hour
        summary.by_weekday[wd] = summary.by_weekday.get(wd, 0.0) + seconds
        summary.by_hour[hr] = summary.by_hour.get(hr, 0.0) + seconds
        cursor = slice_end


def _next_hour_boundary(dt: datetime) -> datetime:
    """Smallest datetime > ``dt`` at minute=0/second=0 in the same tz."""
    from datetime import timedelta
    base = dt.replace(minute=0, second=0, microsecond=0)
    # Always step forward one hour: if dt is mid-hour we land on the next
    # boundary; if dt is exactly on the hour we move to the following one.
    utc = base.astimezone(timezone.utc) + timedelta(hours=1)
    return utc.astimezone(dt.tzinfo)


def format_duration(seconds: float) -> str:
    """Render ``seconds`` as a compact ``Xd Yh Zm`` string."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def summarize_activities(
    events: list[tuple[str | None, str]],
    window_start: "datetime",
    window_end: "datetime",
) -> dict[str, float]:
    """Aggregate activity events into ``{activity_name: total_seconds}``.

    ``events`` is an ordered list of ``(activity_name_or_None, iso_timestamp)``
    tuples (same carry-in convention as ``summarize_presence``).  Returns a
    dict mapping each non-None activity name to the total seconds spent in it
    within the window, sorted descending by time.
    """
    if window_end <= window_start:
        return {}

    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=timezone.utc)
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=timezone.utc)
    window_start = window_start.astimezone(timezone.utc)
    window_end = window_end.astimezone(timezone.utc)

    totals: dict[str, float] = {}
    current_activity: str | None = None
    current_start = window_start

    for activity, ts_str in events:
        ts = _parse_iso(ts_str)
        if ts < window_start:
            current_activity = activity
            continue
        if ts >= window_end:
            break
        if current_activity is not None and ts > current_start:
            secs = (ts - current_start).total_seconds()
            totals[current_activity] = totals.get(current_activity, 0.0) + secs
        current_activity = activity
        current_start = ts

    # Tail segment
    if current_activity is not None and window_end > current_start:
        secs = (window_end - current_start).total_seconds()
        totals[current_activity] = totals.get(current_activity, 0.0) + secs

    return dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))


def estimate_sleep_window(
    by_hour: dict[int, float],
    days: int,
    *,
    offline_threshold: float = 0.75,
    min_hours: int = 4,
    max_hours: int = 12,
) -> tuple[int, int] | None:
    """Estimate a sleep window from per-hour online data.

    Returns ``(start_hour, end_hour_inclusive)`` in 0–23 range (both in the
    same timezone as the ``by_hour`` buckets), or ``None`` if the data is
    insufficient or no clear window is found.

    A "sleep" hour is one where the user was offline for at least
    ``offline_threshold`` of its total possible duration.  The function finds
    the longest consecutive run of sleep hours (wrapping around midnight) and
    returns it only if its length is in ``[min_hours, max_hours]``.

    Requires at least 3 days of data for a meaningful estimate.
    """
    if not by_hour or days < 3:
        return None

    max_possible = days * 3600.0
    # For each hour 0-23: is the user mostly offline?
    asleep = [
        (1.0 - min(1.0, by_hour.get(h, 0.0) / max_possible)) >= offline_threshold
        for h in range(24)
    ]

    # Longest consecutive run with circular wrap-around (double the list).
    best_len, best_start = 0, -1
    cur_len, cur_start = 0, -1
    for i, v in enumerate(asleep * 2):
        if v:
            if cur_len == 0:
                cur_start = i
            cur_len += 1
            if cur_len > best_len:
                best_len, best_start = cur_len, cur_start
        else:
            cur_len = 0

    if best_len < min_hours or best_len > max_hours or best_start < 0:
        return None

    start = best_start % 24
    end = (best_start + best_len - 1) % 24
    return (start, end)


def _clamped_segments(
    events: list[tuple[str, str]],
    window_start: datetime,
    window_end: datetime,
) -> list[tuple[bool, datetime, datetime]]:
    """Turn presence events into contiguous (is_online, start, end) segments.

    Uses the same carry-in convention as :func:`summarize_presence`: the last
    event before ``window_start`` sets the opening status. All bounds are
    returned in UTC. Segments are emitted only once an opening status is known.
    """
    segments: list[tuple[bool, datetime, datetime]] = []
    current_status: str | None = None
    current_start = window_start
    for status, ts_str in events:
        ts = _parse_iso(ts_str)
        if ts < window_start:
            current_status = status
            continue
        if ts >= window_end:
            break
        if current_status is not None and ts > current_start:
            segments.append((is_online(current_status), current_start, ts))
        current_status = status
        current_start = ts
    if current_status is not None and window_end > current_start:
        segments.append((is_online(current_status), current_start, window_end))
    return segments


def nightly_sleep_sessions(
    events: list[tuple[str, str]],
    window_start: datetime,
    window_end: datetime,
    *,
    display_tz=timezone.utc,
    min_sleep_hours: float = 3.0,
    flicker_threshold_s: float = 240.0,
) -> list[dict]:
    """Extract per-night sleep sessions from a presence-event timeline.

    A sleep session is a long offline stretch. Brief online flickers shorter
    than ``flicker_threshold_s`` are folded into the surrounding offline time
    (Discord's gateway flaps clients on/offline for a few seconds, which would
    otherwise split one night into several). Offline runs lasting at least
    ``min_sleep_hours`` are returned, attributed to the local calendar date of
    the wake-up.

    Each session is a dict with ISO-8601 UTC ``start``/``end`` plus
    human-readable ``start_local``/``end_local`` (in ``display_tz``) and a
    rounded ``duration_hours``. The list is ordered oldest-first.
    """
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=timezone.utc)
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=timezone.utc)
    window_start = window_start.astimezone(timezone.utc)
    window_end = window_end.astimezone(timezone.utc)
    if window_end <= window_start:
        return []

    segments = _clamped_segments(events, window_start, window_end)

    # Flip brief "online" blips to offline so a momentary reconnect doesn't
    # carve a single night into two short (sub-threshold) sleep blocks.
    adjusted: list[tuple[bool, datetime, datetime]] = []
    for online, start, end in segments:
        if online and (end - start).total_seconds() < flicker_threshold_s:
            online = False
        adjusted.append((online, start, end))

    # Coalesce neighbouring segments that now share a state.
    merged: list[list] = []
    for online, start, end in adjusted:
        if merged and merged[-1][0] == online:
            merged[-1][2] = end
        else:
            merged.append([online, start, end])

    sessions: list[dict] = []
    for online, start, end in merged:
        if online:
            continue
        duration_h = (end - start).total_seconds() / 3600.0
        if duration_h < min_sleep_hours:
            continue
        start_local = start.astimezone(display_tz)
        end_local = end.astimezone(display_tz)
        sessions.append({
            "date": end_local.strftime("%Y-%m-%d"),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "start_local": start_local.strftime("%Y-%m-%d %H:%M"),
            "end_local": end_local.strftime("%Y-%m-%d %H:%M"),
            "duration_hours": round(duration_h, 2),
        })
    return sessions
