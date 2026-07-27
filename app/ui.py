"""Shared Discord presentation layer: palette, glyphs, embed builders.

``app/bot.py`` grew ~150 commands and 425 send sites, of which only 31 ever
built an embed, so the same ideas got re-typed in slightly different words each
time — three spellings of "Admins only", two progress bars with opposite
meanings, twenty-two copies of the Revo gate. Everything here exists so a call
site states *what it means* and this module decides how that looks.

Kept pure and (almost) Discord-free — the only import is ``discord`` itself for
``Embed``/``Colour`` — so every rule below is unit-testable without a gateway
connection, a token, or a database. That mirrors ``app/calories.py`` and
``app/dayspans.py``.

Two rules earn their own note because they are the bugs this module was written
to kill:

* **A bar's meaning depends on which side of the target you want to be on.**
  Calories are a *target* (reaching it is the win); protein here is a *ceiling*
  (staying under is the win). They previously shared one renderer and one
  colour, so a full bar meant "well done" and "stop" depending on a caller the
  reader could not see. :func:`meter` takes ``ceiling=`` and scores accordingly.
* **Overshoot must stay visible.** The old bar clamped at 100%, so 10 over and
  2 400 over drew identically. :func:`bar` renders overflow cells past the end.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Callable, Iterable, Sequence

import discord

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

#: The bot's accent. Unchanged from the historical ``EMBED_COLOUR``.
BRAND = discord.Colour(0xF26522)
#: The thing you wanted happened: a PR, an upward trend, on target, linked.
SUCCESS = discord.Colour(0x57F287)
#: Rare achievement. Outranks SUCCESS when both apply.
GOLD = discord.Colour(0xFEE75C)
#: Caution / partial / soft over-target. Nothing broke.
WARNING = discord.Colour(0xE67E22)
#: Hard failure, destruction, or a breached ceiling.
DANGER = discord.Colour(0xED4245)
#: Nothing here / not for you / switched off. Never used for data.
NEUTRAL = discord.Colour(0x99AAB5)
#: Model-authored prose, so the rail itself says "a machine wrote this".
INK = discord.Colour(0x4E4E52)

#: Third-party brands. An integration card keeps its brand colour in *every*
#: state including failure — a red "Strava unreachable" card reads as a bot
#: error, an orange one reads as Strava. Severity goes in the icon and title.
STRAVA = discord.Colour(0xFC4C02)
#: Hevy's own near-black (#1D2330) sits at ~1.1:1 against Discord dark's
#: #313338, i.e. an invisible rail on the bot's highest-traffic embed. This is
#: their brand blue instead, which survives both themes.
HEVY = discord.Colour(0x3F8CFF)

# ---------------------------------------------------------------------------
# Discord's hard limits. Exceeding any of these is a 400, not a truncation.
# ---------------------------------------------------------------------------

TITLE_LIMIT = 256
DESC_LIMIT = 4096
FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024
FOOTER_LIMIT = 2048
AUTHOR_LIMIT = 256
FIELD_COUNT_LIMIT = 25
EMBED_TOTAL_LIMIT = 6000
MESSAGE_LIMIT = 2000

#: Zero-width space — Discord rejects an empty field name but renders this as
#: nothing, which is how a column gets a blank heading.
BLANK = "​"

# ---------------------------------------------------------------------------
# Glyphs
# ---------------------------------------------------------------------------

# Status. Leading character of a title or line, never decoration.
OK = "✅"
CAUTION = "⚠️"
FAIL = "❌"
LOCKED = "🔒"
EMPTY = "📭"
AI = "🤖"

# Direction — one vocabulary, three values. delta() owns these.
UP = "🟢"
DOWN = "🔴"
FLAT = "⚪"

# Rank and celebration.
MEDALS = ("🥇", "🥈", "🥉")
PARTY = "🎉"

# Domain — the only icon each subject may carry.
LIFT = "🏋️"
CHART = "📊"
GOAL = "🎯"
SCALE = "⚖️"
FOOD = "🍎"
PROTEIN = "🥩"
STREAK = "🔥"
TROPHY = "🏆"
TICKET = "🎟️"

# Action.
UNDO = "↩️"
EDIT = "✏️"
DELETE = "🗑️"
FILE = "📄"

# Bar glyphs. Fixed cell *count*; every row uses the same glyphs so columns
# hold even though a block glyph is not exactly one ASCII cell wide.
# Exactly two, and they must stay in one glyph family: mixing in a full-height
# block (█) to mark overshoot put a tower of white above the bar line and made
# rows different lengths. Overshoot is a number in its own column — see over_by.
FILL, TRACK = "▰", "▱"
SPARK = "▁▂▃▄▅▆▇█"


# ---------------------------------------------------------------------------
# Scorers — the only way a colour is chosen from data
# ---------------------------------------------------------------------------

def score_target(
    value: float, target: float, *, band: float = 0.10,
) -> discord.Colour:
    """Colour for a metric you are trying to **reach** (calories, goals).

    Under 70% is still climbing (``WARNING``); inside 70%..(100+band)% is the
    win (``SUCCESS``); past that is overshoot (``WARNING``). An absent target
    can't be scored, so it stays ``BRAND``.
    """
    if target <= 0:
        return BRAND
    frac = value / target
    if frac < 0.70:
        return WARNING
    if frac <= 1.0 + band:
        return SUCCESS
    return WARNING


def score_ceiling(
    value: float, ceiling: float, *, caution: float = 0.85,
) -> discord.Colour:
    """Colour for a metric you are trying to **stay under** (protein max).

    The mirror of :func:`score_target`: comfortably under is the win, and
    breaching is a real failure rather than an overshoot, so it reaches
    ``DANGER`` where ``score_target`` tops out at ``WARNING``.
    """
    if ceiling <= 0:
        return BRAND
    frac = value / ceiling
    if frac < caution:
        return SUCCESS
    if frac <= 1.0:
        return WARNING
    return DANGER


def score_trend(delta_value: float, *, good: str = "up") -> discord.Colour:
    """Colour a change by sign. ``good="down"`` inverts (a cut, a time)."""
    if not delta_value:
        return NEUTRAL
    favourable = delta_value > 0 if good == "up" else delta_value < 0
    return SUCCESS if favourable else DANGER


def score_age(days: int, *, warn: int = 60, bad: int = 120) -> discord.Colour:
    """Colour by staleness — for /stale, coverage, and decaying streaks."""
    if days >= bad:
        return DANGER
    if days >= warn:
        return WARNING
    return NEUTRAL


def score_streak(days: int) -> discord.Colour:
    """Colour a streak by how hard it would hurt to lose."""
    if days <= 0:
        return NEUTRAL
    if days < 3:
        return BRAND
    if days < 7:
        return WARNING
    if days < 30:
        return DANGER
    return GOLD


# ---------------------------------------------------------------------------
# Numbers and text
# ---------------------------------------------------------------------------

def num(value: float, unit: str = "", *, decimals: int = 0) -> str:
    """Comma-separated, rounded. Stops ``{:g}`` printing 1712.7659574468084."""
    body = f"{value:,.{decimals}f}"
    if decimals:
        body = body.rstrip("0").rstrip(".")
    return f"{body} {unit}".strip()


def kg(value: float, *, bw: bool = False) -> str:
    """``82.5kg`` / ``1,250kg`` / ``BW+20kg``. One decimal, trailing zero cut."""
    body = f"{value:,.1f}".rstrip("0").rstrip(".")
    return f"BW+{body}kg" if bw else f"{body}kg"


def pct(value: float, total: float, *, decimals: int = 0) -> str:
    """Percentage of *total*. Unscorable totals render as an em dash.

    Separated, because a mis-typed entry can put this in four figures and
    ``1433%`` is harder to read at a glance than ``1,433%``.
    """
    if total <= 0:
        return "—"
    return f"{value / total * 100:,.{decimals}f}%"


def delta(
    value: float, unit: str = "kg", *, good: str = "up", decimals: int = 1,
) -> str:
    """``🟢 +2.5kg`` / ``🔴 -2.5kg`` / ``⚪ no change``.

    ``good="down"`` flips the *colour* (losing bodyweight is favourable) but
    never the sign — the number always reads as the arithmetic did.
    """
    if not value:
        return f"{FLAT} no change"
    body = f"{abs(value):,.{decimals}f}".rstrip("0").rstrip(".")
    sign = "+" if value > 0 else "-"
    favourable = value > 0 if good == "up" else value < 0
    return f"{UP if favourable else DOWN} {sign}{body}{unit}"


def arrow(before: str, after: str) -> str:
    """``80kg → **85kg**`` — the after side carries the emphasis."""
    return f"{before} → **{after}**"


def subtext(text: str) -> str:
    """Discord's small grey tier. Second-tier detail only, never data."""
    return f"-# {text}"


def quote(text: str) -> str:
    """Blockquote. Exactly one use: prose a model wrote."""
    return "\n".join(f"> {line}" for line in text.splitlines() or [""])


def chips(items: Iterable[str]) -> str:
    """``` `a` `b` `c` ``` — for alias lists and other literal vocabularies."""
    return " ".join(f"`{i}`" for i in items)


def rank(index: int, *, mono: bool = False) -> str:
    """Podium marker for a 0-based index, always two display cells.

    ``mono=True`` is mandatory inside :func:`table`: a medal is not one
    monospace cell, so mixing medals and numbers in a fenced block is what
    makes a leaderboard's name column jump at the podium boundary.
    """
    if mono:
        return f"{index + 1:>2}."
    if index < len(MEDALS):
        return MEDALS[index]
    return f"`{index + 1:>2}`"


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    return f"{count:,} {singular if count == 1 else (plural_form or singular + 's')}"


# ---------------------------------------------------------------------------
# Graphics
# ---------------------------------------------------------------------------

def bar(value: float, total: float, *, width: int = 12) -> str:
    """Progress bar of exactly ``width`` cells, always.

    Fixed width is the whole point: these stack in fenced blocks, and a bar
    that grows when a value goes over target makes every column to its right
    ragged. Overshoot is carried by :func:`over_by` and by the percentage in
    :func:`meter`, not by extra cells.

    An earlier version appended ``█`` cells past the end. Two glyph families
    don't mix — ``█`` is full cell height while ``▰``/``▱`` are short and
    centred — so the overflow rendered as a tower of white blocks sitting
    above the bar line. Don't reintroduce it.
    """
    if total <= 0:
        return TRACK * width
    frac = max(0.0, min(1.0, value / total))
    filled = min(width, int(round(frac * width)))
    return FILL * filled + TRACK * (width - filled)


def over_by(value: float, limit: float, fmt: Callable[[float], str] | None = None) -> str:
    """``+34`` when a ceiling is breached, else ``""``.

    The aligned, in-column way to say *how far* over — a bar clamped at full
    can't, and a bare ``!`` says only that it happened.
    """
    if limit <= 0 or value <= limit:
        return ""
    excess = value - limit
    return f"+{fmt(excess)}" if fmt else f"+{excess:,.0f}"


def gauge(score: float, *, width: int = 20) -> str:
    """A 0..100 score as a bar — /overview's consistency number."""
    return bar(score, 100, width=width)


def diverging(
    rows: "Sequence[tuple[str, float | None, float]]",
    fmt: Callable[[float], str],
    *,
    width: int = 8,
    under: str = "under",
    over: str = "over",
    total: str = "total",
) -> str:
    """A fenced chart of *deviation from target*, growing both ways from a rule.

    ``rows`` is ``(label, value, target)`` per row; a ``value`` of None means
    nothing was logged that day.

    A plain progress bar is the wrong form for a week of days scored against a
    target: once most days are over, every bar pins full and the chart goes
    flat exactly when it should be loudest. Here the bar length is |value -
    target|, so the outlier day is the longest bar and an on-target day is
    almost nothing.

    Bars are scaled to the **largest deviation in this set**, which makes the
    shape maximally readable within one card but not comparable between cards —
    hence the literal totals in the last column.
    """
    body = [r for r in rows]
    span = max(
        (abs(v - t) for _l, v, t in body if v is not None and t > 0),
        default=0.0,
    )
    label_w = max((len(l) for l, _v, _t in body), default=0)
    cells = [fmt(v) for _l, v, _t in body if v is not None]
    total_w = max((len(c) for c in cells), default=len(total))

    # Clamped to the bar width: a header word longer than the bars would push
    # the rule off the column the rows put it in, and the whole point of this
    # chart is that the two sides meet on one vertical line.
    head = (
        f"{'':{label_w}}  {under[:width]:>{width}}│{over[:width]:<{width}}"
        f"  {total:>{total_w}}"
    )
    out = [head]
    for label, value, target in body:
        if value is None:
            out.append(
                f"{label:<{label_w}}  {'':{width}}│{'':{width}}  {'—':>{total_w}}"
            )
            continue
        delta = value - target if target > 0 else 0.0
        # Any non-zero deviation gets at least one cell, so a day that is over
        # by a little never renders as if it were exactly on target.
        n = 0 if not delta or span <= 0 else max(1, round(abs(delta) / span * width))
        left = (FILL * n).rjust(width) if delta < 0 else " " * width
        right = (FILL * n).ljust(width) if delta > 0 else " " * width
        out.append(
            f"{label:<{label_w}}  {left}│{right}  {fmt(value):>{total_w}}"
        )
    return "```\n" + "\n".join(out) + "\n```"


def sparkline(values: Sequence[float], *, width: int | None = None) -> str:
    """Eight-level spark. Downsamples by mean so long series still fit."""
    vals = [float(v) for v in values]
    if not vals:
        return ""
    if width and len(vals) > width:
        size = len(vals) / width
        vals = [
            sum(vals[int(i * size):int((i + 1) * size)] or [0.0])
            / max(1, len(vals[int(i * size):int((i + 1) * size)]))
            for i in range(width)
        ]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return SPARK[len(SPARK) // 2] * len(vals)
    step = (hi - lo) / (len(SPARK) - 1)
    return "".join(SPARK[int(round((v - lo) / step))] for v in vals)


def strip(flags: Sequence[bool], *, width: int = 14) -> str:
    """One cell per day, most recent last — an attendance pattern at a glance."""
    tail = list(flags)[-width:]
    return "".join(FILL if f else TRACK for f in tail)


def meter(
    value: float,
    target: float,
    fmt: Callable[[float], str],
    *,
    ceiling: bool = False,
    width: int = 14,
    label: str = "left today",
    note: str | None = None,
) -> tuple[str, discord.Colour]:
    """The shared status line for "X so far against a daily Y".

    Returns ``(text, colour)`` — two tiers, the headline percentage and bar
    above the raw numbers::

        **68%**  ▰▰▰▰▰▰▰▰▰▱▱▱▱▱
        -# 1,700 cal / 2,500 cal · **800 cal** left today

    ``ceiling=True`` switches from "reaching a target is the win" to "staying
    under a max is the win": the wording becomes headroom/over, the scorer
    becomes :func:`score_ceiling`, and overshoot cells are drawn. This is the
    single renderer for both calories and protein, so the two can no longer
    drift apart in wording, in warning glyph, or in what a full bar means.

    A target of zero (not set / cleared) is unscorable, so the percentage and
    the remainder are both dropped rather than dividing by zero.
    """
    scorer = score_ceiling if ceiling else score_target
    colour = scorer(value, target)
    glyphs = bar(value, target, width=width)

    if target <= 0:
        return f"**{fmt(value)}** logged  `{glyphs}`", colour

    remaining = target - value
    if remaining >= 0:
        tail = f"**{fmt(remaining)}** {'headroom' if ceiling else label}"
    else:
        over = f"**{fmt(-remaining)}** over"
        tail = f"{CAUTION} {over} your max" if ceiling else f"{CAUTION} {over} target"

    head = f"**{pct(value, target)}**  `{glyphs}`"
    detail = f"{fmt(value)} / {fmt(target)} · {tail}"
    if note:
        detail += f" {note}"
    return f"{head}\n{subtext(detail)}", colour


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

def table(
    rows: Sequence[Sequence[str]],
    *,
    align: str = "",
    headers: Sequence[str] | None = None,
    max_rows: int = 15,
    cap: int = 20,
) -> str:
    """A fenced monospace block with real columns.

    Two adjacent inline-code spans do *not* line up — Discord puts a
    proportional gap between them — so anything that must align vertically has
    to live inside one fence. Rows are dropped whole (never sliced mid-cell)
    and the remainder is reported inside the fence.

    ``align`` is one character per column: ``>`` right, anything else left.
    """
    body = [[str(c) for c in r] for r in rows]
    shown, dropped = body[:max_rows], max(0, len(body) - max_rows)
    grid = ([list(headers)] if headers else []) + shown
    if not grid:
        return ""
    cols = max(len(r) for r in grid)
    widths = [
        min(cap, max((len(r[i]) for r in grid if i < len(r)), default=0))
        for i in range(cols)
    ]
    out = []
    for r in grid:
        cells = []
        for i in range(cols):
            cell = (r[i] if i < len(r) else "")[:widths[i]]
            right = i < len(align) and align[i] == ">"
            cells.append(cell.rjust(widths[i]) if right else cell.ljust(widths[i]))
        out.append("  ".join(cells).rstrip())
    if headers:
        out.insert(1, "  ".join("─" * w for w in widths).rstrip())
    if dropped:
        out.append(f"… {dropped} more")
    return "```\n" + "\n".join(out) + "\n```"


# ---------------------------------------------------------------------------
# Time. Discord renders <t:…> in the *reader's* timezone, which beats baking
# one server's local date into the text.
# ---------------------------------------------------------------------------

def ts(value: str | datetime | date | int | None) -> int | None:
    """Unix seconds from whatever the DB handed us. Naive ISO is treated as
    UTC, matching how every ``logged_at`` in this schema is written."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    if isinstance(value, date):
        return int(
            datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
            .timestamp()
        )
    return None


def when(value, style: str = "R") -> str:
    """``<t:…:R>`` — "3 days ago" in the reader's own locale and timezone."""
    stamp = ts(value)
    return f"<t:{stamp}:{style}>" if stamp is not None else "—"


def day(value) -> str:
    return when(value, "D")


def clock(value) -> str:
    return when(value, "t")


# ---------------------------------------------------------------------------
# Embed construction
# ---------------------------------------------------------------------------

def fit(text: str, limit: int, *, marker: str = "…") -> str:
    """Trim to *limit*, preferring a line break so nothing is cut mid-token."""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    keep = limit - len(marker) - 1
    cut = text[:keep]
    nl = cut.rfind("\n")
    if nl > keep // 2:
        cut = cut[:nl]
    return cut.rstrip() + f"\n{marker}" if "\n" in cut else cut.rstrip() + marker


def card(
    title: str | None = None,
    *,
    description: str | None = None,
    colour: discord.Colour = BRAND,
    member: object | None = None,
    subject: object | None = None,
    url: str | None = None,
    footer: str | None = None,
    timestamp: bool | datetime = False,
) -> discord.Embed:
    """Build an embed in the house style.

    ``member`` puts a person in the author row *with their avatar*; ``subject``
    puts a different entity's avatar in the thumbnail. Passing the same person
    as both would show one face twice, so callers pick one.

    Titles, field names and footers are plain text in Discord — markdown there
    renders as literal asterisks — so nothing here is bolded for you.
    """
    embed = discord.Embed(colour=colour)
    if title:
        embed.title = fit(title, TITLE_LIMIT)
    if url:
        embed.url = url
    if description:
        # Clipping each part to its own cap isn't enough: a maximal title,
        # description and footer sum to 6 400 against a 6 000 whole-embed cap.
        # The description is the elastic one, so it absorbs the shortfall — and
        # it is budgeted before fields exist, leaving those their own headroom.
        spare = EMBED_TOTAL_LIMIT - len(embed.title or "") - len(footer or "")
        embed.description = fit(description, max(0, min(DESC_LIMIT, spare)))
    if member is not None:
        embed.set_author(
            name=fit(_name_of(member), AUTHOR_LIMIT), icon_url=_avatar_of(member),
        )
    if subject is not None:
        icon = _avatar_of(subject)
        if icon:
            embed.set_thumbnail(url=icon)
    if footer:
        embed.set_footer(text=fit(footer, FOOTER_LIMIT))
    if timestamp is True:
        embed.timestamp = datetime.now(timezone.utc)
    elif isinstance(timestamp, datetime):
        embed.timestamp = timestamp
    return embed


def _name_of(user: object) -> str:
    return str(
        getattr(user, "display_name", None)
        or getattr(user, "global_name", None)
        or getattr(user, "name", "Unknown")
    )


def _avatar_of(user: object) -> str | None:
    asset = getattr(user, "display_avatar", None) or getattr(user, "avatar", None)
    if asset is None:
        return None
    url = getattr(asset, "url", asset)
    return str(url) if url else None


def block(
    embed: discord.Embed, name: str, value: str, *, inline: bool = False,
) -> discord.Embed:
    """Add one field, clipped to the field limit. Empty values are skipped so
    a caller can pass an optional section without guarding every call."""
    if not value:
        return embed
    embed.add_field(
        name=fit(name or BLANK, FIELD_NAME_LIMIT),
        value=fit(value, FIELD_VALUE_LIMIT),
        inline=inline,
    )
    return embed


def tiles(embed: discord.Embed, *pairs: tuple[str, str]) -> discord.Embed:
    """Add inline fields as a row, padded to a full row of three.

    Discord packs three inline fields per row and renders a lone one as a
    narrow column with dead space beside it, which reads as broken — so a
    trailing 1 or 2 get blank spacers.
    """
    real = [(n, v) for n, v in pairs if v]
    for name, value in real:
        block(embed, name, value, inline=True)
    if len(real) % 3:
        for _ in range(3 - len(real) % 3):
            embed.add_field(name=BLANK, value=BLANK, inline=True)
    return embed


def columns(
    embed: discord.Embed, lines: Sequence[str], *, per_column: int = 12,
) -> discord.Embed:
    """Spill a long list into up to three side-by-side columns.

    A 37-item list is ~1.6k characters as one blob — past a field's 1024 cap
    and within reach of the 2000-char message cap. Columns keep each chunk
    small and turn a wall into a directory.
    """
    items = [ln for ln in lines if ln]
    if not items:
        return embed
    n = 1 if len(items) <= per_column else (2 if len(items) <= per_column * 2 else 3)
    size = -(-len(items) // n)
    for i in range(0, len(items), size):
        block(embed, BLANK, "\n".join(items[i:i + size]), inline=n > 1)
    return embed


def total_len(embed: discord.Embed) -> int:
    """Characters Discord counts toward the 6000-per-embed cap."""
    return len(embed)


def overflows(embed: discord.Embed) -> str | None:
    """Describe the first limit an embed breaches, or None if it will send.

    Cheap enough to assert in tests, which is where it belongs: the failure
    mode otherwise is a 400 at send time, in production, with the message lost.
    """
    if embed.title and len(embed.title) > TITLE_LIMIT:
        return f"title {len(embed.title)}>{TITLE_LIMIT}"
    if embed.description and len(embed.description) > DESC_LIMIT:
        return f"description {len(embed.description)}>{DESC_LIMIT}"
    if len(embed.fields) > FIELD_COUNT_LIMIT:
        return f"fields {len(embed.fields)}>{FIELD_COUNT_LIMIT}"
    for f in embed.fields:
        if len(f.name or "") > FIELD_NAME_LIMIT:
            return f"field name {len(f.name)}>{FIELD_NAME_LIMIT}"
        if len(f.value or "") > FIELD_VALUE_LIMIT:
            return f"field {f.name!r} value {len(f.value)}>{FIELD_VALUE_LIMIT}"
    if len(embed) > EMBED_TOTAL_LIMIT:
        return f"total {len(embed)}>{EMBED_TOTAL_LIMIT}"
    return None


# ---------------------------------------------------------------------------
# Null and failure states
# ---------------------------------------------------------------------------

def empty(what: str, *, hint: str | None = None, cmd: str | None = None):
    """"There's nothing here yet" — which is not an error and not your fault.

    Grey rather than red, and it always names the way out: an empty state the
    user can't act on is a dead end.
    """
    return card(f"{EMPTY} {what}", description=hint, colour=NEUTRAL, footer=cmd)


def error(
    title: str,
    detail: str,
    *,
    fix: str | None = None,
    admin: str | None = None,
    diagnostic: str | None = None,
):
    """Something broke. ``fix`` is what the reader can do; ``admin`` is what
    they can't, shown so they know it isn't their fault."""
    embed = card(f"{FAIL} {title}", description=detail, colour=DANGER,
                 footer=diagnostic)
    block(embed, "Try this", fix or "")
    block(embed, "For admins", admin or "")
    return embed


def warn(title: str, detail: str, *, fix: str | None = None):
    embed = card(f"{CAUTION} {title}", description=detail, colour=WARNING)
    block(embed, "Try this", fix or "")
    return embed


def denied(rule: str, *, allowed: str | None = None):
    """A permission gate. Says the rule, then what the reader *can* do."""
    return card(f"{LOCKED} {rule}", description=allowed, colour=NEUTRAL)


def unavailable(feature: str, *, why: str, admin_fix: str | None = None):
    """A switched-off feature. ``why`` is user-facing prose — no env var names;
    the reader can't set those and naming them just leaks configuration."""
    embed = card(f"{LOCKED} {feature} is switched off", description=why,
                 colour=NEUTRAL)
    block(embed, "For admins", admin_fix or "")
    return embed


def ok(text: str) -> str:
    """A one-line success stays plain text — an embed for "Renamed 4 entries"
    is over-serving. Returns ``str``, deliberately, not an Embed."""
    return f"{OK} {text}"


# ---------------------------------------------------------------------------
# Long content
# ---------------------------------------------------------------------------

def chunk(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split plain content on line boundaries so it survives the 2000-char cap.

    Splitting mid-line can leave an unterminated ``**`` or code fence, which
    corrupts everything after it; a paragraph too long to fit alone is the only
    case that gets hard-sliced.
    """
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                out.append(current)
                current = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            out.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        out.append(current)
    return out


# ---------------------------------------------------------------------------
# Preview fixtures — consumed by scripts/preview_ui.py
# ---------------------------------------------------------------------------

class Sections(discord.ui.View):
    """A dropdown that swaps one embed for another in place.

    The reason this exists: an embed message is capped at 6 000 characters
    across *all* its embeds, and a reference card that lists every command in a
    growing bot walks into that ceiling and stays there. Paging by category
    means each view is small no matter how many commands get added, and the
    reader gets a menu instead of a wall.
    """

    def __init__(
        self,
        sections: dict[str, discord.Embed],
        *,
        owner_id: int | None = None,
        placeholder: str = "Pick a category",
        timeout: float | None = 300,
    ) -> None:
        super().__init__(timeout=timeout)
        self.sections = sections
        self.owner_id = owner_id
        self._message: discord.Message | None = None
        self.select = discord.ui.Select(
            placeholder=placeholder,
            options=[
                discord.SelectOption(label=fit(name, 100), value=str(i))
                for i, name in enumerate(sections)
            ][:25],
        )
        self.select.callback = self._on_pick
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id is None or interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "That menu belongs to someone else — run the command yourself.",
            ephemeral=True,
        )
        return False

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        name = list(self.sections)[int(self.select.values[0])]
        for option in self.select.options:
            option.default = option.label == fit(name, 100)
        await interaction.response.edit_message(
            embed=self.sections[name], view=self,
        )

    async def on_timeout(self) -> None:
        """Grey the menu out rather than leaving a control that silently fails."""
        self.select.disabled = True
        self.select.placeholder = "Menu expired — run the command again"
        if self._message is not None:
            try:
                await self._message.edit(view=self)
            except discord.HTTPException:
                pass


def preview_cases() -> list[tuple[str, list]]:
    """Representative renderings for the offline preview page.

    Lives here rather than in the script so the samples are built from the real
    helpers and go stale the moment one of them changes.
    """
    def cal(v: float) -> str:
        return f"{round(v):,} cal"

    def g(v: float) -> str:
        return f"{round(v)} g"

    cases: list[tuple[str, list]] = []

    # --- the shared meter, both polarities -------------------------------
    samples = []
    for title, value, target, ceiling, fmt in (
        ("Calories — climbing", 1180, 2500, False, cal),
        ("Calories — on target", 2380, 2500, False, cal),
        ("Calories — over", 3260, 2500, False, cal),
        ("Protein — comfortable", 96, 180, True, g),
        ("Protein — close to max", 168, 180, True, g),
        ("Protein — over max", 214, 180, True, g),
    ):
        text, colour = meter(value, target, fmt, ceiling=ceiling)
        e = card(f"{PROTEIN if ceiling else FOOD} {title}", colour=colour)
        block(e, "Today", text)
        samples.append(e)
    cases.append(("Shared meter — target vs ceiling", samples))

    # --- the weekly diverging chart --------------------------------------
    week_pro = [("Tue", 56), ("Wed", 93), ("Thu", 112), ("Fri", 193),
                ("Sat", 158), ("Sun", 137), ("Mon", 132)]
    pro_card = card(
        f"{PROTEIN} Protein this week",
        description=diverging([(d, v, 107) for d, v in week_pro], g),
        colour=score_ceiling(126, 107),
        footer=f"{STREAK} 35-day logging streak",
    )
    week_cal = [("Tue", 1763, 1250), ("Wed", 1286, 1250), ("Thu", 1251, 1250),
                ("Fri", 2012, 1250), ("Sat", 3392, 2000), ("Sun", 1845, 2000),
                ("Mon", 1755, 1250)]
    cal_card = card(
        f"{FOOD} Calories this week",
        description=diverging(week_cal, cal),
        colour=score_target(1901, 1464),
        footer=f"{STREAK} 40-day logging streak",
    )
    cases.append((
        "Weekly view — bar length is distance from target, not progress to it",
        [pro_card, cal_card],
    ))

    # --- receipt ---------------------------------------------------------
    text, colour = meter(1850, 2500, cal)
    receipt = card(f"{FOOD} Logged", description=f"**+{cal(650)}** · banh mi",
                   colour=colour, timestamp=True,
                   footer=f"{STREAK} 40-day streak · ❌ to undo")
    block(receipt, "Today", text)
    cases.append(("Calorie receipt (the most-rendered output)", [receipt]))

    # --- leaderboard -----------------------------------------------------
    board = card(f"{TROPHY} incline bench press leaderboard", colour=BRAND,
                 timestamp=True, footer="6 lifters · true load shown where known")
    tiles(board, *[
        (f"{rank(i)} {n}", f"**{kg(w)}**\n{when('2026-07-22T01:40:28+00:00')}")
        for i, (n, w) in enumerate([("Jaidyn", 92.5), ("Poshy", 85), ("musk", 80)])
    ])
    block(board, "The chase", table(
        [[rank(i, mono=True), n, kg(w), bar(w, 92.5, width=8)]
         for i, (n, w) in enumerate(
             [("Cookie Monster", 72.5), ("Dos", 70), ("Dumbaay", 65)], start=3)],
        align="<<>",
    ))
    cases.append(("Leaderboard — podium tiles + monospace chase", [board]))

    # --- null / failure --------------------------------------------------
    cases.append(("Empty, denied, unavailable, error", [
        empty("No lifts logged for Dumbaay yet",
              hint="Post your session in chat — e.g. `bench 80kg` — and I'll store it.",
              cmd="/log adds one manually"),
        denied("Admins only — /purge deletes a lift name for the whole server.",
               allowed="You can remove your own entries with /undo."),
        unavailable("Revo Fitness", why="Nobody has connected a Revo account yet.",
                    admin_fix="Set the Revo credentials in the dashboard."),
        error("Couldn't reach Strava", "Strava didn't answer in time.",
              fix="Try again in a minute.", diagnostic="strava · 504"),
    ]))

    # --- glyph reference -------------------------------------------------
    ref = card("Glyph reference", colour=BRAND)
    block(ref, "Bars", table(
        [
            ["30%", bar(3, 10), ""],
            ["100%", bar(10, 10), ""],
            ["130%", bar(13, 10), over_by(13, 10)],
            ["1,433%", bar(2580, 180), over_by(2580, 180)],
        ],
        align=">",
        headers=["", "bar", "over"],
    ))
    block(ref, "Deltas", " · ".join([delta(2.5), delta(-2.5), delta(0)]))
    block(ref, "Sparkline",
          f"`{sparkline([70.3, 70.9, 71.2, 70.8, 71.6, 71.1, 70.4])}` bodyweight")
    block(ref, "Streak strip",
          f"`{strip([True] * 9 + [False] + [True] * 4)}` 14 days")
    cases.append(("Glyph reference", [ref]))

    return cases
