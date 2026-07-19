"""Pure helpers for voice-session aggregation.

Kept free of Discord and DB imports so it can be unit-tested in isolation,
exactly like :mod:`app.presence`. The DB layer hands us an ordered list of
``(event, iso_timestamp)`` tuples for one member; we turn that into how long
they spent *in a voice call*, *muted*, and *deafened* over a window, plus the
length of any streak still running "now".

The event log is one interleaved stream carrying three independent signals:

* **channel presence** — ``join`` / ``move`` / ``leave``. A ``join`` opens an
  in-call interval, ``leave`` closes it, ``move`` keeps it open across a
  channel change (so switching rooms mid-call is *not* an artificial break).
* **mute** — ``mute_on`` / ``mute_off``.
* **deafen** — ``deaf_on`` / ``deaf_off``.

We reconstruct three boolean timelines from those transitions. Two design
rules keep the reconstruction honest against a messy real-world log:

1. **``join`` resets mute/deafen to off.** Discord doesn't reliably emit a
   ``mute_off`` when you leave a call, so a stale ``mute_on`` could otherwise
   bleed across sessions. A fresh ``join`` re-anchors the derived state; the
   voice handler re-logs ``mute_on`` / ``deaf_on`` immediately after a ``join``
   when the member joined already muted/deafened, so an already-muted join
   still starts the mute clock at the join instant.
2. **Muted/deafened time only accrues while in a call.** ``muted_seconds`` is
   the overlap of "muted" with "in call", never raw mute time. That makes a
   ``leave`` a hard boundary for every signal without needing the handler to
   emit synthetic ``mute_off`` rows on the way out.

Muted vs deafened are recorded and summed *separately* from the raw signals.
Discord's client auto-mutes your mic whenever you deafen, so in practice
``deafened_seconds`` is a subset of ``muted_seconds`` — we keep them apart so
the stats can still say "muted 40m, of which deafened 12m" rather than
collapsing the two.

Open trailing intervals (an ``mute_on`` with no matching ``mute_off`` because
the window ends — or the bot was restarted — while the member is still muted)
are the subtle case. The tail from the last event to ``now`` is credited only
when the caller can *verify* the member is still in that state: pass the live
``in_call`` / ``muted`` / ``deafened`` flags read from Discord and a
reconstructed-but-unverified state is dropped rather than accruing unbounded
phantom time. When a flag is ``None`` (caller can't check, e.g. the web feed)
we fall back to trusting the log's own end state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


# Event strings this module understands. Anything else (a future event kind, a
# legacy row) is ignored rather than raising, so an older build stays readable.
JOIN = "join"
MOVE = "move"
LEAVE = "leave"
MUTE_ON = "mute_on"
MUTE_OFF = "mute_off"
DEAF_ON = "deaf_on"
DEAF_OFF = "deaf_off"

CHANNEL_EVENTS = frozenset({JOIN, MOVE, LEAVE})
MUTE_EVENTS = frozenset({MUTE_ON, MUTE_OFF})
DEAF_EVENTS = frozenset({DEAF_ON, DEAF_OFF})


@dataclass
class VoiceSummary:
    """Result of summarising one member's voice events over a window."""

    in_call_seconds: float = 0.0
    muted_seconds: float = 0.0
    deafened_seconds: float = 0.0
    # Whether the member is in that state at the end of the window (after the
    # live-flag gate below). ``muted_now``/``deafened_now`` imply ``in_call_now``.
    in_call_now: bool = False
    muted_now: bool = False
    deafened_now: bool = False
    # Length of the still-running streak, measured to ``now`` — the true elapsed
    # time even if the streak began before the window opened. 0 when not in the
    # state. Feeds the "currently muted for Xm" line.
    current_in_call_seconds: float = 0.0
    current_muted_seconds: float = 0.0
    current_deafened_seconds: float = 0.0

    @property
    def active_seconds(self) -> float:
        """In-call time while *not* muted — the audible ("mic live") complement.

        Purely derived: active is exactly in-call minus the muted overlap, so it
        needs no timeline of its own. Deafened is a subset of muted, so muted
        already absorbs it and active excludes it automatically. ``max(0, …)``
        guards the sub-microsecond negative that can surface when ``muted`` and
        ``in_call`` are summed from separate float accumulations.
        """
        return max(0.0, self.in_call_seconds - self.muted_seconds)

    def muted_fraction(self) -> float:
        """Muted time as a fraction of in-call time (0.0 when never in call)."""
        return self.muted_seconds / self.in_call_seconds if self.in_call_seconds else 0.0

    def deafened_fraction(self) -> float:
        """Deafened time as a fraction of in-call time (0.0 when never in call)."""
        return (
            self.deafened_seconds / self.in_call_seconds
            if self.in_call_seconds else 0.0
        )


def _parse_iso(value: str) -> datetime:
    """Parse a stored ISO-8601 timestamp; always returns tz-aware UTC."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def summarize_voice(
    events: list[tuple[str, str]],
    window_start: datetime,
    window_end: datetime,
    *,
    now: datetime | None = None,
    live_in_call: bool | None = None,
    live_muted: bool | None = None,
    live_deafened: bool | None = None,
) -> VoiceSummary:
    """Aggregate voice ``events`` into in-call / muted / deafened totals.

    ``events`` is an ordered list of ``(event, iso_timestamp)`` tuples. Events
    strictly before ``window_start`` are treated as carry-in — they set the
    member's state at the window open without contributing time (the same
    convention as :func:`app.presence.summarize_presence`). Events at or after
    ``window_end`` are ignored.

    ``now`` (default ``window_end``) is the instant the still-running streaks
    are measured to. ``live_*`` are the member's verified current state; see the
    module docstring for how they gate the open trailing interval.
    """
    summary = VoiceSummary()
    window_start = _to_utc(window_start)
    window_end = _to_utc(window_end)
    if window_end <= window_start:
        return summary
    now = window_end if now is None else _to_utc(now)

    in_call = muted = deafened = False
    # Timestamp each stretch began (may predate window_start for carry-in), so
    # the current-streak length reflects the true elapsed time. None when off.
    in_call_since: datetime | None = None
    muted_since: datetime | None = None
    deaf_since: datetime | None = None
    seg_start = window_start

    def apply(event: str, ts: datetime) -> None:
        nonlocal in_call, muted, deafened
        nonlocal in_call_since, muted_since, deaf_since
        if event == JOIN:
            # New session: re-anchor everything. An "already muted" join is
            # represented by a mute_on the handler logs right after this.
            if not in_call:
                in_call_since = ts
            in_call = True
            muted = deafened = False
            muted_since = deaf_since = None
        elif event == MOVE:
            # Channel change mid-call: the call (and any mute/deafen) persists.
            if not in_call:
                in_call = True
                in_call_since = ts
        elif event == LEAVE:
            in_call = muted = deafened = False
            in_call_since = muted_since = deaf_since = None
        elif event == MUTE_ON:
            if not muted:
                muted_since = ts
            muted = True
        elif event == MUTE_OFF:
            muted = False
            muted_since = None
        elif event == DEAF_ON:
            if not deafened:
                deaf_since = ts
            deafened = True
        elif event == DEAF_OFF:
            deafened = False
            deaf_since = None
        # Unknown events: ignored on purpose (forward compatibility).

    def accrue(start: datetime, end: datetime) -> None:
        dur = (end - start).total_seconds()
        if dur <= 0 or not in_call:
            return
        summary.in_call_seconds += dur
        if muted:
            summary.muted_seconds += dur
        if deafened:
            summary.deafened_seconds += dur

    for event, ts_str in events:
        ts = _parse_iso(ts_str)
        if ts < window_start:
            apply(event, ts)  # carry-in: state only, no time
            continue
        if ts >= window_end:
            break
        accrue(seg_start, ts)
        apply(event, ts)
        seg_start = ts

    # Trailing interval. Gate each signal on the verified live state: an
    # unterminated _on that Discord can no longer confirm is dropped rather
    # than accruing to now. A None flag means "can't verify" -> trust the log.
    tail_in_call = in_call if live_in_call is None else (in_call and live_in_call)
    tail_muted = muted if live_muted is None else (muted and live_muted)
    tail_deaf = deafened if live_deafened is None else (deafened and live_deafened)

    # Reflect the gate back onto the accrued tail so a dropped signal doesn't
    # pick up window_end - seg_start of phantom time.
    if tail_in_call:
        summary.in_call_seconds += (window_end - seg_start).total_seconds()
        if tail_muted:
            summary.muted_seconds += (window_end - seg_start).total_seconds()
        if tail_deaf:
            summary.deafened_seconds += (window_end - seg_start).total_seconds()

    summary.in_call_now = tail_in_call
    summary.muted_now = tail_in_call and tail_muted
    summary.deafened_now = tail_in_call and tail_deaf

    if summary.in_call_now and in_call_since is not None:
        summary.current_in_call_seconds = max(0.0, (now - in_call_since).total_seconds())
    if summary.muted_now and muted_since is not None:
        summary.current_muted_seconds = max(0.0, (now - muted_since).total_seconds())
    if summary.deafened_now and deaf_since is not None:
        summary.current_deafened_seconds = max(0.0, (now - deaf_since).total_seconds())

    return summary
