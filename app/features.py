"""Shared policy for workout integrations and smart-scale imports."""

INTEGRATION_COMMANDS = frozenset({
    "help", "server", "sync", "hevy", "strava_link", "strava_unlink",
    "strava_status", "strava_latest", "strava_backfill", "strava_subscribe",
    "strava_subscription", "strava_unsubscribe", "strava_refresh_maps",
    "setup_ha", "ha_link", "ha_entities", "ha_unlink", "ha_status",
    "ha_body", "ha_graph", "ha_help", "bodyweight_history", "bodyweight_graph",
})

# Operational controls remain available; disabled features retain their data
# and stored configuration, so switching back does not require relinking.
INTEGRATION_SETTINGS_GROUPS = frozenset({
    "discord", "admin", "core", "strava", "hevy", "backup", "webui",
    "storage", "logging", "homeassistant",
})
HIDDEN_SETTINGS = frozenset({
    "GYM_CHANNEL_IDS", "MIN_LIFTS_FOR_AUTO", "PARSE_REPLY_MAX_ITEMS",
    "BACKFILL_ON_START", "BACKFILL_LIMIT", "PLATE_KG",
    "ENABLE_MEMBER_MIRROR", "HEVY_PUSH_BODYWEIGHT",
})

PROFILE_OVERRIDES = {
    "APPLE_HEALTH_DISABLED": True,
    "REVO_DISABLED": True,
    "BACKFILL_ON_START": False,
    "ENABLE_PRESENCE_TRACKING": False,
    "ENABLE_VOICE_TRACKING": False,
    "ENABLE_MESSAGE_LOGGING": False,
    "ENABLE_MEDIA_DOWNLOAD": False,
    "ENABLE_MEMBER_MIRROR": False,
    "AUTO_UNTIMEOUT": False,
    "HEVY_PUSH_BODYWEIGHT": False,
    "REMINDER_CHANNEL_ID": None,
    "DAILY_UPDATE_CHANNEL_ID": None,
    "WEEKLY_REPORT_CHANNEL_ID": None,
}

ALLOWED_PATHS = frozenset({
    "/", "/overview", "/hevy", "/strava", "/settings", "/login",
    "/logout", "/setup", "/logo.svg", "/healthz", "/api/guilds",
    "/api/overview", "/api/integrations", "/api/members", "/api/worker",
    "/api/password", "/api/hevy/equipment", "/api/hevy/equipment/set",
})


def path_enabled(path: str) -> bool:
    return path in ALLOWED_PATHS or path == "/api/settings" or path.startswith(
        "/api/settings/"
    )
