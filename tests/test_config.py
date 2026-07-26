"""Tests for the settings layer (app/config.py).

The most important test in this file is
:func:`test_defaults_match_pre_refactor_behaviour`. Before this refactor, ~87
constants were bound at import time in ``app/bot.py`` by direct ``os.getenv``
calls. Every one of those was transcribed into ``SPEC``, quirks included, and a
single mistyped default would silently change a running deployment's behaviour
on upgrade -- exactly what the backwards-compatibility requirement forbids, and
exactly the kind of thing that ships green when CI has no coverage gate.

So that test asserts the RESOLVED values (a set, a float, None) rather than the
raw default strings the registry stores. That is a different representation of
the same fact, so a broken coercion function cannot pass it.
"""
from __future__ import annotations

import os

import pytest

from app import config as config_mod
from app.db import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "cfg.sqlite3")
    yield d
    d.close()


def _empty() -> config_mod.Config:
    """Config with no environment and no database -- pure code defaults."""
    return config_mod.load(db=None, env={})


# ---------------------------------------------------------------------------
# The equivalence guard
# ---------------------------------------------------------------------------

#: Every value app/bot.py computed at import time with nothing set in the
#: environment, transcribed from the pre-refactor source. Kept as resolved
#: Python values on purpose -- see this module's docstring.
PRE_REFACTOR_DEFAULTS = {
    # core
    "MIN_LIFTS_FOR_AUTO": 2,
    "PARSE_REPLY_MAX_ITEMS": 15,
    "BACKFILL_ON_START": True,
    "BACKFILL_LIMIT": 1000,
    "SHOW_LB": False,
    "MAX_WEIGHT_KG": 500.0,
    "PLATE_KG": 20.0,
    "DISPLAY_TIMEZONE": "Australia/Adelaide",
    # discord
    "DISCORD_TOKEN": "",
    "GUILD_ID": None,
    "COMMAND_SCOPE": "global",
    "GYM_CHANNEL_IDS": set(),
    "ADMIN_USER_IDS": set(),
    # weekly reminder
    "REMINDER_CHANNEL_ID": None,
    "REMINDER_WEEKDAY": 2,
    "REMINDER_HOUR": 12,
    "REMINDER_MINUTE": 0,
    "REMINDER_ROLE_ID": None,
    # bodyweight reminder
    "BODYWEIGHT_REMINDER_CHANNEL_ID": None,
    "BODYWEIGHT_REMINDER_WEEKDAY": 0,
    "BODYWEIGHT_REMINDER_HOUR": 7,
    "BODYWEIGHT_REMINDER_MINUTE": 30,
    "BODYWEIGHT_REMINDER_ROLE_ID": None,
    # daily update
    "DAILY_UPDATE_CHANNEL_ID": None,
    "DAILY_UPDATE_HOUR": 8,
    "DAILY_UPDATE_MINUTE": 0,
    "DAILY_UPDATE_POST_EMPTY": False,
    # weekly report
    "WEEKLY_REPORT_CHANNEL_ID": None,
    "WEEKLY_REPORT_WEEKDAY": 6,
    "WEEKLY_REPORT_HOUR": 18,
    "WEEKLY_REPORT_MINUTE": 0,
    # backups
    "BACKUP_KEEP": 14,
    "BACKUP_HOUR": 3,
    "BACKUP_MINUTE": 30,
    # gemini
    "GEMINI_API_KEY": "",
    "GEMINI_MODEL": "",
    "BACKUP_GEMINI_MODEL": "",
    "GEMINI_TIMEOUT": 120,
    "GEMINI_MAX_RETRIES": 2,
    "GEMINI_RETRY_DELAY": 0.0,
    # strava
    "STRAVA_DISABLED": False,
    "STRAVA_CLIENT_ID": "",
    "STRAVA_CLIENT_SECRET": "",
    "STRAVA_PUBLIC_URL": "",
    "STRAVA_REDIRECT_URI": "",
    "STRAVA_WEBHOOK_CALLBACK_URL": "",
    "STRAVA_WEBHOOK_VERIFY_TOKEN": "gymbot",
    "STRAVA_FEED_CHANNEL_ID": None,
    "STRAVA_API_BASE": "https://www.strava.com/api/v3",
    "STRAVA_MAPBOX_TOKEN": "",
    "STRAVA_MAP_STYLE": "outdoors-v12",
    "STRAVA_SPORT_TYPES": set(),
    "STRAVA_MIN_DISTANCE_M": 0.0,
    "STRAVA_MIN_DURATION_S": 0,
    "STRAVA_IMPERIAL": False,
    "STRAVA_AUTO_SUBSCRIBE": True,
    "STRAVA_BIND_HOST": "0.0.0.0",
    "STRAVA_PORT": 8080,
    # revo
    "REVO_DISABLED": False,
    "REVO_NOTIFY_CHANNEL_ID": None,
    "REVO_POLL_MINUTES": 10,
    "REVO_USER": "",
    "REVO_PASS": "",
    # hevy
    "HEVY_DISABLED": False,
    "HEVY_FEED_CHANNEL_ID": None,
    "HEVY_POLL_MINUTES": 15,
    # toggles
    "ENABLE_PRESENCE_TRACKING": False,
    "ENABLE_VOICE_TRACKING": True,
    "ENABLE_MESSAGE_LOGGING": True,
    "ENABLE_MEDIA_DOWNLOAD": True,
    "AUTO_UNTIMEOUT": True,
    "MESSAGE_LOG_BACKFILL_DAYS": 30,
    "MEDIA_MAX_MB": 50.0,
    "GAME_ICONS_REFRESH_DAYS": 7.0,
    # Fernet keys: unset by default, and only ever generated when none exists.
    "REVO_FERNET_KEY": "",
    "STRAVA_FERNET_KEY": "",
    "HEVY_FERNET_KEY": "",
    # Derived from DB_PATH's directory when blank, matching the old
    # `os.getenv(K) or os.path.join(os.path.dirname(DB_PATH) or ".", ...)`.
    "BACKUP_DIR": os.path.join("/data", "backups"),
    "MEDIA_DIR": os.path.join("/data", "media"),
    "GAME_ICONS_CACHE": os.path.join("/data", "game_icons.json"),
    # dashboard / storage / logging
    "WEBUI_PASSWORD": "",
    "WEBUI_DISABLED": False,
    "WEBUI_BIND_HOST": "0.0.0.0",
    "WEBUI_PORT": 8081,
    "DB_PATH": "/data/gym.sqlite3",
    "LOG_LEVEL": "INFO",
    "LOG_FORMAT": "text",
}


@pytest.mark.parametrize("key,expected", sorted(PRE_REFACTOR_DEFAULTS.items()))
def test_defaults_match_pre_refactor_behaviour(key, expected):
    """Each default resolves to exactly what app/bot.py used to compute."""
    cfg = _empty()
    assert cfg[key] == expected, (
        f"{key} changed meaning: an existing deployment that never set it "
        f"would silently behave differently after upgrading."
    )


def test_every_setting_is_covered_by_the_equivalence_table():
    """A new setting must be added to the table above, or it ships unguarded.

    ENABLE_MEMBER_MIRROR is exempt: it is new in this refactor and has no
    pre-refactor value to be equivalent to.
    """
    missing = set(config_mod.SPEC) - set(PRE_REFACTOR_DEFAULTS)
    assert missing == {"ENABLE_MEMBER_MIRROR"}, (
        f"settings with no equivalence assertion: {sorted(missing)}"
    )


def test_member_mirror_defaults_off():
    """It requests a PRIVILEGED intent, so a fresh install must not enable it.

    If this ever defaults True, every deployment that has not enabled the
    Server Members intent in the Developer Portal crash-loops on first boot.
    """
    assert _empty()["ENABLE_MEMBER_MIRROR"] is False


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------

def test_environment_beats_database(db):
    db.settings_set("REMINDER_HOUR", "9")
    cfg = config_mod.load(db, {"REMINDER_HOUR": "21"})
    assert cfg["REMINDER_HOUR"] == 21
    assert cfg.source("REMINDER_HOUR") == "env"
    assert cfg.pinned("REMINDER_HOUR") is True


def test_database_beats_default(db):
    db.settings_set("REMINDER_HOUR", "9")
    cfg = config_mod.load(db, {})
    assert cfg["REMINDER_HOUR"] == 9
    assert cfg.source("REMINDER_HOUR") == "db"
    assert cfg.pinned("REMINDER_HOUR") is False


def test_empty_environment_value_is_treated_as_unset(db):
    """An empty env var must fall through, not override with emptiness."""
    db.settings_set("REMINDER_HOUR", "9")
    cfg = config_mod.load(db, {"REMINDER_HOUR": ""})
    assert cfg["REMINDER_HOUR"] == 9


def test_cleared_database_value_falls_back_to_default(db):
    db.settings_set("REMINDER_HOUR", "9")
    db.settings_set("REMINDER_HOUR", None)
    cfg = config_mod.load(db, {})
    assert cfg["REMINDER_HOUR"] == 12
    assert cfg.source("REMINDER_HOUR") == "default"


# ---------------------------------------------------------------------------
# The anti-brick guarantee
# ---------------------------------------------------------------------------

def test_a_bad_stored_value_falls_back_instead_of_raising(db):
    """A typo saved through the dashboard must not stop the next boot.

    Pre-refactor this exact input raised ValueError at import and killed the
    process; the whole point of routing config through here is that it cannot.
    """
    db.settings_set("MIN_LIFTS_FOR_AUTO", "not-a-number")
    cfg = config_mod.load(db, {})
    assert cfg["MIN_LIFTS_FOR_AUTO"] == 2
    assert cfg.source("MIN_LIFTS_FOR_AUTO") == "default"


def test_a_bad_environment_value_also_falls_back(db):
    cfg = config_mod.load(db, {"BACKUP_HOUR": "midnight"})
    assert cfg["BACKUP_HOUR"] == 3


def test_load_survives_a_database_that_raises():
    class Broken:
        def settings_all(self, **_kw):
            raise RuntimeError("disk gone")

    cfg = config_mod.load(Broken(), {"REMINDER_HOUR": "5"})
    assert cfg["REMINDER_HOUR"] == 5


# ---------------------------------------------------------------------------
# Fallback chains
# ---------------------------------------------------------------------------

def test_bodyweight_channel_inherits_the_reminder_channel():
    cfg = config_mod.load(None, {"REMINDER_CHANNEL_ID": "123"})
    assert cfg["BODYWEIGHT_REMINDER_CHANNEL_ID"] == 123


def test_fallback_fires_on_a_malformed_value_not_just_an_empty_one():
    """The originals were ``int(x) if x.isdigit() else <fallback>``.

    A pasted Discord mention is non-empty but not a snowflake. If the fallback
    only triggered on an empty string, this would silently disable the
    bodyweight reminder instead of inheriting the weekly reminder's channel.
    """
    cfg = config_mod.load(None, {
        "REMINDER_CHANNEL_ID": "987654321",
        "BODYWEIGHT_REMINDER_CHANNEL_ID": "<#123456789>",
    })
    assert cfg["BODYWEIGHT_REMINDER_CHANNEL_ID"] == 987654321

    cfg = config_mod.load(None, {
        "DAILY_UPDATE_CHANNEL_ID": "555",
        "WEEKLY_REPORT_CHANNEL_ID": "not-an-id",
    })
    assert cfg["WEEKLY_REPORT_CHANNEL_ID"] == 555


def test_gemini_floors_clamp_negative_values():
    """gemini_client wraps both in max(0, ...); a negative would otherwise
    reach range() or asyncio.sleep()."""
    assert config_mod.load(None, {"GEMINI_MAX_RETRIES": "-1"})[
        "GEMINI_MAX_RETRIES"] == 0
    assert config_mod.load(None, {"GEMINI_RETRY_DELAY": "-5"})[
        "GEMINI_RETRY_DELAY"] == 0.0


def test_public_url_loses_its_trailing_slash():
    """Otherwise the derived callback is https://host//strava/webhook, which
    Strava's subscription validation will not match."""
    assert config_mod.load(None, {
        "STRAVA_PUBLIC_URL": "https://strava.example.com/",
    })["STRAVA_PUBLIC_URL"] == "https://strava.example.com"


@pytest.mark.parametrize("level", ["debug", "Info", "WARNING", "warning"])
def test_install_logging_accepts_any_case_of_level(level):
    """basicConfig only takes an int or an UPPERCASE name.

    Passing a lowercase level raised ValueError out of the supervisor before
    the dashboard bound, crash-looping the container with no way to undo the
    setting except editing SQLite on the volume.
    """
    config_mod.install_logging(config_mod.load(None, {"LOG_LEVEL": level}))


def test_install_logging_survives_a_nonsense_level():
    config_mod.install_logging(config_mod.load(None, {"LOG_LEVEL": "chatty"}))


def test_bodyweight_channel_own_value_wins():
    cfg = config_mod.load(None, {
        "REMINDER_CHANNEL_ID": "123",
        "BODYWEIGHT_REMINDER_CHANNEL_ID": "456",
    })
    assert cfg["BODYWEIGHT_REMINDER_CHANNEL_ID"] == 456


def test_weekly_report_falls_back_through_daily_then_reminder():
    assert config_mod.load(None, {"DAILY_UPDATE_CHANNEL_ID": "1"})[
        "WEEKLY_REPORT_CHANNEL_ID"] == 1
    assert config_mod.load(None, {"REMINDER_CHANNEL_ID": "2"})[
        "WEEKLY_REPORT_CHANNEL_ID"] == 2
    # Daily wins over reminder, matching the old `or` chain's order.
    assert config_mod.load(None, {
        "DAILY_UPDATE_CHANNEL_ID": "1", "REMINDER_CHANNEL_ID": "2",
    })["WEEKLY_REPORT_CHANNEL_ID"] == 1


def test_directory_settings_derive_from_the_database_location():
    cfg = config_mod.load(None, {"DB_PATH": "/srv/gym/gym.sqlite3"})
    assert cfg["BACKUP_DIR"].replace("\\", "/") == "/srv/gym/backups"
    assert cfg["MEDIA_DIR"].replace("\\", "/") == "/srv/gym/media"
    assert cfg["GAME_ICONS_CACHE"].replace("\\", "/") == "/srv/gym/game_icons.json"


# ---------------------------------------------------------------------------
# Parsing quirks that must survive
# ---------------------------------------------------------------------------

def test_id_lists_silently_drop_non_numeric_entries():
    cfg = config_mod.load(None, {"ADMIN_USER_IDS": "1, oops ,2"})
    assert cfg["ADMIN_USER_IDS"] == {1, 2}


def test_strava_feed_channel_accepts_a_negative_id_but_others_do_not():
    """The one asymmetry in the codebase, preserved deliberately."""
    assert config_mod.load(None, {"STRAVA_FEED_CHANNEL_ID": "-5"})[
        "STRAVA_FEED_CHANNEL_ID"] == -5
    assert config_mod.load(None, {"REMINDER_CHANNEL_ID": "-5"})[
        "REMINDER_CHANNEL_ID"] is None


def test_poll_intervals_keep_their_politeness_floors():
    assert config_mod.load(None, {"REVO_POLL_MINUTES": "1"})[
        "REVO_POLL_MINUTES"] == 5
    assert config_mod.load(None, {"HEVY_POLL_MINUTES": "0"})[
        "HEVY_POLL_MINUTES"] == 1


def test_booleans_use_the_historic_truthy_set():
    for truthy in ("1", "true", "TRUE", "yes", "y", "on"):
        assert config_mod.load(None, {"SHOW_LB": truthy})["SHOW_LB"] is True
    for falsy in ("0", "false", "no", "off", "enabled", " true"):
        assert config_mod.load(None, {"SHOW_LB": falsy})["SHOW_LB"] is False


def test_whitespace_only_timezone_means_utc():
    assert config_mod.load(None, {"DISPLAY_TIMEZONE": "   "})[
        "DISPLAY_TIMEZONE"] == "UTC"


def test_api_base_loses_its_trailing_slash():
    cfg = config_mod.load(None, {"STRAVA_API_BASE": "https://example.com/api/"})
    assert cfg["STRAVA_API_BASE"] == "https://example.com/api"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_validate_rejects_out_of_range_and_malformed_values():
    assert config_mod.validate("REMINDER_HOUR", "24")
    assert config_mod.validate("REMINDER_HOUR", "-1")
    assert config_mod.validate("REMINDER_HOUR", "noon")
    assert config_mod.validate("REMINDER_CHANNEL_ID", "#general")
    assert config_mod.validate("DISPLAY_TIMEZONE", "Mars/Olympus")
    assert config_mod.validate("COMMAND_SCOPE", "everywhere")
    assert config_mod.validate("ADMIN_USER_IDS", "1,bob")


def test_validate_accepts_good_values():
    assert config_mod.validate("REMINDER_HOUR", "0") is None
    assert config_mod.validate("REMINDER_HOUR", "23") is None
    assert config_mod.validate("REMINDER_CHANNEL_ID", "123456789") is None
    assert config_mod.validate("DISPLAY_TIMEZONE", "Europe/London") is None
    assert config_mod.validate("COMMAND_SCOPE", "guild") is None
    assert config_mod.validate("ADMIN_USER_IDS", "1,2,3") is None
    # Empty always means "unset" and is always allowed.
    assert config_mod.validate("REMINDER_CHANNEL_ID", "") is None


def test_env_only_settings_cannot_be_written():
    for key in ("DB_PATH", "WEBUI_PORT", "WEBUI_BIND_HOST", "STRAVA_PORT"):
        assert config_mod.validate(key, "1234"), f"{key} must refuse writes"


def test_in_memory_database_path_is_rejected():
    """Two processes cannot share it -- each would get its own empty database."""
    assert config_mod.validate("DB_PATH", ":memory:")


# ---------------------------------------------------------------------------
# Child environment
# ---------------------------------------------------------------------------

def test_child_env_carries_env_pins_and_sibling_keys(db):
    db.settings_set("STRAVA_CLIENT_ID", "999")
    cfg = config_mod.load(db, {"REMINDER_HOUR": "21"})
    child = cfg.as_child_env()
    # An operator's pin must keep winning inside the child.
    assert child["REMINDER_HOUR"] == "21"
    # Sibling modules read this with os.getenv, so it must be exported even
    # though it came from the database.
    assert child["STRAVA_CLIENT_ID"] == "999"
    # Hot, non-sibling settings are deliberately absent so the child reads them
    # from the database and can hot-reload them.
    assert "MAX_WEIGHT_KG" not in child


def test_sibling_env_keys_are_never_hot():
    """Enforced in the module too; asserted here so the reason is discoverable.

    A key exported into the child's environment is a spawn-time snapshot, so
    marking it hot would promise a live update that cannot happen.
    """
    for key in config_mod.SIBLING_ENV_KEYS:
        assert config_mod.SPEC[key].apply != "hot"


def test_restart_scope_takes_the_strongest_change():
    cfg = _empty()
    assert cfg.restart_scope(["MAX_WEIGHT_KG", "SHOW_LB"]) == "hot"
    assert cfg.restart_scope(["MAX_WEIGHT_KG", "DISCORD_TOKEN"]) == "worker"


def test_every_setting_has_a_group_and_a_label():
    for key, s in config_mod.SPEC.items():
        assert s.group in config_mod.GROUP_LABELS, key
        assert s.kind, key
