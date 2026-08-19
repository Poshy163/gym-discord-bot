"""Single source of truth for every tunable the bot exposes.

Precedence is ALWAYS ``environment > database > code default``, for every key,
with no per-key exceptions. That order is what makes "an existing deployment
upgrades with zero behaviour change" provable by inspection rather than by
trusting a migration: whatever is pinned in ``docker-compose.yml`` keeps
winning forever, so an operator who changes nothing sees nothing change.

The obvious alternative (database wins, so the dashboard is authoritative)
fails silently and confusingly: an operator edits their compose file, restarts,
and nothing happens, with no place the UI would ever think to tell them. Under
env-wins the confusing case is visible exactly where they already are — the
field shows a "Pinned by environment" chip and says which file to edit.

Nothing in this module may raise while loading. A stored value that no longer
coerces logs an error and falls back to its code default, which is what makes a
typo saved through the dashboard unable to brick the next boot. This is a
deliberate, documented departure from the pre-refactor behaviour, where a bad
``MIN_LIFTS_FOR_AUTO`` raised ValueError at import and killed the process.

The ``_coerce`` callables below are exact transcriptions of the parsing they
replace, quirks included — ``.isdigit()`` rejecting negative snowflakes on most
channel IDs but ``.lstrip("-").isdigit()`` accepting them on
``STRAVA_FEED_CHANNEL_ID``, ``max(5, ...)`` flooring ``REVO_POLL_MINUTES``,
``or`` fallbacks that treat an empty string differently from an absent key.
``tests/test_config_equivalence.py`` asserts they still agree with the values
``app.bot`` computes, so a drifting transcription fails CI rather than silently
changing a running deployment.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

LOG = logging.getLogger("gymbot.config")

# The truthy set every boolean setting in the codebase has always used. Note
# there is no .strip() — " true" has always read as false, and preserving that
# matters more than fixing it, because someone's compose file may depend on a
# value being ignored.
_TRUE = ("1", "true", "yes", "y", "on")

Apply = str  # "hot" | "worker" | "supervisor" | "env-only"
Source = str  # "env" | "db" | "default"


# ---------------------------------------------------------------------------
# Coercion primitives — one per distinct parsing shape found in the codebase.
# ---------------------------------------------------------------------------

def _bool(raw: str) -> bool:
    return raw.lower() in _TRUE


def _int(raw: str) -> int:
    return int(raw)


def _float(raw: str) -> float:
    return float(raw)


def _str(raw: str) -> str:
    return raw


def _snowflake(raw: str) -> int | None:
    """``int(x) if x.isdigit() else None`` — the shape used by every channel and
    role ID except STRAVA_FEED_CHANNEL_ID. Rejects negatives, which is why a
    pasted "-1" silently disables a feature rather than erroring."""
    stripped = raw.strip()
    return int(stripped) if stripped.isdigit() else None


def _snowflake_signed(raw: str) -> int | None:
    """STRAVA_FEED_CHANNEL_ID alone accepts a leading '-' (app/bot.py:10797)."""
    stripped = raw.strip()
    return int(stripped) if stripped.lstrip("-").isdigit() else None


def _csv_ids(raw: str) -> set[int]:
    """Per-element ``.strip().isdigit()`` filter: non-numeric entries vanish
    silently. A typo in ADMIN_USER_IDS has always removed that admin rather
    than failing loudly, and callers depend on the set never containing junk."""
    return {int(x) for x in raw.strip().split(",") if x.strip().isdigit()}


def _csv_lower(raw: str) -> set[str]:
    return {s.strip().lower() for s in raw.strip().split(",") if s.strip()}


def _stripped_or(fallback: str) -> Callable[[str], str]:
    """``os.getenv(k, d).strip() or fallback`` — used where a whitespace-only
    value must not produce an unbindable host or an empty Mapbox style."""
    def parse(raw: str) -> str:
        return raw.strip() or fallback
    return parse


def _floor_int(lo: int) -> Callable[[str], int]:
    """``max(lo, int(raw))`` — the politeness floors on the two pollers and
    the ``max(0, ...)`` guard in app/gemini_client.py."""
    def parse(raw: str) -> int:
        return max(lo, int(raw))
    return parse


def _floor_float(lo: float) -> Callable[[str], float]:
    """``max(lo, float(raw))`` — gemini_client.retry_delay_seconds()."""
    def parse(raw: str) -> float:
        return max(lo, float(raw))
    return parse


def _int_or_zero(raw: str) -> int:
    """``int(os.getenv(k, "0") or 0)`` — an explicitly empty value means 0
    rather than raising (app/bot.py:10851)."""
    return int(raw or 0)


def _float_or_zero(raw: str) -> float:
    return float(raw or 0)


def _rstrip_slash(raw: str) -> str:
    return raw.rstrip("/")


def _strip(raw: str) -> str:
    return raw.strip()


@dataclass(frozen=True)
class Setting:
    """One tunable: how to parse it, where it shows up, and what applying costs.

    ``default`` is always the RAW string exactly as it appeared in the
    ``os.getenv`` call being replaced, never a pre-parsed value. Keeping it raw
    means the default travels through the identical coercion path as a stored
    or environment value, so the three sources cannot diverge.
    """

    key: str
    kind: str                      # bool int float str enum id ids csv tz secret path
    default: str
    group: str
    coerce: Callable[[str], Any]
    apply: Apply = "worker"        # conservative: a bounce, never a silent lie
    secret: bool = False
    label: str = ""
    help: str = ""
    choices: tuple[str, ...] = ()
    min: float | None = None
    max: float | None = None
    # Fires only when this key's own resolved raw value is empty, reproducing
    # the fallback chains at app/bot.py:213-215, :242-245, :256-258, :310-312
    # and :318-320. Receives the fully-coerced value map.
    derive: Callable[[dict[str, Any]], Any] | None = None
    restart_note: str = ""
    # True for keys that sibling modules read via os.getenv and that therefore
    # must be exported into the worker's environment verbatim.
    sibling_env: bool = False


def _S(key: str, kind: str, default: str, group: str, coerce: Callable[[str], Any],
       **kw: Any) -> Setting:
    return Setting(key=key, kind=kind, default=default, group=group,
                   coerce=coerce, **kw)


# ---------------------------------------------------------------------------
# The registry. 88 entries: the 87 environment variables the pre-refactor code
# read, plus ENABLE_MEMBER_MIRROR, which is new and explained below.
# ---------------------------------------------------------------------------

_SETTINGS: tuple[Setting, ...] = (

    # ---- Discord ---------------------------------------------------------
    _S("DISCORD_TOKEN", "secret", "", "discord", _str,
       apply="worker", secret=True, label="Bot token",
       help="Discord Developer Portal -> your application -> Bot -> Reset "
            "Token. The bot restarts when this changes."),
    _S("GUILD_ID", "id", "", "discord", _snowflake,
       apply="worker", label="Development guild ID",
       help="Only used when command scope is 'guild'. Leave blank otherwise."),
    _S("COMMAND_SCOPE", "enum", "global", "discord",
       lambda raw: raw.strip().lower(),
       apply="worker", label="Slash-command scope", choices=("global", "guild"),
       help="'global' publishes commands to every server (first publish can "
            "take ~1h to propagate). 'guild' publishes only to the development "
            "guild above, for instant updates while developing.",
       restart_note="Changing this forces a real command sync with Discord. "
                    "Toggling it repeatedly can exhaust the daily sync budget."),
    _S("GYM_CHANNEL_IDS", "ids", "", "discord", _csv_ids,
       apply="hot", label="Auto-scan channels",
       help="Comma-separated channel IDs the bot parses for lifts. Leave empty "
            "to watch every channel it can see."),

    # ---- Admins ----------------------------------------------------------
    _S("ADMIN_USER_IDS", "ids", "", "admin", _csv_ids,
       apply="hot", label="Bot administrators",
       help="Select trusted member profiles allowed to run owner-only commands, "
            "undo any tracked reply, and manage other members' data. Environment "
            "configuration still accepts comma-separated Discord user IDs. "
            "Empty means nobody."),

    # ---- Core parsing ----------------------------------------------------
    _S("MIN_LIFTS_FOR_AUTO", "int", "2", "core", _int,
       apply="hot", min=1, max=100, label="Minimum lifts to auto-store",
       help="A message must parse to at least this many lifts before it is "
            "stored automatically. Keeps casual chatter out of the database."),
    _S("PARSE_REPLY_MAX_ITEMS", "int", "15", "core", _int,
       apply="hot", min=0, max=200, label="Max lifts shown in a reply",
       help="0 shows every parsed lift."),
    _S("MAX_WEIGHT_KG", "float", "500", "core", _float,
       apply="hot", min=0, max=10000, label="Maximum plausible lift (kg)",
       help="Guardrail against a typo like '2200kg' becoming a leaderboard PR. "
            "0 disables the ceiling."),
    _S("SHOW_LB", "bool", "false", "core", _bool,
       apply="hot", label="Show pounds alongside kg"),
    _S("PLATE_KG", "float", "20", "core", _float,
       apply="worker", sibling_env=True, min=0.5, max=100,
       label="Plate weight (kg)",
       help="Assumed weight of one Olympic plate per side, for 'two plates' "
            "style parsing. Read by app/parser.py."),
    # ``.strip() or "UTC"`` -- a whitespace-only value has always meant UTC
    # rather than the Adelaide default (app/targets.py:53).
    _S("DISPLAY_TIMEZONE", "tz", "Australia/Adelaide", "core",
       _stripped_or("UTC"),
       apply="worker", sibling_env=True, label="Display timezone",
       help="Which local day an entry belongs to, and when scheduled posts "
            "fire.",
       restart_note="Changes which calendar day existing entries fall into, "
                    "so totals and streaks may shift."),
    _S("BACKFILL_ON_START", "bool", "true", "core", _bool,
       apply="worker", label="Scan history on startup",
       help="Only runs when at least one auto-scan channel is set."),
    _S("BACKFILL_LIMIT", "int", "1000", "core", _int,
       apply="hot", min=0, max=100000, label="Startup scan depth",
       help="Messages per channel to scan on startup. 0 means no limit."),

    # ---- Weekly reminder -------------------------------------------------
    _S("REMINDER_CHANNEL_ID", "id", "", "reminder", _snowflake,
       apply="worker", label="Reminder channel",
       help="Leave blank to disable the weekly 'drop your bests' nudge.",
       restart_note="Setting this for the first time needs a restart, because "
                    "the schedule only starts when the bot connects."),
    _S("REMINDER_WEEKDAY", "int", "2", "reminder", _int,
       apply="hot", min=0, max=6, label="Reminder day",
       help="Monday=0 ... Sunday=6."),
    _S("REMINDER_HOUR", "int", "12", "reminder", _int,
       apply="worker", min=0, max=23, label="Reminder hour",
       restart_note="Schedule times are fixed when the bot starts."),
    _S("REMINDER_MINUTE", "int", "0", "reminder", _int,
       apply="worker", min=0, max=59, label="Reminder minute",
       restart_note="Schedule times are fixed when the bot starts."),
    _S("REMINDER_ROLE_ID", "id", "", "reminder", _snowflake,
       apply="hot", label="Role to ping",
       help="Leave blank for no ping."),

    # ---- Bodyweight reminder --------------------------------------------
    _S("BODYWEIGHT_REMINDER_CHANNEL_ID", "id", "", "reminder", _snowflake,
       apply="worker", label="Bodyweight reminder channel",
       help="Blank inherits the weekly reminder's channel.",
       derive=lambda v: v["REMINDER_CHANNEL_ID"]),
    _S("BODYWEIGHT_REMINDER_WEEKDAY", "int", "0", "reminder", _int,
       apply="hot", min=0, max=6, label="Bodyweight reminder day"),
    _S("BODYWEIGHT_REMINDER_HOUR", "int", "7", "reminder", _int,
       apply="worker", min=0, max=23, label="Bodyweight reminder hour",
       restart_note="Schedule times are fixed when the bot starts."),
    _S("BODYWEIGHT_REMINDER_MINUTE", "int", "30", "reminder", _int,
       apply="worker", min=0, max=59, label="Bodyweight reminder minute",
       restart_note="Schedule times are fixed when the bot starts."),
    _S("BODYWEIGHT_REMINDER_ROLE_ID", "id", "", "reminder", _snowflake,
       apply="hot", label="Role to ping"),

    # ---- Daily update ----------------------------------------------------
    _S("DAILY_UPDATE_CHANNEL_ID", "id", "", "daily", _snowflake,
       apply="worker", label="Daily update channel",
       help="Leave blank to disable yesterday's activity summary.",
       restart_note="Setting this for the first time needs a restart."),
    _S("DAILY_UPDATE_HOUR", "int", "8", "daily", _int,
       apply="worker", min=0, max=23, label="Daily update hour",
       restart_note="Schedule times are fixed when the bot starts."),
    _S("DAILY_UPDATE_MINUTE", "int", "0", "daily", _int,
       apply="worker", min=0, max=59, label="Daily update minute",
       restart_note="Schedule times are fixed when the bot starts."),
    _S("DAILY_UPDATE_POST_EMPTY", "bool", "false", "daily", _bool,
       apply="hot", label="Post even when there was no activity"),

    # ---- Weekly report ---------------------------------------------------
    _S("WEEKLY_REPORT_CHANNEL_ID", "id", "", "report", _snowflake,
       apply="worker", label="Weekly report channel",
       help="Blank inherits the daily update channel, then the reminder "
            "channel. Blank everywhere disables the report.",
       derive=lambda v: v["DAILY_UPDATE_CHANNEL_ID"] or v["REMINDER_CHANNEL_ID"]),
    _S("WEEKLY_REPORT_WEEKDAY", "int", "6", "report", _int,
       apply="hot", min=0, max=6, label="Weekly report day"),
    _S("WEEKLY_REPORT_HOUR", "int", "18", "report", _int,
       apply="worker", min=0, max=23, label="Weekly report hour",
       restart_note="Schedule times are fixed when the bot starts."),
    _S("WEEKLY_REPORT_MINUTE", "int", "0", "report", _int,
       apply="worker", min=0, max=59, label="Weekly report minute",
       restart_note="Schedule times are fixed when the bot starts."),

    # ---- Backups ---------------------------------------------------------
    # Runs in the supervisor, so these stay live even while the bot is down or
    # quarantined -- which is exactly when you most want backups still running.
    _S("BACKUP_DIR", "path", "", "backup", _str,
       apply="hot", label="Backup folder",
       help="Blank means a 'backups' folder next to the database.",
       derive=lambda v: os.path.join(os.path.dirname(v["DB_PATH"]) or ".",
                                     "backups")),
    _S("BACKUP_KEEP", "int", "14", "backup", _int,
       apply="hot", min=0, max=365, label="Snapshots to keep",
       help="0 disables nightly backups entirely."),
    _S("BACKUP_HOUR", "int", "3", "backup", _int,
       apply="hot", min=0, max=23, label="Backup hour"),
    _S("BACKUP_MINUTE", "int", "30", "backup", _int,
       apply="hot", min=0, max=59, label="Backup minute"),

    # ---- Gemini (AI) -----------------------------------------------------
    _S("GEMINI_API_KEY", "secret", "", "gemini", _strip,
       apply="worker", secret=True, sibling_env=True, label="Gemini API key",
       help="Enables AI food parsing and the weekly calorie summary."),
    _S("GEMINI_MODEL", "str", "", "gemini", _strip,
       apply="worker", sibling_env=True, label="Model",
       help="Blank uses gemini-2.5-flash."),
    _S("BACKUP_GEMINI_MODEL", "str", "", "gemini", _strip,
       apply="worker", sibling_env=True, label="Fallback model",
       help="Tried when the primary model is overloaded. Blank disables the "
            "fallback."),
    _S("GEMINI_TIMEOUT", "int", "120", "gemini", _int,
       apply="worker", sibling_env=True, min=1, max=600,
       label="Request timeout (seconds)"),
    # Both carry the max(0, ...) floors gemini_client applies, so a negative
    # value clamps rather than reaching range() or asyncio.sleep().
    _S("GEMINI_MAX_RETRIES", "int", "2", "gemini", _floor_int(0),
       apply="worker", sibling_env=True, min=0, max=10, label="Max retries"),
    _S("GEMINI_RETRY_DELAY", "float", "0", "gemini", _floor_float(0.0),
       apply="worker", sibling_env=True, min=0, max=300,
       label="Fixed retry delay (seconds)",
       help="0 uses an escalating backoff instead."),

    # ---- Strava ----------------------------------------------------------
    _S("STRAVA_DISABLED", "bool", "0", "strava", _bool,
       apply="worker", label="Disable Strava entirely"),
    _S("STRAVA_CLIENT_ID", "str", "", "strava", _strip,
       apply="worker", sibling_env=True, label="Client ID"),
    _S("STRAVA_CLIENT_SECRET", "secret", "", "strava", _strip,
       apply="worker", secret=True, sibling_env=True, label="Client secret"),
    # rstrip('/') mirrors strava_client.config_from_env(), so a trailing slash
    # cannot produce a '//strava/webhook' callback that Strava won't match.
    _S("STRAVA_PUBLIC_URL", "str", "", "strava", _rstrip_slash,
       apply="worker", sibling_env=True, label="Public base URL",
       help="The externally reachable HTTPS base, e.g. "
            "https://strava.example.com. The OAuth callback and webhook URLs "
            "are derived from it."),
    _S("STRAVA_REDIRECT_URI", "str", "", "strava", _strip,
       apply="worker", sibling_env=True, label="OAuth redirect override",
       help="Blank derives <public URL>/strava/callback."),
    _S("STRAVA_WEBHOOK_CALLBACK_URL", "str", "", "strava", _strip,
       apply="worker", sibling_env=True, label="Webhook URL override",
       help="Blank derives <public URL>/strava/webhook."),
    _S("STRAVA_WEBHOOK_VERIFY_TOKEN", "secret", "gymbot", "strava", _strip,
       apply="worker", secret=True, sibling_env=True, label="Webhook verify token",
       help="Any string. Strava echoes it back when validating the "
            "subscription."),
    _S("STRAVA_FEED_CHANNEL_ID", "id", "", "strava", _snowflake_signed,
       apply="worker", label="Feed channel",
       help="Where every linked athlete's finished workouts post.",
       restart_note="Setting this for the first time needs a restart."),
    _S("STRAVA_API_BASE", "str", "https://www.strava.com/api/v3", "strava",
       _rstrip_slash, apply="worker", sibling_env=True, label="API base URL",
       help="Only change this if Strava moves the API host."),
    _S("STRAVA_MAPBOX_TOKEN", "secret", "", "strava", _strip,
       apply="worker", secret=True, label="Mapbox token",
       help="Optional. When set, route maps render over a real basemap "
            "instead of a bare line silhouette."),
    _S("STRAVA_MAP_STYLE", "str", "outdoors-v12", "strava",
       _stripped_or("outdoors-v12"),
       apply="worker", label="Mapbox style",
       help="outdoors-v12 (terrain), streets-v12 (roads), "
            "satellite-streets-v12 (aerial)."),
    _S("STRAVA_SPORT_TYPES", "csv", "", "strava", _csv_lower,
       apply="worker", label="Sport allow-list",
       help="Comma-separated Strava sport_type values, e.g. "
            "Run,Ride,WeightTraining. Empty allows all."),
    # ``float(os.getenv(k, "0") or 0)`` -- an explicitly empty value means 0
    # rather than raising (app/bot.py:10847, :10851).
    _S("STRAVA_MIN_DISTANCE_M", "float", "0", "strava", _float_or_zero,
       apply="worker", min=0, max=1000000, label="Minimum distance (m)",
       help="0 disables the filter. Only applies to distance sports."),
    _S("STRAVA_MIN_DURATION_S", "int", "0", "strava", _int_or_zero,
       apply="worker", min=0, max=86400, label="Minimum duration (s)",
       help="0 disables the filter."),
    _S("STRAVA_IMPERIAL", "bool", "0", "strava", _bool,
       apply="worker", label="Imperial units in embeds"),
    _S("STRAVA_AUTO_SUBSCRIBE", "bool", "1", "strava", _bool,
       apply="worker", label="Auto-create webhook subscription",
       help="Requires the callback URL to be publicly reachable when it runs."),
    _S("STRAVA_FERNET_KEY", "secret", "", "strava", _strip,
       apply="worker", secret=True, sibling_env=True,
       label="Strava encryption key",
       help="Encrypts stored Strava tokens. Managed automatically -- only set "
            "this if you are migrating an existing deployment."),
    _S("STRAVA_BIND_HOST", "str", "0.0.0.0", "strava", _stripped_or("0.0.0.0"),
       apply="env-only", label="Webhook bind host",
       help="Container-internal. Changing it requires editing docker-compose.yml."),
    _S("STRAVA_PORT", "int", "8080", "strava", _int,
       apply="env-only", label="Webhook port",
       help="Container-internal. Must match the compose port mapping, so it "
            "is not editable here."),

    # ---- Apple Health ----------------------------------------------------
    # Apple exposes HealthKit only on the member's device, so an iPhone
    # Shortcut sends summaries to the same tiny HTTPS server used by Strava.
    _S("APPLE_HEALTH_DISABLED", "bool", "0", "apple_health", _bool,
       apply="worker", label="Disable Apple Health entirely"),
    _S("APPLE_HEALTH_PUBLIC_URL", "str", "", "apple_health", _rstrip_slash,
       apply="worker", label="Public base URL",
       help="Externally reachable HTTPS base for the iPhone Shortcut. Blank "
            "reuses the Strava public URL."),
    _S("APPLE_HEALTH_FEED_CHANNEL_ID", "id", "", "apple_health",
       _snowflake_signed, apply="worker", label="Feed channel",
       help="Where imported Apple Health workouts post. Blank reuses the "
            "Strava feed channel.",
       restart_note="Setting this for the first time needs a restart."),

    # ---- Revo ------------------------------------------------------------
    _S("REVO_DISABLED", "bool", "0", "revo", _bool,
       apply="worker", label="Disable Revo entirely"),
    _S("REVO_NOTIFY_CHANNEL_ID", "id", "", "revo", _snowflake,
       apply="worker", label="Default notification channel",
       help="Fallback for linked accounts that do not pin their own."),
    _S("REVO_POLL_MINUTES", "int", "10", "revo", _floor_int(5),
       apply="worker", min=5, max=1440, label="Poll interval (minutes)",
       help="Floored at 5 to stay within the portal's politeness limit."),
    _S("REVO_USER", "str", "", "revo", _strip,
       apply="worker", sibling_env=True, label="Shared account email",
       help="Used by /busy for gym occupancy. Optional."),
    _S("REVO_PASS", "secret", "", "revo", _strip,
       apply="worker", secret=True, sibling_env=True,
       label="Shared account password"),
    _S("REVO_FERNET_KEY", "secret", "", "revo", _strip,
       apply="worker", secret=True, sibling_env=True,
       label="Revo encryption key",
       help="Encrypts every linked member's Revo password at rest. Managed "
            "automatically -- only set this if you are migrating an existing "
            "deployment. Rotating it makes stored credentials unreadable."),

    # ---- Hevy ------------------------------------------------------------
    _S("HEVY_DISABLED", "bool", "0", "hevy", _bool,
       apply="worker", label="Disable Hevy entirely"),
    _S("HEVY_FEED_CHANNEL_ID", "id", "", "hevy", _snowflake,
       apply="worker", label="Feed channel",
       restart_note="Setting this for the first time needs a restart."),
    _S("HEVY_POLL_MINUTES", "int", "15", "hevy", _floor_int(1),
       apply="worker", min=1, max=1440, label="Poll interval (minutes)"),
    _S("HEVY_PUSH_BODYWEIGHT", "bool", "1", "hevy", _bool,
       apply="worker", label="Mirror weigh-ins to Hevy",
       help="When a member logs a bodyweight -- by hand or from a linked "
            "smart scale -- write it to their Hevy body measurements too, "
            "along with body fat and lean mass when the scale reports them. "
            "Only affects members who linked Hevy. Existing hand-entered "
            "measurements (waist, chest, ...) are never overwritten."),
    _S("HEVY_FERNET_KEY", "secret", "", "hevy", _strip,
       apply="worker", secret=True, sibling_env=True,
       label="Hevy encryption key",
       help="Managed automatically -- only set this if you are migrating an "
            "existing deployment."),

    # ---- Home Assistant --------------------------------------------------
    # There is deliberately no URL or token here. Every member brings their own
    # Home Assistant, set up with /setup_ha, and the credential is stored
    # Fernet-encrypted per user like the Hevy/Strava/Revo ones -- so an operator
    # never holds somebody else's house key, and members on different servers can
    # all use the feature. What is left here is the global tuning.
    _S("HA_DISABLED", "bool", "0", "homeassistant", _bool,
       apply="worker", label="Disable Home Assistant entirely"),
    _S("HA_POLL_MINUTES", "int", "10", "homeassistant", _floor_int(1),
       apply="worker", min=1, max=1440, label="Poll interval (minutes)",
       help="How often to check for new weigh-ins. A scale only reports when "
            "someone stands on it, so this is cheap."),
    _S("HA_BACKFILL_DAYS", "int", "14", "homeassistant", _floor_int(0),
       apply="worker", min=0, max=90, label="Import weigh-ins from the last (days)",
       help="How far back past weigh-ins are imported. The first link is "
            "announced as one summary rather than an alert each. 0 means the "
            "current reading only. Where the history comes from depends on the "
            "scale -- see docs/HOME_ASSISTANT.md; if it comes from Home "
            "Assistant's recorder, that keeps 10 days by default."),
    _S("HA_IGNORE_ENTITIES", "csv", "", "homeassistant", _csv_lower,
       apply="worker", label="Ignore entities containing",
       help="Comma-separated text fragments. Any body sensor whose entity id "
            "contains one is ignored completely -- not listed, not linkable, "
            "never polled. Use it for the phantom entities a phone or fitness "
            "bridge creates and then never writes to, e.g. _iphone."),
    _S("HA_VERIFY_SSL", "bool", "1", "homeassistant", _bool,
       apply="worker", label="Verify the TLS certificate",
       help="Turn off only if someone's Home Assistant uses a self-signed "
            "certificate. Plain http:// on a LAN is unaffected. This applies to "
            "every member, so prefer a real certificate."),
    _S("HA_FERNET_KEY", "secret", "", "homeassistant", _strip,
       apply="worker", secret=True, sibling_env=True,
       label="Home Assistant encryption key",
       help="Encrypts every member's stored access token at rest. Managed "
            "automatically -- only set this if you are migrating an existing "
            "deployment. Rotating it makes stored tokens unreadable."),

    # ---- Presence / voice / moderation -----------------------------------
    _S("ENABLE_PRESENCE_TRACKING", "bool", "false", "presence", _bool,
       apply="worker", label="Presence & activity tracking",
       help="Powers /track, the Activity tab and the Sleep tab.",
       restart_note="Requests the PRIVILEGED Presence intent. Turn it on at "
                    "discord.com/developers -> your app -> Bot -> Privileged "
                    "Gateway Intents FIRST, or the bot cannot connect."),
    _S("ENABLE_VOICE_TRACKING", "bool", "true", "voice", _bool,
       apply="hot", label="Voice-channel tracking",
       help="Uses only the non-privileged voice_states intent."),
    _S("AUTO_UNTIMEOUT", "bool", "true", "moderation", _bool,
       apply="worker", label="Automatically remove timeouts",
       restart_note="Requests the PRIVILEGED Server Members intent."),

    # ---- Message logging & media -----------------------------------------
    _S("ENABLE_MESSAGE_LOGGING", "bool", "true", "messages", _bool,
       apply="hot", label="Log message content",
       help="Captures full message content across all channels for the "
            "dashboard's Messages tab."),
    _S("MESSAGE_LOG_BACKFILL_DAYS", "int", "30", "messages", _int,
       apply="worker", min=0, max=3650, label="History to seed on startup (days)",
       help="0 disables the seed."),
    _S("ENABLE_MEDIA_DOWNLOAD", "bool", "true", "messages", _bool,
       apply="hot", label="Download attachments",
       help="Keeps images and videos after Discord's CDN links expire."),
    _S("MEDIA_MAX_MB", "float", "50", "messages", _float,
       apply="hot", min=0, max=10000, label="Maximum attachment size (MB)",
       help="Larger files are left as a remote URL. 0 means no cap."),
    _S("MEDIA_DIR", "path", "", "messages", _str,
       apply="hot", label="Media folder",
       help="Blank means a 'media' folder next to the database.",
       derive=lambda v: os.path.join(os.path.dirname(v["DB_PATH"]) or ".",
                                     "media")),

    # ---- Game icons ------------------------------------------------------
    # Refreshed by the supervisor, so the cache stays warm while the bot is down.
    _S("GAME_ICONS_CACHE", "path", "", "gameicons", _str,
       apply="worker", label="Icon cache file",
       help="Blank means game_icons.json next to the database.",
       derive=lambda v: os.path.join(os.path.dirname(v["DB_PATH"]) or ".",
                                     "game_icons.json")),
    _S("GAME_ICONS_REFRESH_DAYS", "float", "7", "gameicons", _float,
       apply="hot", min=0, max=365, label="Refresh interval (days)",
       help="0 forces a refresh on every boot."),

    # ---- Dashboard -------------------------------------------------------
    # Deliberately env-only, and therefore rendered disabled. Authentication
    # reads this from the process environment (or the stored PBKDF2 hash) and
    # never from app_settings, so making it writable here would accept a new
    # password, report success, and change nothing — while an operator who
    # believed they had just rotated it would not set a real one. Rotation goes
    # through Settings -> Dashboard account -> Change password.
    _S("WEBUI_PASSWORD", "secret", "", "webui", _strip,
       apply="env-only", secret=True, label="Dashboard password (legacy)",
       help="Read only from docker-compose.yml, for deployments that still pin "
            "it there. It overrides the dashboard password while set. To change "
            "your password use Change password below; to stop pinning it, "
            "remove it from your compose file — the same password keeps "
            "working."),
    _S("ENABLE_MEMBER_MIRROR", "bool", "false", "webui", _bool,
       apply="worker", label="Mirror members & roles",
       help="Populates the Members and Roles tabs and the audit log. Without "
            "it those tabs stay empty.",
       restart_note="Requests the PRIVILEGED Server Members intent. Turn it on "
                    "at discord.com/developers -> your app -> Bot -> Privileged "
                    "Gateway Intents FIRST, or the bot cannot connect."),
    _S("WEBUI_DISABLED", "bool", "0", "webui", _bool,
       apply="env-only", label="Disable the dashboard",
       help="Stops the dashboard binding at all. Only settable in "
            "docker-compose.yml, because turning it off from inside the "
            "dashboard would be a one-way door."),
    _S("WEBUI_BIND_HOST", "str", "0.0.0.0", "webui", _stripped_or("0.0.0.0"),
       apply="env-only", label="Bind host",
       help="Container-internal. Changing it here could make the dashboard "
            "unreachable, so it is only settable in docker-compose.yml."),
    _S("WEBUI_PORT", "int", "8081", "webui", _int,
       apply="env-only", label="Port",
       help="Container-internal. Must match the compose port mapping."),

    # ---- Storage & logging -----------------------------------------------
    _S("DB_PATH", "path", "/data/gym.sqlite3", "storage", _str,
       apply="env-only", label="Database file",
       help="Cannot be changed here -- the settings you are reading are stored "
            "in this file. Edit docker-compose.yml if you must move it."),
    _S("LOG_LEVEL", "enum", "INFO", "logging", _str,
       apply="worker", label="Log level",
       choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")),
    _S("LOG_FORMAT", "enum", "text", "logging", _str,
       apply="worker", label="Log format", choices=("text", "json"),
       help="'json' emits one JSON object per line for log shippers."),
)

SPEC: dict[str, Setting] = {s.key: s for s in _SETTINGS}

#: Keys that are only ever read from the process environment. They are rendered
#: disabled in the dashboard because the host half of every port mapping and
#: volume mount lives in docker-compose.yml, where the UI cannot reach. This is
#: what makes locking yourself out of the dashboard structurally impossible
#: rather than merely unlikely.
ENV_ONLY: frozenset[str] = frozenset(
    k for k, s in SPEC.items() if s.apply == "env-only"
)

#: Keys that sibling modules (strava_client, gemini_client, revo_client,
#: hevy_client, parser, targets) read directly with os.getenv. Those modules are
#: deliberately NOT modified by this refactor -- routing their reads through the
#: database would put a lock-protected SQLite read inside executor-thread poll
#: loops and invert the layering. Instead the supervisor exports the resolved
#: value into the worker's environment, which keeps all three Fernet fallback
#: chains and ~70 monkeypatch.setenv test call sites working byte-identically.
SIBLING_ENV_KEYS: frozenset[str] = frozenset(
    k for k, s in SPEC.items() if s.sibling_env
)

# A key exported into the child's environment is a spawn-time snapshot, so it
# can never be hot-reloaded. Asserted rather than documented so the invariant
# cannot silently drift as settings are added.
assert all(SPEC[k].apply != "hot" for k in SIBLING_ENV_KEYS), (
    "a key exported into the worker's environment cannot be marked apply='hot'"
)
assert not (SIBLING_ENV_KEYS & ENV_ONLY), (
    "env-only keys are never exported as sibling env"
)

GROUP_LABELS: dict[str, str] = {
    "discord": "Discord",
    "admin": "Admins & permissions",
    "core": "Core parsing",
    "reminder": "Reminders",
    "daily": "Daily update",
    "report": "Weekly report",
    "backup": "Backups",
    "gemini": "AI (Gemini)",
    "strava": "Strava",
    "apple_health": "Apple Health",
    "revo": "Revo Fitness",
    "hevy": "Hevy",
    "homeassistant": "Home Assistant",
    "presence": "Presence",
    "voice": "Voice",
    "moderation": "Moderation",
    "messages": "Message logging & media",
    "gameicons": "Game icons",
    "webui": "Dashboard",
    "storage": "Storage",
    "logging": "Logging",
}

#: Render order for the Settings tab -- most-used first.
GROUP_ORDER: tuple[str, ...] = (
    "discord", "admin", "core", "reminder", "daily", "report",
    "strava", "apple_health", "revo", "hevy", "homeassistant", "gemini", "messages",
    "presence", "voice",
    "moderation", "gameicons", "backup", "webui", "storage", "logging",
)

assert set(GROUP_ORDER) == set(GROUP_LABELS) == {s.group for s in _SETTINGS}, (
    "every group must be labelled and ordered"
)


class Config:
    """An immutable resolved snapshot of every setting.

    Indexing returns the coerced value; :meth:`raw` returns the string it came
    from and :meth:`source` says which layer won. Callers that care about
    precedence (the Settings tab) use the latter two; everything else just
    indexes.
    """

    __slots__ = ("_values", "_raws", "_sources")

    def __init__(self, values: dict[str, Any], raws: dict[str, str],
                 sources: dict[str, Source]) -> None:
        self._values = values
        self._raws = raws
        self._sources = sources

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __contains__(self, key: str) -> bool:
        return key in self._values

    def raw(self, key: str) -> str:
        return self._raws.get(key, "")

    def source(self, key: str) -> Source:
        return self._sources.get(key, "default")

    def pinned(self, key: str) -> bool:
        """True when the environment is supplying this value, so a dashboard
        edit is stored but not in effect."""
        return self._sources.get(key) == "env"

    def as_dict(self) -> dict[str, Any]:
        return dict(self._values)

    def diff(self, other: "Config") -> set[str]:
        return {k for k in SPEC if self._values.get(k) != other._values.get(k)}

    def restart_scope(self, changed: Iterable[str]) -> Apply:
        """The strongest apply scope among ``changed``.

        'hot' only when every changed key is hot; any worker-scope key in the
        batch means the whole batch needs a bounce.
        """
        rank = {"hot": 0, "worker": 1, "supervisor": 2, "env-only": 3}
        worst = "hot"
        for key in changed:
            s = SPEC.get(key)
            if s is not None and rank[s.apply] > rank[worst]:
                worst = s.apply
        return worst

    def as_child_env(self) -> dict[str, str]:
        """The environment handed to the bot worker at spawn.

        Two kinds of key go in, and nothing else:

        * anything the operator genuinely pinned in the container environment,
          verbatim, so env-wins precedence still holds inside the child;
        * every :data:`SIBLING_ENV_KEYS` value, resolved, because the modules
          that read them are not touched by this refactor.

        Everything else is deliberately omitted so the worker's own
        :func:`load` reads it from the database and hot reload can change it.
        """
        out = {k: self.raw(k) for k in SPEC if self.source(k) == "env"}
        out.update({
            k: self.raw(k) for k in SIBLING_ENV_KEYS if self.raw(k)
        })
        return out


def _coerce_one(setting: Setting, raw: str) -> Any:
    return setting.coerce(raw)


def load(db: Any = None, env: Mapping[str, str] | None = None,
         *, decrypt: Callable[[str], str] | None = None) -> Config:
    """Resolve every setting from environment, then database, then default.

    Never raises. A value that fails to coerce logs an error and falls back to
    its code default -- the guarantee that a typo saved through the dashboard
    cannot brick the next boot.
    """
    env = os.environ if env is None else env
    stored: dict[str, str | None] = {}
    if db is not None:
        try:
            stored = db.settings_all(decrypt=decrypt)
        except Exception as exc:  # noqa: BLE001 - never let config load fail
            LOG.error("Could not read stored settings (%s) -- "
                      "using environment and defaults only.", exc)

    raws: dict[str, str] = {}
    sources: dict[str, Source] = {}
    for s in _SETTINGS:
        env_value = env.get(s.key)
        if env_value is not None and env_value != "":
            raws[s.key], sources[s.key] = env_value, "env"
            continue
        db_value = stored.get(s.key)
        if db_value is not None and db_value != "":
            raws[s.key], sources[s.key] = db_value, "db"
            continue
        raws[s.key], sources[s.key] = s.default, "default"

    values: dict[str, Any] = {}
    for s in _SETTINGS:
        try:
            values[s.key] = _coerce_one(s, raws[s.key])
        except Exception as exc:  # noqa: BLE001 - anti-brick guarantee
            shown = "***" if s.secret else repr(raws[s.key])
            LOG.error(
                "Setting %s=%s is invalid (%s) -- falling back to the default "
                "%r. Fix it in the dashboard's Settings tab.",
                s.key, shown, exc, s.default,
            )
            values[s.key] = _coerce_one(s, s.default)
            sources[s.key] = "default"
            raws[s.key] = s.default

    # Second pass: fallback chains.
    #
    # The trigger is a falsy COERCED value, not an empty raw string. That
    # distinction matters: the originals were written as
    # ``int(x) if x.isdigit() else <fallback>``, so a non-numeric value fell
    # back too. Keying off the raw string instead would mean a pasted "<#123>"
    # mention silently disabled the bodyweight reminder rather than inheriting
    # the weekly reminder's channel. None and "" are the only falsy results the
    # derived settings can produce.
    #
    # Order matters -- WEEKLY_REPORT_CHANNEL_ID reads DAILY_UPDATE_CHANNEL_ID --
    # so this runs in registry order, which is dependency order.
    for s in _SETTINGS:
        if s.derive is not None and not values[s.key]:
            try:
                values[s.key] = s.derive(values)
            except Exception as exc:  # noqa: BLE001
                LOG.error("Could not derive %s (%s).", s.key, exc)

    return Config(values, raws, sources)


def validate(key: str, raw: str) -> str | None:
    """Return a human-readable error, or None when ``raw`` is acceptable.

    Called by the settings API before anything is written, so a bad value is a
    400 with a field-level message instead of a row that quietly reverts to a
    default on the next boot.
    """
    s = SPEC.get(key)
    if s is None:
        return f"Unknown setting {key!r}."
    if s.apply == "env-only":
        return (f"{key} can only be set in docker-compose.yml, because the "
                f"host side of it lives there.")
    if raw == "":
        return None  # empty always means "unset"; the default takes over

    try:
        value = s.coerce(raw)
    except Exception:  # noqa: BLE001
        return _kind_message(s)

    if s.choices and raw.strip().lower() not in {c.lower() for c in s.choices}:
        return f"Must be one of: {', '.join(s.choices)}."

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if s.min is not None and value < s.min:
            return f"Must be at least {_num(s.min)}."
        if s.max is not None and value > s.max:
            return f"Must be at most {_num(s.max)}."

    if s.kind == "id":
        # The coercion returns None for junk rather than raising, which would
        # silently disable the very feature this field enables. Reject it at
        # the point of entry instead, where we can explain how to get an ID.
        #
        # Only STRAVA_FEED_CHANNEL_ID accepts a leading '-'; for every other ID
        # a negative value coerces to None, so accepting one here would store a
        # value that reads back as "unset" while the UI said "Saved".
        text = raw.strip()
        ok = (text.lstrip("-").isdigit() if s.coerce is _snowflake_signed
              else text.isdigit())
        if not ok:
            return ("Must be a numeric Discord ID. Enable Developer Mode in "
                    "Discord (Settings -> Advanced), then right-click the "
                    "channel or role and choose Copy ID.")

    if s.kind == "ids":
        bad = [x.strip() for x in raw.split(",")
               if x.strip() and not x.strip().isdigit()]
        if bad:
            return (f"Not a numeric Discord ID: {', '.join(bad[:3])}. "
                    f"Use a comma-separated list of IDs.")

    if s.kind == "tz":
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(raw.strip())
        except Exception:  # noqa: BLE001
            return (f"Unknown timezone {raw.strip()!r}. Use an IANA name like "
                    f"Australia/Adelaide or Europe/London.")

    if key == "DB_PATH" and raw.strip() == ":memory:":
        return ("':memory:' cannot be shared between the supervisor and the "
                "bot -- each would get its own empty database.")

    return None


#: A DNS label: 1-63 chars of letters/digits/hyphen, not starting or ending with
#: a hyphen. The 63-char ceiling is what catches a mis-pasted access token, whose
#: base64 segments run well past it — and a rejected token here is the difference
#: between an error at the point of entry and a credential echoed back into a
#: Discord reply by the connection-failure message.
_HOST_LABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def bad_base_url(raw: str) -> str | None:
    """Validate a user-entered Home Assistant URL. Returns an error, or None.

    Accepts what actually works: a bare host, a host:port, either scheme, a
    single-label Docker service name (``http://homeassistant``), a public domain
    and an IP. The aim is to reject values that would be accepted and then fail
    on every poll with a connection error nobody can trace back to what they
    typed. Called by ``/setup_ha`` before anything is stored.
    """
    text = raw.strip()
    if not text:
        return None
    if any(ch.isspace() for ch in text):
        return "A URL cannot contain spaces -- did a token get pasted here?"
    scheme, sep, rest = text.partition("://")
    if sep and scheme.lower() not in ("http", "https"):
        return "Must start with http:// or https:// (or just the host:port)."
    authority = (rest if sep else text).split("/")[0]
    if "@" in authority:
        return ("Home Assistant does not use user:password in the URL -- put the "
                "long-lived access token in the token field instead.")
    host, _, port = authority.rpartition(":") if ":" in authority else (
        authority, "", "")
    if port and not port.isdigit():
        return f"{port!r} is not a valid port number."
    if port and not (0 < int(port) < 65536):
        return "Port must be between 1 and 65535."
    if host.startswith("[") and host.endswith("]"):
        return None  # bracketed IPv6 literal; trust it rather than reimplement it
    if not host or not all(_HOST_LABEL.match(part) for part in host.split(".")):
        return ("Must be the address you open Home Assistant on, e.g. "
                "`http://192.168.1.50:8123` or `https://home.example.com`.")
    return None


def bad_access_token(raw: str) -> str | None:
    """Validate a user-pasted long-lived access token. Returns an error, or None.

    A long-lived token is a long JWT copied out of a scrolling text box, so it
    arrives with a wrapped newline more often than not. Home Assistant splits the
    Authorization header on the first space, so an embedded one produces a 401
    that reads as "wrong token" and sends the member back to make another.
    """
    text = (raw or "").strip()
    if not text:
        return "I need the access token as well."
    if any(ch.isspace() for ch in text):
        return ("That token has a space or line break in it -- copy it again in "
                "one piece (it should be a single long line).")
    if text.count(".") != 2 or len(text) < 60:
        return ("That doesn't look like a long-lived access token. Create one "
                "under your Home Assistant profile -> Security -> Long-lived "
                "access tokens, and paste the whole thing.")
    return None


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _kind_message(s: Setting) -> str:
    if s.kind in ("int",):
        lo, hi = s.min, s.max
        if lo is not None and hi is not None:
            return f"Must be a whole number between {_num(lo)} and {_num(hi)}."
        return "Must be a whole number."
    if s.kind == "float":
        return "Must be a number."
    if s.kind == "id":
        return "Must be a numeric Discord ID."
    if s.kind == "ids":
        return "Must be a comma-separated list of numeric Discord IDs."
    return "Invalid value."


class JsonFormatter(logging.Formatter):
    """Minimal JSON log formatter -- chosen over python-json-logger to avoid
    pulling in a dep just for one optional output mode. Container log shippers
    (Loki, Datadog, etc.) generally prefer one JSON object per line.

    Moved here from app/bot.py so the supervisor can format its own logs
    identically without importing the bot; app.bot re-exports it under the old
    private name so nothing that referenced it has to change.
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        import json

        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def install_logging(cfg: Config) -> None:
    """Configure the root logger from ``cfg``.

    Replaces the import-time block at app/bot.py:113-124. An unrecognised
    LOG_LEVEL degrades to INFO with a warning instead of raising ValueError and
    killing the process at import, which is what used to happen.
    """
    handler = logging.StreamHandler()
    if cfg["LOG_FORMAT"].lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    # basicConfig only accepts an int or an UPPERCASE level name, so the value
    # actually passed must be normalised -- not merely normalised for the
    # check. Getting that wrong meant LOG_LEVEL=debug raised ValueError out of
    # the supervisor before the dashboard had bound, crash-looping the
    # container with no way in but hand-editing SQLite on the volume.
    raw_level = str(cfg["LOG_LEVEL"]).strip()
    level = raw_level.upper()
    if not isinstance(getattr(logging, level, None), int):
        LOG.warning("Unknown LOG_LEVEL=%r -- using INFO.", raw_level)
        level = "INFO"
    logging.basicConfig(level=level, handlers=[handler], force=True)


def bootstrap_db_path(env: Mapping[str, str] | None = None) -> str:
    """Where the database lives, resolved from the environment alone.

    Deliberately does not consult the database: this is the value used to OPEN
    it, so it is the one setting that genuinely cannot be stored inside itself.
    """
    env = os.environ if env is None else env
    return (env.get("DB_PATH") or "").strip() or SPEC["DB_PATH"].default
