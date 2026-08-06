# SQLite storage for lift entries.

from __future__ import annotations

import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import config as _config
from . import targets

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Iterable, Mapping

SCHEMA = """
CREATE TABLE IF NOT EXISTS lifts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    username      TEXT    NOT NULL,
    equipment     TEXT    NOT NULL,
    weight_kg     REAL    NOT NULL,
    bodyweight_add INTEGER NOT NULL DEFAULT 0,
    message_id    INTEGER,
    channel_id    INTEGER,
    logged_at     TEXT    NOT NULL,
    raw           TEXT,
    reps          INTEGER
);

CREATE INDEX IF NOT EXISTS idx_lifts_user_equip
    ON lifts (guild_id, user_id, equipment, logged_at);

CREATE INDEX IF NOT EXISTS idx_lifts_equip
    ON lifts (guild_id, equipment, weight_kg);

CREATE UNIQUE INDEX IF NOT EXISTS idx_lifts_dedupe
    ON lifts (message_id, equipment) WHERE message_id IS NOT NULL;

-- Per-user training goals. target_kg is the weight to hit; bodyweight_add
-- tracks whether the goal is relative to bodyweight (e.g. BW+30kg dips).
CREATE TABLE IF NOT EXISTS goals (
    guild_id       INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,
    equipment      TEXT    NOT NULL,
    target_kg      REAL    NOT NULL,
    bodyweight_add INTEGER NOT NULL DEFAULT 0,
    set_at         TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id, equipment)
);

-- Server-local alias table: lets users teach the bot nicknames the built-in
-- table doesn't know (e.g. "hack sled" -> "leg press").
CREATE TABLE IF NOT EXISTS custom_aliases (
    guild_id         INTEGER NOT NULL,
    alias_normalized TEXT    NOT NULL,
    canonical        TEXT    NOT NULL,
    added_by         INTEGER NOT NULL,
    added_at         TEXT    NOT NULL,
    PRIMARY KEY (guild_id, alias_normalized)
);

-- Tracks bot replies to parsed messages so we can implement reaction-based
-- undo. reply_message_id is what the user reacts on; message_id is the
-- original gym post whose rows we'd delete. lift_ids stores the inserted
-- row ids as a comma-separated string for /log-style single inserts where
-- there's no parseable message_id.
CREATE TABLE IF NOT EXISTS reply_tracking (
    reply_message_id INTEGER PRIMARY KEY,
    guild_id         INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    target_user_id   INTEGER,
    message_id       INTEGER,
    lift_ids         TEXT,
    created_at       TEXT    NOT NULL
);

-- Per-user bodyweight history. Used to compute the "true" load for
-- bodyweight-relative lifts (assisted pull-ups/dips give the assistance
-- amount, weighted pull-ups/dips give bodyweight + added kg). One row per
-- update; we read the most recent row (or the most recent on/before a
-- given lift's logged_at) when displaying true weights.
CREATE TABLE IF NOT EXISTS bodyweights (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    weight_kg    REAL    NOT NULL,
    recorded_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bodyweights_user
    ON bodyweights (guild_id, user_id, recorded_at);

-- Bodyweight is global-per-user (guild 0); the latest/history lookups query on
-- user_id alone, so give them an index that doesn't lead with guild_id.
CREATE INDEX IF NOT EXISTS idx_bodyweights_user_global
    ON bodyweights (user_id, recorded_at);

-- Source-message ids whose lifts were explicitly removed via undo. The
-- startup backfill consults this so it doesn't re-import a post the user
-- already told us to forget. Cleared automatically when the user edits the
-- message (treated as a correction worth re-parsing).
CREATE TABLE IF NOT EXISTS suppressed_messages (
    guild_id      INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    suppressed_at TEXT    NOT NULL,
    PRIMARY KEY (guild_id, message_id)
);

-- Linked Revo Fitness portal accounts. password_enc is a Fernet token; the
-- plaintext is never persisted. last_ticket_signature is a stable hash of
-- the most-recently-seen ticket-tally row so the attendance poller can tell
-- when a fresh check-in has happened. notify_guild_id / notify_channel_id
-- pin attendance announcements to a specific room (defaults to the channel
-- the user ran /revo_link from).
CREATE TABLE IF NOT EXISTS revo_account (
    user_id                INTEGER PRIMARY KEY,
    email                  TEXT    NOT NULL,
    password_enc           TEXT    NOT NULL,
    member_id              INTEGER,
    membership_level       INTEGER,
    favorite_club_id       INTEGER,
    last_ticket_signature  TEXT,
    last_streak_weeks      INTEGER,
    last_checkin_date      TEXT,
    notify_guild_id        INTEGER,
    notify_channel_id      INTEGER,
    linked_at              TEXT    NOT NULL,
    last_polled_at         TEXT
);

-- Bot-wide user nicknames. Any admin or the user themselves can assign a
-- friendly display name (e.g. "Cookie Monster", "Sean") that shows up instead
-- of the Discord username/mention in bot responses. Not guild-scoped — one
-- nickname per user across all servers the bot is in.
CREATE TABLE IF NOT EXISTS user_nicknames (
    user_id   INTEGER PRIMARY KEY,
    nickname  TEXT    NOT NULL,
    set_by    INTEGER NOT NULL,
    set_at    TEXT    NOT NULL
);

-- Presence tracking. ``presence_tracked_users`` is the owner-managed
-- allow-list of (guild, user) pairs whose Discord status transitions we
-- record. ``presence_events`` is an append-only log of those transitions
-- — one row per actual status change (we de-dupe consecutive duplicates
-- in the writer). ``status`` is one of: online, idle, dnd, offline.
CREATE TABLE IF NOT EXISTS presence_tracked_users (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    started_by  INTEGER NOT NULL,
    started_at  TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS presence_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    status    TEXT    NOT NULL,
    at        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_presence_events_user
    ON presence_events (guild_id, user_id, at);

-- Activity tracking. A user can run several games/apps at once (e.g. a game
-- plus a launcher), so each row snapshots the *full set* currently active:
-- ``activities`` is a JSON array of ``{"n": name, "i": image_url|null}`` in
-- Discord's reported order. ``activity``/``image_url`` mirror the primary
-- (first) entry for backward compatibility with the snapshot helpers and rows
-- written before concurrent tracking existed; ``activity`` is NULL and
-- ``activities`` is ``[]``/NULL when the user stops everything. We de-dupe
-- against the previous snapshot. ``image_url`` is the Discord rich-presence
-- large image when the activity exposes one (many plain "playing X" presences
-- don't) — used by the web dashboard to show real game art.
CREATE TABLE IF NOT EXISTS activity_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    activity   TEXT,
    image_url  TEXT,
    activities TEXT,
    at         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_activity_events_user
    ON activity_events (guild_id, user_id, at);

-- Voice-channel tracking. Append-only log of voice transitions. ``event`` is:
--   'join'  / 'leave' / 'move'  — channel-presence changes (the original kinds)
--   'mute_on'  / 'mute_off'     — the member's *muted* signal flipped
--   'deaf_on'  / 'deaf_off'     — the member's *deafened* signal flipped
-- Muted means self_mute OR server mute; deafened means self_deaf OR server
-- deaf. Discord's client auto-mutes your mic whenever you deafen, so a deafen
-- almost always coincides with a mute — we still record the two raw signals
-- independently so stats can tell muted-but-listening apart from deafened.
-- Mute/deafen rows are only written while the member is in a channel, and only
-- on an actual transition; app.voicetime reconstructs in-call / muted /
-- deafened durations from the interleaved stream (a 'join' re-anchors mute and
-- deafen state, and muted/deafened time is only counted while in a call).
-- ``channel_id`` / ``channel_name`` is the channel involved (the destination
-- for join/move, the channel left for leave, the current channel for a
-- mute/deafen toggle), snapshotted at write time so the dashboard needn't
-- resolve channels.
CREATE TABLE IF NOT EXISTS voice_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    event        TEXT    NOT NULL,
    channel_id   INTEGER,
    channel_name TEXT,
    at           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_voice_events_guild
    ON voice_events (guild_id, at);

-- voice_events_for() reads one member's history filtered by (guild_id,
-- user_id, at) and runs unbounded-lower carry-in probes per signal group.
-- The guild-only index above leads with guild_id/at, so those probes would
-- walk the guild's whole voice log backward filtering user_id in memory —
-- and every mute/deafen toggle is now logged, so the table grows fast. This
-- user-leading index lets each per-member query seek straight to the rows.
CREATE INDEX IF NOT EXISTS idx_voice_events_user
    ON voice_events (guild_id, user_id, at);

-- Message logging. When presence tracking is on, every message a tracked
-- user sends is appended here with full content so the web dashboard can show
-- a per-member message feed alongside presence/activity. ``channel_name`` is
-- snapshotted at write time so the dashboard needn't resolve channels, and the
-- unique index on (guild_id, message_id) makes re-dispatches idempotent.
CREATE TABLE IF NOT EXISTS message_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    channel_id   INTEGER,
    channel_name TEXT,
    message_id   INTEGER,
    content      TEXT,
    attachments  TEXT,
    at           TEXT    NOT NULL,
    edited_at    TEXT,
    deleted_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_message_log_user
    ON message_log (guild_id, user_id, at);

-- The dashboard channel view filters (guild_id, channel_id) ORDER BY at DESC;
-- without this it temp-sorts the whole guild's log on every page load.
CREATE INDEX IF NOT EXISTS idx_message_log_channel
    ON message_log (guild_id, channel_id, at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_message_log_msgid
    ON message_log (guild_id, message_id) WHERE message_id IS NOT NULL;

-- Operators can blacklist members via the dashboard. A blacklisted user can't
-- add anything to the bot (lifts, calories, protein, bodyweight, slash/prefix
-- commands), but their chat is still logged and kept — blacklisting deletes
-- nothing. ``reason`` is operator-supplied and shown publicly when the bot
-- announces the blacklisting in chat.
CREATE TABLE IF NOT EXISTS message_log_blacklist (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    reason    TEXT,
    added_by  TEXT,
    added_at  TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- Per-user auto un-timeout protection. When the AUTO_UNTIMEOUT master switch is
-- on, the bot only auto-removes timeouts for members listed here (added via the
-- dashboard's per-member Moderation toggle). Empty list => nobody is protected.
CREATE TABLE IF NOT EXISTS auto_untimeout (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    added_by  TEXT,
    added_at  TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- Per-day nutrition targets — the source of truth for what a user is aiming at
-- on any given date, and what ``calorie_goals``/``protein_goals`` below were
-- replaced by.
--
-- One row per (user, macro, scope, effective_from):
--   macro           'kcal' or 'protein_g'. The two trackers are independent
--                   everywhere else in the bot, so they get independent rows —
--                   turning calorie tracking off must not disturb a protein
--                   ceiling.
--   scope           which days the rule covers: 'default' (every day),
--                   'weekend' (Sat/Sun), and — reserved — 'weekday', 'dow:N',
--                   'date:YYYY-MM-DD'. Most specific match wins.
--   value           the target. NULL is meaningful: it says "this rule sets
--                   nothing", which is how an override gets cleared, and how
--                   tracking gets switched off, WITHOUT deleting the rows that
--                   already-lived-through days resolve against.
--   effective_from  a LOCAL YYYY-MM-DD; the rule applies from that day on.
--                   Editing a goal appends a row rather than overwriting one,
--                   so last week keeps scoring against last week's target. Rows
--                   dated in the future are ignored until they arrive.
--
-- Targets are per-user/global like the goal tables they replace; ``guild_id``
-- only records where a goal was last set, for DM attribution and audit.
-- app/targets.py owns the matching and priority rules.
CREATE TABLE IF NOT EXISTS nutrition_targets (
    user_id        INTEGER NOT NULL,
    guild_id       INTEGER NOT NULL,
    username       TEXT    NOT NULL,
    macro          TEXT    NOT NULL,
    scope          TEXT    NOT NULL,
    value          REAL,
    effective_from TEXT    NOT NULL,
    set_at         TEXT    NOT NULL,
    PRIMARY KEY (user_id, macro, scope, effective_from)
);

CREATE INDEX IF NOT EXISTS idx_nutrition_targets_user
    ON nutrition_targets (user_id, macro);

-- LEGACY (read-only since nutrition_targets landed). These held one all-week
-- target per user. ``_backfill_nutrition_targets`` copies them across as
-- 'default'-scope rules effective from the beginning of time, which is what
-- makes a pre-existing single goal keep applying seven days a week — to new
-- days and to every day already logged. Nothing writes them any more; they are
-- kept so a rollback to an older build still finds its data.
CREATE TABLE IF NOT EXISTS calorie_goals (
    guild_id          INTEGER NOT NULL,
    user_id           INTEGER NOT NULL,
    username          TEXT    NOT NULL,
    daily_target_kcal REAL    NOT NULL,
    set_at            TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- One row per logged intake. kcal is always kilocalories; ``raw`` keeps the
-- original user input (e.g. "3550kj") for display/debugging. ``message_id``
-- is set for chat-logged entries so the startup backfill can de-duplicate
-- (one calorie entry per source message); slash-command entries leave it NULL.
CREATE TABLE IF NOT EXISTS calorie_entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    username   TEXT    NOT NULL,
    kcal       REAL    NOT NULL,
    note       TEXT,
    raw        TEXT,
    message_id INTEGER,
    logged_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calorie_entries_user
    ON calorie_entries (guild_id, user_id, logged_at);

-- Calories are global-per-user (stored under guild 0), so the hot queries
-- (undo, day totals, streak-saver loop) filter on user_id alone and can't use
-- the guild-led index above without a full scan + temp sort.
CREATE INDEX IF NOT EXISTS idx_calorie_entries_user_global
    ON calorie_entries (user_id, logged_at);

-- Per-user saved foods: a name → calorie shortcut so typing "coffee" logs a
-- known amount. Scoped per (guild, user). ``name`` is stored normalized
-- (lowercased, whitespace-collapsed) for lookups; ``display`` keeps the
-- original casing for output.
CREATE TABLE IF NOT EXISTS calorie_foods (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    name      TEXT    NOT NULL,
    display   TEXT    NOT NULL,
    kcal      REAL    NOT NULL,
    protein_g REAL,
    set_at    TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id, name)
);

-- Alternate names for a saved food: "boneless wicked wings" → the food saved
-- as "boneless wicked wing". Per-user and global like the foods themselves, so
-- an alias set in one server works in every server and in DMs. ``alias`` and
-- ``name`` are both stored normalized. Deleting or renaming a food clears the
-- aliases pointing at it, so a shortcut can never resolve to nothing.
CREATE TABLE IF NOT EXISTS calorie_food_aliases (
    user_id INTEGER NOT NULL,
    alias   TEXT    NOT NULL,
    name    TEXT    NOT NULL,
    set_at  TEXT    NOT NULL,
    PRIMARY KEY (user_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_calorie_food_aliases_name
    ON calorie_food_aliases (user_id, name);

-- Saved meals: a named bundle of saved foods ("breakfast" = coffee + oats)
-- logged in one go. ``items`` is a JSON array of [servings, food_name] pairs
-- resolved against calorie_foods at log time, so editing a food updates every
-- meal that includes it. Per-user and global (shared across servers + DMs),
-- matching saved foods.
CREATE TABLE IF NOT EXISTS calorie_meals (
    user_id INTEGER NOT NULL,
    name    TEXT    NOT NULL,
    display TEXT    NOT NULL,
    items   TEXT    NOT NULL,
    set_at  TEXT    NOT NULL,
    PRIMARY KEY (user_id, name)
);

-- Streak-saver reminders: opt-in evening DM when nothing has been logged that
-- day. ``hour``/``minute`` are in DISPLAY_TIMEZONE; ``last_sent`` is the local
-- YYYY-MM-DD the last nudge went out (one per day max). Global per user.
CREATE TABLE IF NOT EXISTS calorie_reminder_prefs (
    user_id    INTEGER PRIMARY KEY,
    hour       INTEGER NOT NULL DEFAULT 20,
    minute     INTEGER NOT NULL DEFAULT 0,
    last_sent  TEXT,
    updated_at TEXT    NOT NULL
);

-- Personal bodyweight target (kg). Global per user like bodyweights — cutting
-- and bulking goals both make sense, so no direction is implied.
CREATE TABLE IF NOT EXISTS bodyweight_goals (
    user_id   INTEGER PRIMARY KEY,
    username  TEXT    NOT NULL,
    target_kg REAL    NOT NULL,
    set_at    TEXT    NOT NULL
);

-- Optional protein tracker (grams). ``protein_goals`` holds each user's daily
-- *ceiling* (the feature is about not overeating protein), and a row here is
-- what marks a user as protein-tracking. ``protein_entries`` is one row per
-- logged amount; ``message_id`` dedupes chat-logged entries on backfill re-scan.
CREATE TABLE IF NOT EXISTS protein_goals (
    guild_id       INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,
    username       TEXT    NOT NULL,
    daily_target_g REAL    NOT NULL,
    set_at         TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS protein_entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    username   TEXT    NOT NULL,
    grams      REAL    NOT NULL,
    note       TEXT,
    raw        TEXT,
    message_id INTEGER,
    logged_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_protein_entries_user
    ON protein_entries (guild_id, user_id, logged_at);

-- Protein is global-per-user too; same rationale as idx_calorie_entries_user_global.
CREATE INDEX IF NOT EXISTS idx_protein_entries_user_global
    ON protein_entries (user_id, logged_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_protein_entries_dedupe
    ON protein_entries (message_id) WHERE message_id IS NOT NULL;

-- Ties a user's protein ceiling to their latest bodyweight at ``grams_per_kg``
-- grams of protein per kilogram. A row here means every ``set_bodyweight``
-- re-derives the all-week protein target (round(weight_kg * grams_per_kg)) and
-- writes a fresh effective-dated ``nutrition_targets`` row, so the ceiling
-- follows the scale without the user re-running ``/protein setup``.
-- ``grams_per_kg`` is stored even though the UI only ever writes the 1.0 g/kg
-- floor today, so a future "tie me to 1.6 g/kg for my cut" is a value change,
-- not a schema migration. Global per user, matching bodyweights and
-- bodyweight_goals — one body, one link — so lookups are by user_id alone.
CREATE TABLE IF NOT EXISTS protein_bw_links (
    user_id      INTEGER PRIMARY KEY,
    grams_per_kg REAL    NOT NULL DEFAULT 1.0,
    username     TEXT,
    created_at   TEXT
);

-- Maps the bot's calorie-log reply to the entry it created, so a ❌ reaction on
-- that reply removes exactly that entry (per-entry undo, mirroring lifts).
-- original_message_id is the chat message that triggered the log — suppressed on
-- undo so a restart backfill doesn't re-import it.
CREATE TABLE IF NOT EXISTS calorie_reply_tracking (
    reply_message_id    INTEGER PRIMARY KEY,
    guild_id            INTEGER NOT NULL,
    user_id             INTEGER NOT NULL,
    target_user_id      INTEGER NOT NULL,
    calorie_id          INTEGER NOT NULL,
    original_message_id INTEGER,
    created_at          TEXT    NOT NULL
);

-- Enough about each posted nutrition reply to re-render its running totals
-- later. Every confirmation shows the day's total *as at that entry*, so
-- removing or editing an earlier entry silently falsifies every card posted
-- after it — they stay on screen contradicting /today and each other.
--
-- Deliberately a separate table from calorie_reply_tracking above: that one
-- decides what a ❌ deletes, and nothing about restating a total should be
-- able to reach the code that removes someone's data. ``headline`` and
-- ``footnote`` are the rendered top and bottom of a text reply, kept so the
-- middle can be replaced without re-deriving the parts that didn't change
-- (who it was logged for, whether it was backdated). Card replies carry their
-- own title and leave both NULL.
CREATE TABLE IF NOT EXISTS nutrition_reply_render (
    reply_message_id INTEGER PRIMARY KEY,
    channel_id       INTEGER NOT NULL,
    target_user_id   INTEGER NOT NULL,
    calorie_id       INTEGER NOT NULL DEFAULT 0,
    protein_id       INTEGER NOT NULL DEFAULT 0,
    headline         TEXT,
    footnote         TEXT,
    created_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nutrition_reply_render_user
    ON nutrition_reply_render (target_user_id);

-- Linked Strava accounts (see app/strava_client.py). access/refresh tokens are
-- Fernet-encrypted — the plaintext is never persisted. ``expires_at`` is epoch
-- seconds (Strava's own) so the refresh path can tell when the access token is
-- stale. ``athlete_id`` is Strava's numeric id; it's how an inbound webhook
-- event (which only carries owner_id) is routed back to the Discord user.
-- ``last_activity_id`` de-dupes repeated webhook deliveries for one activity.
CREATE TABLE IF NOT EXISTS strava_account (
    user_id            INTEGER PRIMARY KEY,
    athlete_id         INTEGER UNIQUE,
    access_token_enc   TEXT    NOT NULL,
    refresh_token_enc  TEXT    NOT NULL,
    expires_at         INTEGER NOT NULL,
    scope              TEXT,
    athlete_name       TEXT,
    last_activity_id   INTEGER,
    last_message_id    INTEGER,
    last_channel_id    INTEGER,
    linked_at          TEXT    NOT NULL
);

-- Pending OAuth handshakes: maps the opaque ``state`` we embed in the authorize
-- URL back to the Discord user who ran /strava_link, so the browser redirect
-- can be attributed to them. Rows are consumed on callback and swept by age.
CREATE TABLE IF NOT EXISTS strava_pending_auth (
    state      TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT    NOT NULL
);

-- Linked Hevy (hevyapp.com) accounts. Hevy uses a per-user API key (no OAuth),
-- stored Fernet-encrypted in ``api_key_enc`` — plaintext is never persisted.
-- ``guild_id`` is where polled workouts are imported as lifts and where the feed
-- embed is posted. ``hevy_imported`` records which Hevy workout ids have already
-- been imported so repeated polls never double-log.
CREATE TABLE IF NOT EXISTS hevy_account (
    user_id        INTEGER PRIMARY KEY,
    guild_id       INTEGER NOT NULL,
    api_key_enc    TEXT    NOT NULL,
    hevy_username  TEXT,
    last_synced_at TEXT,
    linked_at      TEXT    NOT NULL,
    backfilled_at  TEXT
);

CREATE TABLE IF NOT EXISTS hevy_imported (
    user_id     INTEGER NOT NULL,
    workout_id  TEXT    NOT NULL,
    imported_at TEXT    NOT NULL,
    PRIMARY KEY (user_id, workout_id)
);

-- Each member's own Home Assistant, set up with /setup_ha. The long-lived access
-- token is stored Fernet-encrypted in ``token_enc`` — plaintext is never
-- persisted, matching the Hevy/Strava/Revo convention.
--
-- Deliberately separate from ``ha_account`` below, which records *which sensors*
-- on that server are theirs. Two tables rather than two nullable columns because
-- the states are genuinely independent: a member can have a verified server with
-- no entities chosen yet (the normal state between /setup_ha and /ha_link), and
-- "" is a legitimate entity prefix (a single-person install has bare
-- sensor.weight), so it cannot double as "not chosen yet".
CREATE TABLE IF NOT EXISTS ha_server (
    user_id     INTEGER PRIMARY KEY,
    base_url    TEXT    NOT NULL,
    token_enc   TEXT    NOT NULL,
    ha_version  TEXT,
    location    TEXT,
    verified_at TEXT,
    linked_at   TEXT    NOT NULL
);

-- Members linked to their Home Assistant body-composition sensors. What a member
-- links is ``entity_prefix`` — the shared prefix of their entity ids
-- ("joshua_s" for sensor.joshua_s_weight and its siblings).
-- Storing the prefix rather than ten entity ids means a scale that starts
-- reporting a new metric is picked up on the next poll with no re-link.
-- ``weight_entity`` is the entity the prefix was derived from, kept so
-- /ha_status can name it and so an oddly-named weight sensor still resolves.
-- ``guild_id`` is where the weigh-in is logged and the alert is posted.
CREATE TABLE IF NOT EXISTS ha_account (
    user_id        INTEGER PRIMARY KEY,
    guild_id       INTEGER NOT NULL,
    entity_prefix  TEXT    NOT NULL,
    weight_entity  TEXT,
    friendly_name  TEXT,
    -- Vestigial: announcements used to have a per-member opt-out here. They are
    -- unconditional now, and nothing reads or writes this column anymore.
    alerts_enabled INTEGER NOT NULL DEFAULT 1,
    last_synced_at TEXT,
    linked_at      TEXT    NOT NULL,
    backfilled_at  TEXT,
    -- The last reading actually imported from Home Assistant. Needed because
    -- restarting Home Assistant re-creates every entity, which gives each one a
    -- FRESH last_changed while the value is unchanged. Since last_changed is the
    -- de-duplication key, without this a routine HA update would log and
    -- announce a duplicate weigh-in for every linked member. Comparing the value
    -- tells a restored state apart from a real one; see app/bot._ha_import_reading.
    last_reading_at TEXT,
    last_weight_kg  REAL
);

-- One row per imported weigh-in, keyed by the weight sensor's ``last_changed``
-- (see app/ha_client.reading_key). This is what makes the poll idempotent: a
-- restart or a re-link both recompute the same key and so cannot double-log a
-- weigh-in or re-post its alert.
CREATE TABLE IF NOT EXISTS ha_imported (
    user_id     INTEGER NOT NULL,
    reading_key TEXT    NOT NULL,
    imported_at TEXT    NOT NULL,
    PRIMARY KEY (user_id, reading_key)
);

-- Body-composition metrics beyond bodyweight (body fat %, muscle mass, BMI,
-- BMR, water %, ...). Deliberately long-and-narrow — one row per metric per
-- weigh-in — so a scale that reports a metric the bot has never seen needs a
-- registry entry in app/ha_client.METRICS and no schema migration. Bodyweight
-- itself is NOT duplicated here; it goes through ``set_bodyweight`` so it keeps
-- feeding TDEE, protein links, goals and the graphs.
-- The UNIQUE key makes re-importing the same weigh-in a no-op.
CREATE TABLE IF NOT EXISTS body_metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    metric      TEXT    NOT NULL,
    value       REAL    NOT NULL,
    unit        TEXT,
    source      TEXT    NOT NULL DEFAULT 'home_assistant',
    recorded_at TEXT    NOT NULL,
    UNIQUE (user_id, metric, recorded_at)
);

CREATE INDEX IF NOT EXISTS idx_body_metrics_user
    ON body_metrics (user_id, metric, recorded_at);

-- Weigh-in announcements the bot posted, so an ❌ on one undoes exactly that
-- import. Mirrors ``reply_tracking`` for chat-logged lifts, including the
-- claim-by-delete race guard. ``recorded_ats`` is a comma-separated list of the
-- weigh-in timestamps the message covers -- usually one, but a first-link
-- backfill summary stands for the whole batch, and a bad import is exactly the
-- case somebody wants to undo in one go.
CREATE TABLE IF NOT EXISTS ha_reply_tracking (
    reply_message_id INTEGER PRIMARY KEY,
    user_id          INTEGER NOT NULL,
    guild_id         INTEGER NOT NULL,
    recorded_ats     TEXT    NOT NULL,
    chart_message_id INTEGER,
    created_at       TEXT    NOT NULL
);

-- ---------------------------------------------------------------------------
-- Web dashboard support: a mirror of the guild's member/role state plus a
-- unified audit log. These are populated by the bot (members intent required)
-- on startup and from gateway events, and read/edited by app/webui.py.
-- ---------------------------------------------------------------------------

-- Friendly guild names for the dashboard's guild picker, kept fresh by the
-- member/role sync. Tiny by design — one row per guild the bot is in.
CREATE TABLE IF NOT EXISTS guild_meta (
    guild_id     INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL,
    member_count INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT    NOT NULL
);

-- Tiny global key/value store for bot-internal bookkeeping that isn't
-- per-guild (e.g. the hash of the last successfully-synced slash-command set,
-- so we don't re-sync — and risk Discord's rate limit — when nothing changed).
CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per role the bot can see, refreshed on startup and from the
-- guild_role_* gateway events. ``color`` is the integer RGB value Discord
-- gives us (0 = no colour). ``position`` orders roles like the Discord UI.
CREATE TABLE IF NOT EXISTS guild_roles (
    guild_id   INTEGER NOT NULL,
    role_id    INTEGER NOT NULL,
    name       TEXT    NOT NULL,
    color      INTEGER NOT NULL DEFAULT 0,
    position   INTEGER NOT NULL DEFAULT 0,
    managed    INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

-- Mirror of guild membership. ``present`` is 1 while the member is in the
-- guild, 0 once they leave (we keep the row so historical audit entries still
-- resolve to a name). ``display_name`` is the server nickname or username.
CREATE TABLE IF NOT EXISTS members (
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    username     TEXT    NOT NULL,
    display_name TEXT    NOT NULL,
    avatar       TEXT,
    is_bot       INTEGER NOT NULL DEFAULT 0,
    present      INTEGER NOT NULL DEFAULT 1,
    joined_at    TEXT,
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- Current role assignments — the (guild, user) -> role edges. Replaced
-- wholesale whenever a member's roles change so it always reflects "now".
CREATE TABLE IF NOT EXISTS member_roles (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    role_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, role_id)
);

CREATE INDEX IF NOT EXISTS idx_member_roles_role
    ON member_roles (guild_id, role_id);

-- Append-only audit trail. ``category`` is one of: role, member, data. ``actor``
-- is who performed the change (a Discord user, or the web user for dashboard
-- edits — actor_id is NULL/0 then and actor_name carries the label). ``subject``
-- is who/what the change is about. ``detail`` is a free-form human string.
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    at           TEXT    NOT NULL,
    category     TEXT    NOT NULL,
    action       TEXT    NOT NULL,
    actor_id     INTEGER,
    actor_name   TEXT,
    subject_id   INTEGER,
    subject_name TEXT,
    detail       TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_log_at
    ON audit_log (guild_id, at);

CREATE INDEX IF NOT EXISTS idx_audit_log_subject
    ON audit_log (guild_id, subject_id, at);

CREATE INDEX IF NOT EXISTS idx_audit_log_category
    ON audit_log (guild_id, category, at);

-- Per-user preferences for DM command context. When someone runs a command in
-- a DM with the bot there is no guild_id on the interaction, so we resolve one:
-- their stored ``default_guild_id`` (if they're still a member) wins, otherwise
-- the single server they share with the bot is used automatically. Set via the
-- ``/server`` command.
CREATE TABLE IF NOT EXISTS user_dm_prefs (
    user_id          INTEGER PRIMARY KEY,
    default_guild_id INTEGER,
    updated_at       TEXT    NOT NULL
);

-- Operator-editable configuration, one row per key in app/config.py's SPEC.
-- Read at boot by config.load(), which resolves environment > this table >
-- code default. Environment always wins, so a row here can exist without being
-- in effect; the dashboard shows that as a "Pinned by environment" chip.
--
-- ``value`` NULL means "explicitly cleared" and falls through to the code
-- default. Rows are NEVER deleted: that is what lets the one-time environment
-- seed be INSERT OR IGNORE and still never resurrect a value the operator
-- deliberately cleared.
--
-- ``encrypted`` rows hold a Fernet token under the key at <db dir>/.secret_key;
-- this layer never sees the plaintext, mirroring the convention already used by
-- revo_account.password_enc and strava_account's token columns.
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    encrypted  INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL,
    updated_by TEXT
);

-- Append-only change log powering the Settings tab's history panel and the
-- "revert last change" button. ``audit_log`` is deliberately NOT reused: its
-- guild_id is NOT NULL and every reader filters on it, so a global settings row
-- would be written and then be invisible everywhere.
--
-- Secret changes record only that they happened: both value columns are NULL
-- and ``redacted`` is 1, so the history can never be used to read a secret back
-- out after it has been masked in the API.
CREATE TABLE IF NOT EXISTS app_settings_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    rev       INTEGER NOT NULL,
    key       TEXT    NOT NULL,
    old_value TEXT,
    new_value TEXT,
    redacted  INTEGER NOT NULL DEFAULT 0,
    at        TEXT    NOT NULL,
    actor     TEXT
);

CREATE INDEX IF NOT EXISTS idx_app_settings_history_rev
    ON app_settings_history (rev DESC, id DESC);

-- Optional long-lived dashboard logins. The browser owns the random bearer
-- token; SQLite stores only its SHA-256 digest so a database read cannot be
-- turned directly into a valid login cookie. Rows are global because the
-- dashboard itself has one shared operator account, not per-guild accounts.
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_web_sessions_expiry
    ON web_sessions (expires_at);
"""


class _Keep:
    """Sentinel type for :data:`KEEP`."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<keep>"


#: "Leave this setting exactly as it is." Needed wherever ``None`` already means
#: something: clearing a weekend override is ``None``, whereas not mentioning it
#: at all is ``KEEP``.
KEEP = _Keep()


#: Upper bound on a bodyweight-derived protein target, mirroring the bot's
#: ``_MAX_PROTEIN_TARGET_G``. It lives here as a defensive backstop because
#: :meth:`Database.set_bodyweight` is the Discord-free choke point and can be
#: called directly (tests, future importers). In normal operation the bot's
#: bodyweight commands already reject weights above ``MAX_WEIGHT_KG`` (500 by
#: default) before ``set_bodyweight`` runs, so at the 1.0 g/kg ratio the derived
#: target can't reach this cap anyway — the check exists so a raised
#: ``MAX_WEIGHT_KG`` or a future ``grams_per_kg`` > 1 can't silently push an
#: absurd ceiling into ``nutrition_targets``.
_BW_LINK_MAX_TARGET_G = 500


def _normalize_iso(dt: datetime | None) -> str:
    """Always store timestamps as UTC ISO-8601, regardless of caller tz."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    """Inverse of :func:`_normalize_iso`, tolerating a naive or 'Z'-suffixed value.

    Returns None rather than raising, because the callers are matching stored
    timestamps against ones that arrived from Discord or Home Assistant and a
    malformed one should narrow the search, not break it.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class LiftRow:
    username: str
    equipment: str
    weight_kg: float
    bodyweight_add: bool
    logged_at: str


class Database:
    """Tiny SQLite wrapper.

    A single connection is held for the lifetime of the process (SQLite is
    happy with one writer + many readers when WAL mode is on). All access
    is serialised via a thread lock because discord.py occasionally calls
    blocking sync code from worker threads (e.g. autocomplete callbacks).
    """

    def __init__(self, path: str | Path, *, migrate: bool = True,
                 busy_timeout_ms: int = 0) -> None:
        """Open (and by default migrate) the database at ``path``.

        ``migrate=False`` is for the bot worker under the supervisor, which
        must not run ``executescript``/``_migrate`` concurrently with the
        supervisor that already did. It asserts the schema is present rather
        than assuming, so a mis-ordered future refactor fails loudly here
        instead of as "no such table" deep inside a command handler.

        ``busy_timeout_ms`` defaults to 0 -- today's behaviour, where a write
        that collides returns SQLITE_BUSY immediately. Only the two processes
        that genuinely share the file set it, so the 100-odd existing tests and
        ``scripts/cleanup_lift.py`` keep their current failure mode.
        """
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Gates audit logging of *data* mutations (lift/calorie/protein adds and
        # removals) made through normal bot operation. Left False during the
        # startup backfill so re-importing history doesn't flood the audit log;
        # bot.py flips it to True once the backfill settles. Web-dashboard edits
        # always audit explicitly via ``add_audit`` regardless of this flag.
        self.audit_live = False
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,  # we manage transactions explicitly
        )
        self._connection.row_factory = sqlite3.Row
        # WAL boosts concurrent reads while a writer is active. NORMAL sync
        # is the standard recommendation for WAL — durable enough for our
        # workload and noticeably faster than FULL.
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        if busy_timeout_ms > 0:
            # Mandatory once the supervisor and the bot share this file:
            # without it a BEGIN IMMEDIATE that collides with the other
            # process's write fails instantly, and every dashboard save landing
            # mid-write would 500.
            self._connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        if migrate:
            self._connection.executescript(SCHEMA)
            self._migrate()
        elif self._connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'app_settings'"
        ).fetchone() is None:
            raise RuntimeError(
                "database has not been migrated -- the supervisor must open it "
                "first. This process was started with migrate=False."
            )

    def _migrate(self) -> None:
        """Apply lightweight, idempotent schema migrations.

        Older databases were created without the ``reps`` column. ``ALTER
        TABLE ... ADD COLUMN`` is a no-op when the column already exists
        (we check pragma first to keep the operation truly idempotent).
        """
        with self._lock:
            cols = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(lifts)")
            }
            if "reps" not in cols:
                self._connection.execute("ALTER TABLE lifts ADD COLUMN reps INTEGER")
            reply_cols = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(reply_tracking)"
                )
            }
            if "target_user_id" not in reply_cols:
                self._connection.execute(
                    "ALTER TABLE reply_tracking ADD COLUMN target_user_id INTEGER"
                )
                self._connection.execute(
                    "UPDATE reply_tracking SET target_user_id = user_id "
                    "WHERE target_user_id IS NULL"
                )
            # Calorie chat-logging dedupe: older DBs created calorie_entries
            # without message_id. Add it, then build the partial unique index
            # here (it can't live in SCHEMA because executescript runs before
            # this ALTER on an upgrade). The index makes the backfill re-scan
            # idempotent — one calorie entry per source message.
            cal_cols = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(calorie_entries)"
                )
            }
            if cal_cols and "message_id" not in cal_cols:
                self._connection.execute(
                    "ALTER TABLE calorie_entries ADD COLUMN message_id INTEGER"
                )
            if cal_cols:
                self._connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "idx_calorie_entries_dedupe ON calorie_entries (message_id) "
                    "WHERE message_id IS NOT NULL"
                )
            # The attendance poller moved from a ticket-tally signature cursor to
            # a per-day calendar cursor (last_checkin_date, ISO YYYY-MM-DD). Older
            # DBs predate the column.
            revo_cols = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(revo_account)")
            }
            if revo_cols and "last_checkin_date" not in revo_cols:
                self._connection.execute(
                    "ALTER TABLE revo_account ADD COLUMN last_checkin_date TEXT"
                )
            # Strava edit-on-rename: older DBs created strava_account before the
            # posted-message bookkeeping columns existed.
            strava_cols = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(strava_account)"
                )
            }
            if strava_cols and "last_message_id" not in strava_cols:
                self._connection.execute(
                    "ALTER TABLE strava_account ADD COLUMN last_message_id INTEGER"
                )
            if strava_cols and "last_channel_id" not in strava_cols:
                self._connection.execute(
                    "ALTER TABLE strava_account ADD COLUMN last_channel_id INTEGER"
                )
            # Dashboard avatars: older member mirrors predate the avatar column.
            member_cols = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(members)")
            }
            if member_cols and "avatar" not in member_cols:
                self._connection.execute(
                    "ALTER TABLE members ADD COLUMN avatar TEXT"
                )
            # Saved foods can carry an optional protein value (grams/serving)
            # so logging the food logs protein too. Older DBs predate it.
            food_cols = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(calorie_foods)"
                )
            }
            if food_cols and "protein_g" not in food_cols:
                self._connection.execute(
                    "ALTER TABLE calorie_foods ADD COLUMN protein_g REAL"
                )
            # Game art for the activity feed: older activity logs predate it.
            act_cols = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(activity_events)"
                )
            }
            if act_cols and "image_url" not in act_cols:
                self._connection.execute(
                    "ALTER TABLE activity_events ADD COLUMN image_url TEXT"
                )
            # Concurrent-activity snapshots: rows written before this hold only
            # the single primary game in ``activity``; the reader treats a NULL
            # ``activities`` column as "[{primary}]" so old data still aggregates.
            if act_cols and "activities" not in act_cols:
                self._connection.execute(
                    "ALTER TABLE activity_events ADD COLUMN activities TEXT"
                )
            # One-time 50-workout backfill marker. Accounts linked before the
            # backfill existed have this NULL, so the next poll catches them up.
            hevy_cols = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(hevy_account)"
                )
            }
            if hevy_cols and "backfilled_at" not in hevy_cols:
                self._connection.execute(
                    "ALTER TABLE hevy_account ADD COLUMN backfilled_at TEXT"
                )
            msg_cols = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(message_log)"
                )
            }
            if msg_cols and "attachments" not in msg_cols:
                self._connection.execute(
                    "ALTER TABLE message_log ADD COLUMN attachments TEXT"
                )
            # Soft-delete marker: set when a logged message is deleted in Discord
            # so the dashboard can flag it without losing the content/media.
            if msg_cols and "deleted_at" not in msg_cols:
                self._connection.execute(
                    "ALTER TABLE message_log ADD COLUMN deleted_at TEXT"
                )
            # Records the most recent edit so an edited message shows as such.
            if msg_cols and "edited_at" not in msg_cols:
                self._connection.execute(
                    "ALTER TABLE message_log ADD COLUMN edited_at TEXT"
                )
            # Automatic graphs are attached once per Home Assistant import
            # batch. Every announcement in that batch points at the holder so
            # undoing an earlier reading can remove the now-stale attachment.
            ha_reply_cols = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(ha_reply_tracking)"
                )
            }
            if ha_reply_cols and "chart_message_id" not in ha_reply_cols:
                self._connection.execute(
                    "ALTER TABLE ha_reply_tracking "
                    "ADD COLUMN chart_message_id INTEGER"
                )
            # One-time: older Hevy imports forked a separate machine per
            # equipment qualifier ("Bench Press (Barbell)" -> "bench press
            # barbell"). Now that canonicalize() strips the parenthetical,
            # re-derive those rows' equipment from the original Hevy title kept
            # in ``raw`` so the history merges with the matching machine. Keyed
            # off ``raw`` (which still has the parens) — a plain re-canonicalize
            # can't recover it because the stored value already lost them.
            if self._connection.execute(
                "SELECT 1 FROM app_meta WHERE key = 'hevy_equip_recanon_v1'"
            ).fetchone() is None:
                from .aliases import canonicalize as _canon
                for r in self._connection.execute(
                    "SELECT id, raw FROM lifts WHERE raw LIKE 'hevy:%'"
                ).fetchall():
                    eq = _canon(r["raw"][5:])
                    if eq:
                        self._connection.execute(
                            "UPDATE lifts SET equipment = ? WHERE id = ?",
                            (eq, r["id"]),
                        )
                self._connection.execute(
                    "INSERT OR REPLACE INTO app_meta (key, value) "
                    "VALUES ('hevy_equip_recanon_v1', 'done')"
                )
            self._consolidate_global_goals()
            # Must follow consolidation: it guarantees one legacy row per user,
            # so the backfill has an unambiguous target to copy.
            self._backfill_nutrition_targets()
            self._recanonicalize_equipment()
            # Older Home Assistant builds could re-add body-composition rows
            # after their weigh-in had been explicitly deleted: the import
            # ledger correctly kept the weight suppressed, but a later
            # metrics-only pass wrote its siblings back.  Composition describes
            # one concrete weigh-in, so without the matching bodyweights row it
            # is an orphan and must not surface as somebody's "latest" reading.
            # The write path below now enforces the same invariant; this
            # idempotent cleanup repairs databases written before that fix.
            self._connection.execute(
                """
                DELETE FROM body_metrics
                 WHERE NOT EXISTS (
                       SELECT 1
                         FROM bodyweights bw
                        WHERE bw.user_id = body_metrics.user_id
                          AND bw.recorded_at = body_metrics.recorded_at
                 )
                """
            )

    def _backfill_nutrition_targets(self) -> None:
        """Copy legacy ``calorie_goals``/``protein_goals`` into ``nutrition_targets``.

        Each becomes a 'default'-scope rule effective from the beginning of
        time, so a user who only ever set one calorie and one protein goal keeps
        getting exactly that target seven days a week — on new days, and on
        every day they had already logged before this table existed.

        Deliberately unconditional rather than gated on an ``app_meta`` flag:
        the primary key makes ``INSERT OR IGNORE`` a no-op on re-run, and a flag
        set during the very first open of an *empty* database would skip the
        backfill forever if legacy rows showed up later (which is exactly what
        the older tests, and a rollback-then-roll-forward, do). Both source
        tables hold at most one row per tracking user, so the scan is trivial.

        A user who has since switched tracking off has a NULL-valued rule dated
        later than the epoch, and that rule still wins — re-inserting the old
        row here cannot silently re-enable them.
        """
        for table, column, macro in (
            ("calorie_goals", "daily_target_kcal", targets.MACRO_KCAL),
            ("protein_goals", "daily_target_g", targets.MACRO_PROTEIN),
        ):
            self._connection.execute(
                f"""
                INSERT OR IGNORE INTO nutrition_targets
                    (user_id, guild_id, username, macro, scope, value,
                     effective_from, set_at)
                SELECT user_id, guild_id, username, ?, ?, {column}, ?, set_at
                FROM {table}
                """,
                (macro, targets.SCOPE_DEFAULT, targets.BEGINNING_OF_TIME),
            )

    def _consolidate_global_goals(self) -> None:
        """Collapse calorie/protein/lift goals to one row per user.

        Tracking is per-user/global, but databases written before that change
        could hold a separate goal row per server. Logged *entries/lifts* are
        already read globally (by user_id), so no row migration is needed for
        those — only the goal rows (targets) need de-duplicating. We keep each
        user's most-recently-set row and drop the rest. Idempotent: a DB that
        already has one row per group is left untouched.
        """
        # Calorie/protein goals are one-per-user; lift goals are one per
        # (user, equipment). The bare-column-with-MAX() trick gives, per group,
        # the rowid of the latest set_at row to keep.
        for table, group in (
            ("calorie_goals", "user_id"),
            ("protein_goals", "user_id"),
            ("goals", "user_id, equipment"),
        ):
            self._connection.execute(
                f"DELETE FROM {table} WHERE rowid NOT IN ("
                f"  SELECT rowid FROM ("
                f"    SELECT rowid, MAX(set_at) FROM {table} GROUP BY {group}"
                f"  )"
                f")"
            )

    def _recanonicalize_equipment(self) -> None:
        """Re-run the alias table over every stored equipment label.

        Lets newly-added aliases (e.g. mapping ``angled leg press`` ->
        ``leg press``) retroactively merge old rows. Idempotent: rows that
        are already canonical are no-ops.
        """
        # Local import to avoid a circular import at module load.
        from .aliases import canonicalize

        conn = self._connection
        # Build the rename map from distinct equipment values across the
        # tables that store one. ``custom_aliases.canonical`` is also
        # rewritten so user-defined aliases stay pointed at the right name.
        sources = (
            ("lifts", "equipment"),
            ("goals", "equipment"),
            ("custom_aliases", "canonical"),
        )
        rename: dict[str, str] = {}
        for table, col in sources:
            for row in conn.execute(f"SELECT DISTINCT {col} AS v FROM {table}"):
                old = row["v"]
                if not old:
                    continue
                new = canonicalize(old)
                if new and new != old:
                    rename[old] = new
        if not rename:
            return

        conn.execute("BEGIN IMMEDIATE")
        try:
            for old, new in rename.items():
                # ``lifts`` has a unique index on (message_id, equipment).
                # Drop rows that would collide with an already-canonical
                # entry from the same source message before renaming.
                conn.execute(
                    """
                    DELETE FROM lifts
                    WHERE equipment = ?
                      AND message_id IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM lifts l2
                          WHERE l2.message_id = lifts.message_id
                            AND l2.equipment  = ?
                      )
                    """,
                    (old, new),
                )
                conn.execute(
                    "UPDATE lifts SET equipment = ? WHERE equipment = ?",
                    (new, old),
                )
                # ``goals`` PK is (guild_id, user_id, equipment). When
                # both old and new rows exist for the same user, bump the
                # surviving canonical row to the higher target_kg and
                # drop the obsolete one.
                conn.execute(
                    """
                    UPDATE goals
                       SET target_kg = MAX(target_kg, (
                           SELECT target_kg FROM goals g_old
                           WHERE g_old.guild_id  = goals.guild_id
                             AND g_old.user_id   = goals.user_id
                             AND g_old.equipment = ?
                       ))
                     WHERE equipment = ?
                       AND EXISTS (
                           SELECT 1 FROM goals g_old
                           WHERE g_old.guild_id  = goals.guild_id
                             AND g_old.user_id   = goals.user_id
                             AND g_old.equipment = ?
                       )
                    """,
                    (old, new, old),
                )
                conn.execute(
                    """
                    DELETE FROM goals
                     WHERE equipment = ?
                       AND EXISTS (
                           SELECT 1 FROM goals g_new
                           WHERE g_new.guild_id  = goals.guild_id
                             AND g_new.user_id   = goals.user_id
                             AND g_new.equipment = ?
                       )
                    """,
                    (old, new),
                )
                conn.execute(
                    "UPDATE goals SET equipment = ? WHERE equipment = ?",
                    (new, old),
                )
                conn.execute(
                    "UPDATE custom_aliases SET canonical = ? WHERE canonical = ?",
                    (new, old),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        with self._lock:
            try:
                self._connection.close()
            except sqlite3.Error:
                pass

    def backup_to(self, dest_path: str | Path) -> None:
        """Write a consistent snapshot of the database to ``dest_path``.

        Uses SQLite's online backup API, which is safe against the live WAL
        connection (readers/writers keep flowing; the copy is transactionally
        consistent). Writes to a ``.tmp`` sibling first and renames into place
        so a crash mid-copy never leaves a truncated file that looks like a
        valid backup.
        """
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        with self._lock:
            target = sqlite3.connect(str(tmp))
            try:
                self._connection.backup(target)
            finally:
                target.close()
        tmp.replace(dest)

    @staticmethod
    def verify_snapshot(path: str | Path) -> tuple[bool, str]:
        """Open a snapshot file read-only and confirm it's a usable backup.

        Runs ``PRAGMA integrity_check`` and reads a couple of core tables so a
        silently-corrupt snapshot is caught the night it happens rather than at
        restore time. Returns ``(ok, detail)`` — ``detail`` is ``"ok"`` on
        success or a short reason on failure. Never raises.
        """
        p = Path(path)
        try:
            conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            return False, f"open failed: {exc}"
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            result = (row[0] if row else "").strip().lower()
            if result != "ok":
                return False, f"integrity_check: {result or 'no result'}"
            # Sanity-read a core table so a header-only/truncated file is caught.
            conn.execute("SELECT COUNT(*) FROM lifts").fetchone()
            return True, "ok"
        except sqlite3.Error as exc:
            return False, f"read failed: {exc}"
        finally:
            conn.close()

    @contextmanager
    def _conn(self):
        """Yield the shared connection inside an immediate transaction.

        Using ``BEGIN IMMEDIATE`` means writers are serialised while readers
        keep flowing on the WAL. Commit on clean exit, rollback otherwise —
        this is what makes ``add_lifts`` (and friends) atomic across the
        whole batch instead of row-by-row.
        """
        with self._lock:
            conn = self._connection
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def add_lifts(
        self,
        guild_id: int,
        user_id: int,
        username: str,
        lifts: list,
        message_id: int | None = None,
        channel_id: int | None = None,
        logged_at: datetime | None = None,
        actor_id: int | None = None,
        actor_name: str | None = None,
    ) -> int:
        """Insert lifts. Returns the number of rows actually inserted
        (duplicates from the same message are ignored).

        The whole batch runs inside a single transaction (see ``_conn``),
        so a mid-batch failure won't leave half the lifts persisted.

        ``actor_id``/``actor_name`` identify *who logged it* when that differs
        from the target (proxy logging via ``@user bench 80kg``); they default
        to the target so ordinary self-logging is unchanged.
        """
        if not lifts:
            return 0
        act_id = actor_id if actor_id is not None else user_id
        act_name = actor_name if actor_name is not None else username
        ts = _normalize_iso(logged_at)
        inserted = 0
        with self._conn() as c:
            for lift in lifts:
                try:
                    c.execute(
                        """
                        INSERT INTO lifts
                        (guild_id, user_id, username, equipment, weight_kg,
                         bodyweight_add, message_id, channel_id, logged_at,
                         raw, reps)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            guild_id, user_id, username,
                            lift.equipment, lift.weight_kg,
                            1 if lift.bodyweight_add else 0,
                            message_id, channel_id, ts, lift.raw,
                            getattr(lift, "reps", None),
                        ),
                    )
                    inserted += 1
                    if self.audit_live:
                        self._audit(
                            c, guild_id, "data", "lift_add",
                            actor_id=act_id, actor_name=act_name,
                            subject_id=user_id, subject_name=username,
                            detail=f"{lift.equipment} {lift.weight_kg:g}kg",
                        )
                except sqlite3.IntegrityError:
                    # dedupe on (message_id, equipment) — silent skip is
                    # intentional so re-runs of /parse and backfills are
                    # safe to repeat.
                    continue
        return inserted

    def add_lifts_returning_ids(
        self,
        guild_id: int,
        user_id: int,
        username: str,
        lifts: list,
        message_id: int | None = None,
        channel_id: int | None = None,
        logged_at: datetime | None = None,
        actor_id: int | None = None,
        actor_name: str | None = None,
    ) -> list[int]:
        """Same as ``add_lifts`` but returns the row ids that were inserted.

        Used by ``/log``-style flows where there's no source message_id, so
        the reaction-undo path can target the exact rows we just created.
        ``actor_id``/``actor_name`` credit the logger on proxy entries (see
        ``add_lifts``); they default to the target.
        """
        if not lifts:
            return []
        act_id = actor_id if actor_id is not None else user_id
        act_name = actor_name if actor_name is not None else username
        ts = _normalize_iso(logged_at)
        ids: list[int] = []
        with self._conn() as c:
            for lift in lifts:
                try:
                    cur = c.execute(
                        """
                        INSERT INTO lifts
                        (guild_id, user_id, username, equipment, weight_kg,
                         bodyweight_add, message_id, channel_id, logged_at,
                         raw, reps)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            guild_id, user_id, username,
                            lift.equipment, lift.weight_kg,
                            1 if lift.bodyweight_add else 0,
                            message_id, channel_id, ts, lift.raw,
                            getattr(lift, "reps", None),
                        ),
                    )
                    if cur.lastrowid:
                        ids.append(int(cur.lastrowid))
                        if self.audit_live:
                            self._audit(
                                c, guild_id, "data", "lift_add",
                                actor_id=act_id, actor_name=act_name,
                                subject_id=user_id, subject_name=username,
                                detail=f"{lift.equipment} {lift.weight_kg:g}kg",
                            )
                except sqlite3.IntegrityError:
                    continue
        return ids

    def personal_bests(self, guild_id: int, user_id: int) -> list[sqlite3.Row]:
        # For each equipment, pick the row with the highest weight_kg, and
        # return the date that PR was set on (earliest date at that weight).
        # Lifts are global per-user, so bests span every server (``guild_id`` is
        # kept for signature compatibility but not filtered on).
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT l.equipment,
                       l.weight_kg    AS best,
                       l.bodyweight_add AS bw,
                       MIN(l.logged_at) AS set_on
                FROM lifts l
                JOIN (
                    SELECT equipment, MAX(weight_kg) AS mx
                    FROM lifts
                    WHERE user_id = ?
                    GROUP BY equipment
                ) m ON m.equipment = l.equipment AND m.mx = l.weight_kg
                WHERE l.user_id = ?
                GROUP BY l.equipment
                ORDER BY l.equipment
                """,
                (user_id, user_id),
            ))

    def leaderboard(self, guild_id: int, equipment: str) -> list[sqlite3.Row]:
        """Top lifts on one equipment for **this guild's current members**.

        Lifts are global per-user, so each member is ranked by their all-time
        best across every server — but the board itself stays per-community
        (only present members of ``guild_id`` appear), matched via the members
        mirror rather than where the lift happened to be logged."""
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT l.user_id,
                       l.username,
                       l.weight_kg       AS best,
                       l.bodyweight_add  AS bw,
                       MIN(l.logged_at)  AS set_on
                FROM lifts l
                JOIN (
                    SELECT l2.user_id, MAX(l2.weight_kg) AS mx
                    FROM lifts l2
                    JOIN members mem
                      ON mem.user_id = l2.user_id
                     AND mem.guild_id = ? AND mem.present = 1
                    WHERE l2.equipment = ?
                    GROUP BY l2.user_id
                ) m ON m.user_id = l.user_id AND m.mx = l.weight_kg
                WHERE l.equipment = ?
                GROUP BY l.user_id
                ORDER BY best DESC
                LIMIT 25
                """,
                (guild_id, equipment, equipment),
            ))

    def progress(
        self, guild_id: int, user_id: int, equipment: str
    ) -> list[sqlite3.Row]:
        """Best weight per calendar month for a user/equipment, plus the
        date within the month the best was achieved."""
        with self._conn() as c:
            return list(c.execute(
                """
                WITH ranked AS (
                    SELECT substr(logged_at, 1, 7) AS month,
                           weight_kg,
                           bodyweight_add,
                           logged_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY substr(logged_at, 1, 7)
                               ORDER BY weight_kg DESC, logged_at ASC, id ASC
                           ) AS rn
                    FROM lifts
                    WHERE user_id = ? AND equipment = ?
                )
                SELECT month,
                       weight_kg      AS best,
                       bodyweight_add AS bw,
                       logged_at      AS first_seen
                FROM ranked
                WHERE rn = 1
                ORDER BY month
                """,
                (user_id, equipment),
            ))

    def known_equipment(self, guild_id: int) -> list[str]:
        with self._conn() as c:
            return [r[0] for r in c.execute(
                "SELECT DISTINCT equipment FROM lifts WHERE guild_id = ? ORDER BY equipment",
                (guild_id,),
            )]

    def delete_equipment(
        self, guild_id: int, equipment: str,
        actor_id: int | None = None, actor_name: str | None = None,
    ) -> int:
        """Delete every row for a given equipment name in a guild. Returns
        the number of rows removed. Writes an ``equipment_purge`` audit row
        crediting ``actor_id``/``actor_name`` (the admin who ran /purge)."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM lifts WHERE guild_id = ? AND equipment = ?",
                (guild_id, equipment),
            )
            removed = cur.rowcount or 0
            self._audit(
                c, guild_id, "data", "equipment_purge",
                actor_id=actor_id, actor_name=actor_name,
                detail=f"purged '{equipment}' ({removed} rows, guild-wide)",
            )
            return removed

    def rename_equipment(
        self, guild_id: int, src: str, dst: str,
        user_id: int | None = None,
        actor_id: int | None = None, actor_name: str | None = None,
    ) -> int:
        """Re-label every row from equipment=src to equipment=dst. Returns
        the number of rows affected. The unique (message_id, equipment) index
        is respected: if the destination already exists for a given message,
        the duplicate source row is dropped instead of renamed.

        If ``user_id`` is provided, the rename is **global** for that user — it
        re-labels their rows in every server, since lifts are global per-user.
        A guild-wide rename (no user filter) stays scoped to ``guild_id`` and
        also repoints that guild's custom aliases whose canonical pointed at
        ``src`` (aliases are guild-scoped, not user-scoped).
        """
        # Per-user rename → global (by user_id); guild-wide → this guild only.
        if user_id is not None:
            scope_clause = "user_id = ?"
            scope_params: tuple[object, ...] = (user_id,)
        else:
            scope_clause = "guild_id = ?"
            scope_params = (guild_id,)
        with self._conn() as c:
            # Remove rows that would collide with the (message_id, equipment)
            # dedupe index after the rename. message_id is unique to one
            # message, so matching on it + user_id finds the collision without
            # needing a guild filter.
            c.execute(
                f"""
                DELETE FROM lifts
                WHERE {scope_clause} AND equipment = ?
                  AND message_id IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM lifts b
                      WHERE b.message_id = lifts.message_id
                        AND b.equipment = ?
                        AND b.user_id = lifts.user_id
                  )
                """,
                (*scope_params, src, dst),
            )
            cur = c.execute(
                f"UPDATE lifts SET equipment = ? WHERE {scope_clause} AND equipment = ?",
                (dst, *scope_params, src),
            )
            if user_id is None:
                # Repoint guild aliases so future parses land on the new
                # canonical instead of the old one.
                c.execute(
                    "UPDATE custom_aliases SET canonical = ? "
                    "WHERE guild_id = ? AND canonical = ?",
                    (dst, guild_id, src),
                )
            # Move/merge affected lift goals to match the rename. Scope mirrors
            # the lifts rename (per-user = global, guild-wide = this guild). The
            # inner writes key by (user, equipment) since goals are global —
            # exactly one row per user+equipment.
            goal_sql = (
                f"SELECT user_id, equipment, target_kg, bodyweight_add, set_at "
                f"FROM goals WHERE {scope_clause} AND equipment IN (?, ?)"
            )
            goals_by_user: dict[int, dict[str, sqlite3.Row]] = {}
            for row in c.execute(goal_sql, (*scope_params, src, dst)):
                goals_by_user.setdefault(row["user_id"], {})[row["equipment"]] = row
            for goal_user_id, goals in goals_by_user.items():
                src_goal = goals.get(src)
                if src_goal is None:
                    continue
                dst_goal = goals.get(dst)
                if dst_goal is None:
                    c.execute(
                        "UPDATE goals SET equipment = ? "
                        "WHERE user_id = ? AND equipment = ?",
                        (dst, goal_user_id, src),
                    )
                    continue
                if src_goal["target_kg"] > dst_goal["target_kg"]:
                    c.execute(
                        """
                        UPDATE goals
                        SET target_kg = ?, bodyweight_add = ?, set_at = ?
                        WHERE user_id = ? AND equipment = ?
                        """,
                        (
                            src_goal["target_kg"],
                            src_goal["bodyweight_add"],
                            src_goal["set_at"],
                            goal_user_id,
                            dst,
                        ),
                    )
                c.execute(
                    "DELETE FROM goals "
                    "WHERE user_id = ? AND equipment = ?",
                    (goal_user_id, src),
                )
            renamed = cur.rowcount or 0
            scope = "global (per-user)" if user_id is not None else "guild-wide"
            self._audit(
                c, guild_id, "data", "equipment_rename",
                actor_id=actor_id, actor_name=actor_name,
                subject_id=user_id,
                detail=f"renamed '{src}' -> '{dst}' ({renamed} rows, {scope})",
            )
            return renamed

    def count_equipment_rows(
        self, guild_id: int, equipment: str,
        user_id: int | None = None,
    ) -> int:
        """How many rows match equipment (optionally for one user). Used for
        rename previews / dry-runs. A per-user count is **global** (lifts are
        global per-user); a guild-wide count stays scoped to the guild — so each
        preview matches what the corresponding rename/purge will actually do."""
        if user_id is not None:
            sql = "SELECT COUNT(*) FROM lifts WHERE user_id = ? AND equipment = ?"
            params: list[object] = [user_id, equipment]
        else:
            sql = "SELECT COUNT(*) FROM lifts WHERE guild_id = ? AND equipment = ?"
            params = [guild_id, equipment]
        with self._conn() as c:
            row = c.execute(sql, params).fetchone()
            return int(row[0]) if row else 0

    def delete_entry(
        self,
        guild_id: int,
        equipment: str,
        date: str,
        user_id: int | None = None,
        actor_id: int | None = None,
        actor_name: str | None = None,
    ) -> int:
        """Delete entries matching equipment + YYYY-MM-DD date. A per-user
        delete is **global** (the user's matching rows in every server); a
        guild-wide delete (no user) stays scoped to the guild. Returns rows
        deleted. Writes an ``entry_delete`` audit row when live."""
        if user_id is not None:
            sql = (
                "DELETE FROM lifts WHERE user_id = ? AND equipment = ? "
                "AND substr(logged_at, 1, 10) = ?"
            )
            params: list[object] = [user_id, equipment, date]
        else:
            sql = (
                "DELETE FROM lifts WHERE guild_id = ? AND equipment = ? "
                "AND substr(logged_at, 1, 10) = ?"
            )
            params = [guild_id, equipment, date]
        with self._conn() as c:
            cur = c.execute(sql, params)
            removed = cur.rowcount or 0
            self._audit_data(
                c, guild_id, "entry_delete",
                actor_id=actor_id, actor_name=actor_name,
                subject_id=user_id,
                detail=f"deleted '{equipment}' on {date} ({removed} rows)",
            )
            return removed

    def delete_entry_between(
        self,
        guild_id: int,
        equipment: str,
        start_iso: str,
        end_iso: str,
        user_id: int | None = None,
        actor_id: int | None = None,
        actor_name: str | None = None,
    ) -> int:
        """Delete entries matching equipment inside a UTC timestamp range. A
        per-user delete is **global**; a guild-wide delete stays scoped to the
        guild. Writes an ``entry_delete`` audit row when live."""
        if user_id is not None:
            sql = (
                "DELETE FROM lifts WHERE user_id = ? AND equipment = ? "
                "AND logged_at >= ? AND logged_at < ?"
            )
            params: list[object] = [user_id, equipment, start_iso, end_iso]
        else:
            sql = (
                "DELETE FROM lifts WHERE guild_id = ? AND equipment = ? "
                "AND logged_at >= ? AND logged_at < ?"
            )
            params = [guild_id, equipment, start_iso, end_iso]
        with self._conn() as c:
            cur = c.execute(sql, params)
            removed = cur.rowcount or 0
            self._audit_data(
                c, guild_id, "entry_delete",
                actor_id=actor_id, actor_name=actor_name,
                subject_id=user_id,
                detail=f"deleted '{equipment}' {start_iso[:10]} ({removed} rows)",
            )
            return removed

    def _latest_lift(
        self,
        c: sqlite3.Connection,
        guild_id: int,
        user_id: int,
        equipment: str,
        start_iso: str | None = None,
        end_iso: str | None = None,
    ) -> sqlite3.Row | None:
        # Per-user edit helper → global (lifts are global per-user); ``guild_id``
        # is kept for signature compatibility but not filtered on.
        sql = (
            "SELECT id, equipment, weight_kg, bodyweight_add AS bw, reps, logged_at "
            "FROM lifts WHERE user_id = ? AND equipment = ?"
        )
        params: list[object] = [user_id, equipment]
        if start_iso is not None and end_iso is not None:
            sql += " AND logged_at >= ? AND logged_at < ?"
            params.extend([start_iso, end_iso])
        sql += " ORDER BY logged_at DESC, id DESC LIMIT 1"
        return c.execute(sql, params).fetchone()

    def update_latest_lift_weight(
        self,
        guild_id: int,
        user_id: int,
        equipment: str,
        weight_kg: float,
        bodyweight_add: bool,
        start_iso: str | None = None,
        end_iso: str | None = None,
    ) -> sqlite3.Row | None:
        """Update the latest matching row and return its previous values."""
        with self._conn() as c:
            row = self._latest_lift(
                c, guild_id, user_id, equipment, start_iso, end_iso,
            )
            if row is None:
                return None
            c.execute(
                """
                UPDATE lifts
                SET weight_kg = ?, bodyweight_add = ?
                WHERE id = ?
                """,
                (weight_kg, 1 if bodyweight_add else 0, row["id"]),
            )
            return row

    def swap_latest_lift_weights(
        self,
        guild_id: int,
        user_id: int,
        first_equipment: str,
        second_equipment: str,
        start_iso: str | None = None,
        end_iso: str | None = None,
    ) -> tuple[sqlite3.Row, sqlite3.Row] | None:
        """Swap weight/bodyweight/reps between two latest matching rows."""
        with self._conn() as c:
            first = self._latest_lift(
                c, guild_id, user_id, first_equipment, start_iso, end_iso,
            )
            second = self._latest_lift(
                c, guild_id, user_id, second_equipment, start_iso, end_iso,
            )
            if first is None or second is None or first["id"] == second["id"]:
                return None
            c.execute(
                """
                UPDATE lifts
                SET weight_kg = ?, bodyweight_add = ?, reps = ?
                WHERE id = ?
                """,
                (second["weight_kg"], second["bw"], second["reps"], first["id"]),
            )
            c.execute(
                """
                UPDATE lifts
                SET weight_kg = ?, bodyweight_add = ?, reps = ?
                WHERE id = ?
                """,
                (first["weight_kg"], first["bw"], first["reps"], second["id"]),
            )
            return first, second

    def history(
        self, guild_id: int, user_id: int, equipment: str, limit: int = 25
    ) -> list[sqlite3.Row]:
        """Chronological per-entry history for one user/equipment.

        The LIMIT takes the **most recent** rows, then flips them back into
        ascending order for display. Selecting ascending and limiting returned
        the *oldest* N instead, so a lifter with 71 entries on one exercise saw
        a "timeline" that stopped eight months ago and never showed today's.
        """
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT * FROM (
                    SELECT weight_kg, bodyweight_add AS bw, logged_at, reps
                    FROM lifts
                    WHERE guild_id = ? AND user_id = ? AND equipment = ?
                    ORDER BY logged_at DESC
                    LIMIT ?
                ) ORDER BY logged_at
                """,
                (guild_id, user_id, equipment, limit),
            ))

    def total_tonnage(
        self,
        guild_id: int,
        user_id: int,
        since_iso: str | None = None,
    ) -> tuple[float, int]:
        """Sum every logged ``weight_kg`` for a user (optionally since an
        ISO timestamp). Returns ``(total_kg, lift_count)``.

        Rep counts aren't multiplied in — the bot doesn't reliably capture
        sets, so this is a coarse "weight-on-the-bar across all entries"
        figure rather than true volume. Bodyweight-relative entries logged
        as 0kg (pure pull-ups) contribute nothing, which matches the way
        every other surface treats them.
        """
        # Global per-user: tonnage spans every server (``guild_id`` unused).
        params: list[object] = [user_id]
        sql = (
            "SELECT COALESCE(SUM(weight_kg), 0) AS total, COUNT(*) AS n "
            "FROM lifts WHERE user_id = ?"
        )
        if since_iso is not None:
            sql += " AND logged_at >= ?"
            params.append(since_iso)
        with self._conn() as c:
            row = c.execute(sql, params).fetchone()
            return float(row["total"] or 0.0), int(row["n"] or 0)

    def last_session_for_user(
        self, guild_id: int, user_id: int,
    ) -> tuple[str | None, list[sqlite3.Row]]:
        """Return ``(date_str, rows)`` for the most recent local-date on
        which the user logged anything, plus every lift on that date.

        Date bucketing uses the stored ISO timestamp's ``YYYY-MM-DD``
        prefix (which is UTC). Display layers can convert if they want to
        be precise about timezone boundaries; sessions still group sanely
        as long as the user trains in roughly the same TZ each time.
        """
        with self._conn() as c:
            row = c.execute(
                """
                SELECT substr(logged_at, 1, 10) AS d
                FROM lifts
                WHERE user_id = ?
                ORDER BY logged_at DESC, id DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if not row:
                return None, []
            day = row["d"]
            rows = list(c.execute(
                """
                SELECT equipment, weight_kg, bodyweight_add AS bw,
                       logged_at, reps
                FROM lifts
                WHERE user_id = ?
                  AND substr(logged_at, 1, 10) = ?
                ORDER BY logged_at ASC, id ASC
                """,
                (user_id, day),
            ))
            return day, rows

    def machine_history(
        self, guild_id: int, equipment: str, limit: int = 30
    ) -> list[sqlite3.Row]:
        """Chronological timeline of all users' entries on one equipment.

        Most-recent-N then re-sorted ascending, for the same reason as
        :meth:`history`. The limit is 30 rather than 50 because 50 rows of
        ``date — **name**: weight (+delta)`` measured 2 052 characters on real
        data — past Discord's 2 000-char message cap, so ``/machine`` on the
        most-logged exercise failed to send at all.
        """
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT * FROM (
                    SELECT username, weight_kg, bodyweight_add AS bw, logged_at
                    FROM lifts
                    WHERE guild_id = ? AND equipment = ?
                    ORDER BY logged_at DESC
                    LIMIT ?
                ) ORDER BY logged_at
                """,
                (guild_id, equipment, limit),
            ))

    def user_summary(self, guild_id: int, user_id: int) -> dict | None:
        """High-level counters for a user. Returns None if they have no lifts."""
        with self._conn() as c:
            row = c.execute(
                """
                SELECT COUNT(*)                          AS total_lifts,
                       COUNT(DISTINCT equipment)         AS unique_equip,
                       COUNT(DISTINCT message_id)        AS sessions,
                       MIN(logged_at)                    AS first_at,
                       MAX(logged_at)                    AS last_at
                FROM lifts
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if not row or row["total_lifts"] == 0:
                return None
            return dict(row)

    def user_top_prs(
        self, guild_id: int, user_id: int, limit: int = 5
    ) -> list[sqlite3.Row]:
        """Heaviest (by weight) personal bests for the user, ignoring BW-only."""
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT equipment,
                       MAX(weight_kg) AS best,
                       MAX(bodyweight_add) AS bw
                FROM lifts
                WHERE user_id = ?
                GROUP BY equipment
                ORDER BY best DESC
                LIMIT ?
                """,
                (user_id, limit),
            ))

    def user_most_trained(
        self, guild_id: int, user_id: int, limit: int = 5
    ) -> list[sqlite3.Row]:
        """Equipment the user logs most often."""
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT equipment, COUNT(*) AS n
                FROM lifts
                WHERE user_id = ?
                GROUP BY equipment
                ORDER BY n DESC, equipment
                LIMIT ?
                """,
                (user_id, limit),
            ))

    def user_biggest_gains(
        self, guild_id: int, user_id: int, limit: int = 5
    ) -> list[sqlite3.Row]:
        """Largest (latest - first) weight difference per equipment,
        restricted to equipment the user has logged at least twice."""
        with self._conn() as c:
            return list(c.execute(
                """
                WITH firsts AS (
                    SELECT equipment,
                           weight_kg AS first_w,
                           logged_at AS first_at,
                           ROW_NUMBER() OVER (PARTITION BY equipment
                                              ORDER BY logged_at ASC) AS rn
                    FROM lifts
                    WHERE user_id = ?
                ),
                lasts AS (
                    SELECT equipment,
                           weight_kg AS last_w,
                           logged_at AS last_at,
                           ROW_NUMBER() OVER (PARTITION BY equipment
                                              ORDER BY logged_at DESC) AS rn
                    FROM lifts
                    WHERE user_id = ?
                )
                SELECT f.equipment,
                       f.first_w, f.first_at,
                       l.last_w,  l.last_at,
                       (l.last_w - f.first_w) AS delta
                FROM firsts f JOIN lasts l USING (equipment)
                WHERE f.rn = 1 AND l.rn = 1
                  AND f.first_at <> l.last_at
                ORDER BY delta DESC
                LIMIT ?
                """,
                (user_id, user_id, limit),
            ))

    def recent_user_equipment(
        self, guild_id: int, user_id: int, limit: int = 25
    ) -> list[str]:
        with self._conn() as c:
            return [r[0] for r in c.execute(
                """
                SELECT equipment FROM lifts
                WHERE user_id = ?
                GROUP BY equipment
                ORDER BY MAX(logged_at) DESC
                LIMIT ?
                """,
                (user_id, limit),
            )]

    def previous_best(
        self, guild_id: int, user_id: int, equipment: str,
        before_id: int | None = None,
    ) -> float | None:
        """Highest weight the user had recorded for this equipment, optionally
        strictly before a given row id. Returns None if no prior entry.

        Global per-user: a PR is an all-time best across **every** server
        (``guild_id`` kept for signature compatibility but not filtered on)."""
        sql = (
            "SELECT MAX(weight_kg) AS best FROM lifts "
            "WHERE user_id = ? AND equipment = ?"
        )
        params: list[object] = [user_id, equipment]
        if before_id is not None:
            sql += " AND id < ?"
            params.append(before_id)
        with self._conn() as c:
            row = c.execute(sql, params).fetchone()
            return row["best"] if row and row["best"] is not None else None

    def user_recent(
        self, guild_id: int, user_id: int, limit: int = 10
    ) -> list[sqlite3.Row]:
        """Most recent N lift entries across all equipment for one user."""
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT id, equipment, weight_kg,
                       bodyweight_add AS bw, logged_at
                FROM lifts
                WHERE user_id = ?
                ORDER BY logged_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ))

    def user_all_lifts(
        self, guild_id: int, user_id: int,
    ) -> list[sqlite3.Row]:
        """Every lift entry across all equipment for one user.

        Returned in chronological order so callers can stream straight into
        a CSV / transcript without re-sorting. No ``LIMIT`` — used by
        bulk-export surfaces like ``/export_lifts``.
        """
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT id, equipment, weight_kg,
                       bodyweight_add AS bw, reps,
                       logged_at, message_id, channel_id, raw
                FROM lifts
                WHERE user_id = ?
                ORDER BY logged_at, id
                """,
                (user_id,),
            ))

    def user_rep_sets(
        self, guild_id: int, user_id: int,
    ) -> list[sqlite3.Row]:
        """Every set that carries a rep count (weight + reps), oldest-first.

        Used to estimate 1RM progression — strength gains often show up as more
        reps at the same weight before the top-set weight moves, which a raw
        weight timeline misses entirely.
        """
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT equipment, weight_kg, reps, logged_at
                FROM lifts
                WHERE user_id = ?
                  AND reps IS NOT NULL AND reps > 0 AND weight_kg > 0
                ORDER BY equipment, logged_at, id
                """,
                (user_id,),
            ))

    def user_latest_by_equipment(
        self, guild_id: int, user_id: int,
    ) -> list[sqlite3.Row]:
        """Latest row for each equipment a user has logged."""
        with self._conn() as c:
            return list(c.execute(
                """
                WITH ranked AS (
                    SELECT equipment, weight_kg,
                           bodyweight_add AS bw, logged_at,
                           COUNT(*) OVER (PARTITION BY equipment) AS n,
                           ROW_NUMBER() OVER (
                               PARTITION BY equipment
                               ORDER BY logged_at DESC, id DESC
                           ) AS rn
                    FROM lifts
                    WHERE user_id = ?
                )
                SELECT equipment, weight_kg, bw, logged_at, n
                FROM ranked
                WHERE rn = 1
                ORDER BY logged_at ASC, equipment
                """,
                (user_id,),
            ))

    def pop_last_for_user(
        self, guild_id: int, user_id: int,
        *, actor_id: int | None = None, actor_name: str | None = None,
    ) -> sqlite3.Row | None:
        """Delete the user's most recently logged row and return it. Returns
        None if they have no entries. Global per-user: undoes the latest lift
        from **any** server (``guild_id`` is kept only for the audit record)."""
        with self._conn() as c:
            row = c.execute(
                """
                SELECT id, equipment, weight_kg,
                       bodyweight_add AS bw, logged_at
                FROM lifts
                WHERE user_id = ?
                ORDER BY logged_at DESC, id DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            c.execute("DELETE FROM lifts WHERE id = ?", (row["id"],))
            self._audit_data(
                c, guild_id, "lift_undo", subject_id=user_id,
                actor_id=actor_id, actor_name=actor_name,
                detail=f"undid {row['equipment']} {row['weight_kg']:g}kg",
            )
            return row

    def pop_last_n_for_user(
        self, guild_id: int, user_id: int, n: int,
        *, actor_id: int | None = None, actor_name: str | None = None,
    ) -> list[sqlite3.Row]:
        """Delete the user's N most recent rows and return them, newest first.

        Used by ``/undo count:N``. Returns an empty list if the user has no
        entries; if N exceeds available rows, removes whatever exists.
        """
        if n <= 0:
            return []
        with self._conn() as c:
            rows = list(c.execute(
                """
                SELECT id, equipment, weight_kg,
                       bodyweight_add AS bw, logged_at, message_id
                FROM lifts
                WHERE user_id = ?
                ORDER BY logged_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, n),
            ))
            if not rows:
                return []
            placeholders = ",".join("?" for _ in rows)
            c.execute(
                f"DELETE FROM lifts WHERE id IN ({placeholders})",
                [r["id"] for r in rows],
            )
            detail = (
                f"undid {rows[0]['equipment']} {rows[0]['weight_kg']:g}kg"
                if len(rows) == 1
                else f"undid {len(rows)} lifts"
            )
            self._audit_data(
                c, guild_id, "lift_undo", subject_id=user_id,
                actor_id=actor_id, actor_name=actor_name, detail=detail,
            )
            return rows

    def server_totals(self, guild_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                """
                SELECT COUNT(*)                    AS total_lifts,
                       COUNT(DISTINCT user_id)     AS lifters,
                       COUNT(DISTINCT equipment)   AS unique_equip,
                       COUNT(DISTINCT message_id)  AS sessions,
                       MIN(logged_at)              AS first_at,
                       MAX(logged_at)              AS last_at
                FROM lifts
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
            if not row or row["total_lifts"] == 0:
                return None
            return dict(row)

    def server_top_users(
        self, guild_id: int, limit: int = 5
    ) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT username, COUNT(*) AS n,
                       COUNT(DISTINCT equipment) AS equip
                FROM lifts
                WHERE guild_id = ?
                GROUP BY user_id
                ORDER BY n DESC, username
                LIMIT ?
                """,
                (guild_id, limit),
            ))

    def server_popular_equipment(
        self, guild_id: int, limit: int = 5
    ) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT equipment, COUNT(*) AS n,
                       COUNT(DISTINCT user_id) AS users
                FROM lifts
                WHERE guild_id = ?
                GROUP BY equipment
                ORDER BY n DESC, equipment
                LIMIT ?
                """,
                (guild_id, limit),
            ))

    def daily_activity(
        self, guild_id: int, start_iso: str, end_iso: str, limit: int = 5,
    ) -> dict[str, object]:
        """Activity summary for rows logged in [start_iso, end_iso)."""
        with self._conn() as c:
            totals = c.execute(
                """
                SELECT COUNT(*)                    AS total_lifts,
                       COUNT(DISTINCT user_id)     AS lifters,
                       COUNT(DISTINCT equipment)   AS unique_equip,
                       COUNT(DISTINCT message_id)  AS sessions
                FROM lifts
                WHERE guild_id = ? AND logged_at >= ? AND logged_at < ?
                """,
                (guild_id, start_iso, end_iso),
            ).fetchone()
            top_users = list(c.execute(
                """
                SELECT username, COUNT(*) AS n,
                       COUNT(DISTINCT equipment) AS equip
                FROM lifts
                WHERE guild_id = ? AND logged_at >= ? AND logged_at < ?
                GROUP BY user_id
                ORDER BY n DESC, username
                LIMIT ?
                """,
                (guild_id, start_iso, end_iso, limit),
            ))
            popular_equipment = list(c.execute(
                """
                SELECT equipment, COUNT(*) AS n,
                       COUNT(DISTINCT user_id) AS users
                FROM lifts
                WHERE guild_id = ? AND logged_at >= ? AND logged_at < ?
                GROUP BY equipment
                ORDER BY n DESC, equipment
                LIMIT ?
                """,
                (guild_id, start_iso, end_iso, limit),
            ))
            prs = list(c.execute(
                """
                WITH period AS (
                    SELECT l.user_id, l.username, l.equipment, l.weight_kg,
                           l.bodyweight_add AS bw, l.logged_at, l.id,
                           (
                               SELECT MAX(prev.weight_kg)
                               FROM lifts prev
                               WHERE prev.guild_id = l.guild_id
                                 AND prev.user_id = l.user_id
                                 AND prev.equipment = l.equipment
                                 AND (
                                     prev.logged_at < l.logged_at
                                     OR (
                                         prev.logged_at = l.logged_at
                                         AND prev.id < l.id
                                     )
                                 )
                           ) AS prev_best
                    FROM lifts l
                    WHERE l.guild_id = ?
                      AND l.logged_at >= ?
                      AND l.logged_at < ?
                ),
                period_prs AS (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY user_id, equipment
                               ORDER BY weight_kg DESC, logged_at DESC, id DESC
                           ) AS rn
                    FROM period
                    WHERE weight_kg > 0
                      AND (prev_best IS NULL OR weight_kg > prev_best)
                )
                SELECT username, equipment, weight_kg, bw, logged_at, prev_best
                FROM period_prs
                WHERE rn = 1
                ORDER BY (weight_kg - COALESCE(prev_best, 0)) DESC,
                         weight_kg DESC,
                         logged_at ASC
                LIMIT ?
                """,
                (guild_id, start_iso, end_iso, limit),
            ))
            return {
                "totals": dict(totals) if totals else {
                    "total_lifts": 0,
                    "lifters": 0,
                    "unique_equip": 0,
                    "sessions": 0,
                },
                "top_users": top_users,
                "popular_equipment": popular_equipment,
                "prs": prs,
            }

    def export_rows(
        self, guild_id: int, user_id: int | None = None
    ) -> list[sqlite3.Row]:
        """All lift rows for a guild, optionally scoped to one user. Used
        by /export to produce a CSV."""
        sql = (
            "SELECT logged_at, username, equipment, weight_kg, "
            "bodyweight_add AS bw, raw "
            "FROM lifts WHERE guild_id = ?"
        )
        params: list[object] = [guild_id]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY logged_at"
        with self._conn() as c:
            return list(c.execute(sql, params))

    # ---- streaks ---------------------------------------------------------

    def user_log_dates(
        self, guild_id: int, user_id: int
    ) -> list[str]:
        """All distinct YYYY-MM-DD dates (UTC) on which the user logged at
        least one lift, ordered ascending.

        Note: this buckets by the *UTC* calendar date. For streaks and any
        other "what local day was this?" use, prefer
        :meth:`user_log_timestamps` and bucket in the display timezone —
        otherwise early-morning sessions in a +HH:MM timezone land on the
        previous day.
        """
        with self._conn() as c:
            return [r[0] for r in c.execute(
                """
                SELECT DISTINCT substr(logged_at, 1, 10)
                FROM lifts
                WHERE user_id = ?
                ORDER BY 1
                """,
                (user_id,),
            )]

    def user_log_timestamps(
        self, guild_id: int, user_id: int
    ) -> list[str]:
        """All raw ``logged_at`` timestamps (UTC ISO-8601) for the user,
        ordered ascending. Callers convert to local dates themselves so
        day/week bucketing respects the display timezone (incl. DST)."""
        with self._conn() as c:
            return [r[0] for r in c.execute(
                """
                SELECT logged_at
                FROM lifts
                WHERE user_id = ?
                ORDER BY logged_at
                """,
                (user_id,),
            )]

    # ---- goals -----------------------------------------------------------

    def goal_set(
        self, guild_id: int, user_id: int, equipment: str,
        target_kg: float, bodyweight_add: bool,
    ) -> None:
        """Set a lift goal. Goals are **per-user / global** like lifts — setting
        one consolidates to a single row per (user, equipment), so it applies in
        every server."""
        with self._conn() as c:
            c.execute(
                "DELETE FROM goals WHERE user_id = ? AND equipment = ? "
                "AND guild_id <> ?",
                (user_id, equipment, guild_id),
            )
            c.execute(
                """
                INSERT INTO goals (guild_id, user_id, equipment,
                                   target_kg, bodyweight_add, set_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (guild_id, user_id, equipment) DO UPDATE SET
                    target_kg = excluded.target_kg,
                    bodyweight_add = excluded.bodyweight_add,
                    set_at = excluded.set_at
                """,
                (
                    guild_id, user_id, equipment,
                    target_kg, 1 if bodyweight_add else 0,
                    _normalize_iso(None),
                ),
            )
            unit = "kg" + ("+BW" if bodyweight_add else "")
            self._audit_data(
                c, guild_id, "goal_set", subject_id=user_id,
                detail=f"goal {equipment} → {target_kg:g}{unit}",
            )

    def goal_remove(
        self, guild_id: int, user_id: int, equipment: str
    ) -> int:
        # Goals are global — remove the user's goal for this equipment
        # everywhere (``guild_id`` only labels the audit entry).
        with self._conn() as c:
            cur = c.execute(
                """
                DELETE FROM goals
                WHERE user_id = ? AND equipment = ?
                """,
                (user_id, equipment),
            )
            n = cur.rowcount or 0
            if n:
                self._audit_data(
                    c, guild_id, "goal_remove", subject_id=user_id,
                    detail=f"removed goal {equipment}",
                )
            return n

    def goal_get(
        self, guild_id: int, user_id: int, equipment: str
    ) -> sqlite3.Row | None:
        """A lift goal, resolved **per-user** so it applies in every server
        (prefers the current guild's row, else the most recent)."""
        with self._conn() as c:
            return c.execute(
                """
                SELECT equipment, target_kg, bodyweight_add AS bw, set_at
                FROM goals
                WHERE user_id = ? AND equipment = ?
                ORDER BY (guild_id = ?) DESC, set_at DESC LIMIT 1
                """,
                (user_id, equipment, guild_id),
            ).fetchone()

    def goal_list(
        self, guild_id: int, user_id: int
    ) -> list[sqlite3.Row]:
        """Each goal joined with the user's current (global) best on that
        equipment. Goals and bests are both per-user/global."""
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT g.equipment, g.target_kg,
                       g.bodyweight_add AS bw,
                       g.set_at,
                       COALESCE(
                           (SELECT MAX(weight_kg) FROM lifts l
                            WHERE l.user_id  = g.user_id
                              AND l.equipment = g.equipment),
                           0
                       ) AS current_best
                FROM goals g
                WHERE g.user_id = ?
                ORDER BY g.equipment
                """,
                (user_id,),
            ))

    # ---- custom aliases --------------------------------------------------

    def alias_set(
        self, guild_id: int, alias_normalized: str, canonical: str,
        added_by: int,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO custom_aliases
                    (guild_id, alias_normalized, canonical,
                     added_by, added_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (guild_id, alias_normalized) DO UPDATE SET
                    canonical = excluded.canonical,
                    added_by  = excluded.added_by,
                    added_at  = excluded.added_at
                """,
                (
                    guild_id, alias_normalized, canonical,
                    added_by, _normalize_iso(None),
                ),
            )

    def alias_remove(
        self, guild_id: int, alias_normalized: str
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """
                DELETE FROM custom_aliases
                WHERE guild_id = ? AND alias_normalized = ?
                """,
                (guild_id, alias_normalized),
            )
            return cur.rowcount or 0

    def alias_list(self, guild_id: int) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT alias_normalized, canonical, added_at
                FROM custom_aliases
                WHERE guild_id = ?
                ORDER BY canonical, alias_normalized
                """,
                (guild_id,),
            ))

    def alias_resolve(
        self, guild_id: int, alias_normalized: str
    ) -> str | None:
        with self._conn() as c:
            row = c.execute(
                """
                SELECT canonical FROM custom_aliases
                WHERE guild_id = ? AND alias_normalized = ?
                """,
                (guild_id, alias_normalized),
            ).fetchone()
            return row["canonical"] if row else None

    # ---- reaction-undo bookkeeping --------------------------------------

    def track_reply(
        self, reply_message_id: int, guild_id: int, user_id: int,
        message_id: int | None, lift_ids: list[int] | None,
        target_user_id: int | None = None,
    ) -> None:
        target_id = user_id if target_user_id is None else target_user_id
        ids_str = ",".join(str(i) for i in (lift_ids or [])) or None
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO reply_tracking
                    (reply_message_id, guild_id, user_id, target_user_id,
                     message_id, lift_ids, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reply_message_id, guild_id, user_id, target_id,
                    message_id, ids_str,
                    _normalize_iso(None),
                ),
            )

    def get_reply(
        self, reply_message_id: int
    ) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                """
                SELECT reply_message_id, guild_id, user_id,
                       COALESCE(target_user_id, user_id) AS target_user_id,
                       message_id, lift_ids, created_at
                FROM reply_tracking
                WHERE reply_message_id = ?
                """,
                (reply_message_id,),
            ).fetchone()

    def delete_reply(self, reply_message_id: int) -> int:
        """Delete a reply-tracking row. Returns the rowcount so callers can
        race-protect themselves: if two ❌ reactions land at once, only the
        first delete returns 1 and the second sees 0."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM reply_tracking WHERE reply_message_id = ?",
                (reply_message_id,),
            )
            return cur.rowcount or 0

    # ---- backfill suppression ------------------------------------------

    def suppress_message(self, guild_id: int, message_id: int) -> None:
        """Mark a source message as 'do not re-import' for backfill.

        Called by the undo paths so a restart doesn't resurrect lifts the
        user just removed. Idempotent.
        """
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO suppressed_messages
                    (guild_id, message_id, suppressed_at)
                VALUES (?, ?, ?)
                """,
                (guild_id, message_id, _normalize_iso(None)),
            )

    def unsuppress_message(self, guild_id: int, message_id: int) -> int:
        """Clear a backfill-suppression row. Used when the source message
        is edited so a corrected post can flow through the normal pipeline
        again."""
        with self._conn() as c:
            cur = c.execute(
                """
                DELETE FROM suppressed_messages
                WHERE guild_id = ? AND message_id = ?
                """,
                (guild_id, message_id),
            )
            return cur.rowcount or 0

    def is_message_suppressed(self, guild_id: int, message_id: int) -> bool:
        with self._conn() as c:
            row = c.execute(
                """
                SELECT 1 FROM suppressed_messages
                WHERE guild_id = ? AND message_id = ?
                """,
                (guild_id, message_id),
            ).fetchone()
            return row is not None

    def retarget_replies_for_message(
        self, guild_id: int, message_id: int, target_user_id: int,
    ) -> int:
        """Point tracked replies for a source message at the current lifter.

        Used when a user edits a message to add, remove, or change a leading
        @mention. Without this, reaction undo could remain scoped to the old
        lifter even though the stored rows moved to the new one.
        """
        with self._conn() as c:
            cur = c.execute(
                """
                UPDATE reply_tracking
                SET target_user_id = ?
                WHERE guild_id = ? AND message_id = ?
                """,
                (target_user_id, guild_id, message_id),
            )
            return cur.rowcount or 0

    def delete_lifts_for_message(
        self, guild_id: int, user_id: int, message_id: int,
        *, actor_id: int | None = None, actor_name: str | None = None,
    ) -> int:
        """Used by reaction-undo: remove every row the bot stored for a
        specific gym post, scoped to that user so another member can't
        retroactively affect someone else's history."""
        with self._conn() as c:
            cur = c.execute(
                """
                DELETE FROM lifts
                WHERE guild_id = ? AND user_id = ? AND message_id = ?
                """,
                (guild_id, user_id, message_id),
            )
            n = cur.rowcount or 0
            if n:
                self._audit_data(
                    c, guild_id, "lift_undo", subject_id=user_id,
                    actor_id=actor_id, actor_name=actor_name,
                    detail=f"undid {n} lift{'s' if n != 1 else ''}",
                )
            return n

    def delete_lifts_for_message_any_user(
        self, guild_id: int, message_id: int
    ) -> int:
        """Remove every lift row tied to a specific source message,
        regardless of who logged it. Used by the admin retroactive cleanup
        path where we already trust the signal (the bot's own undo
        footer)."""
        with self._conn() as c:
            cur = c.execute(
                """
                DELETE FROM lifts
                WHERE guild_id = ? AND message_id = ?
                """,
                (guild_id, message_id),
            )
            return cur.rowcount or 0

    def count_lifts_for_message(
        self, guild_id: int, message_id: int
    ) -> int:
        """Return how many lift rows are currently stored for a given
        source message. Used by the dry-run preview of the cleanup
        command."""
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM lifts "
                "WHERE guild_id = ? AND message_id = ?",
                (guild_id, message_id),
            ).fetchone()
            return int(row["n"] if row else 0)

    def delete_lifts_by_ids(
        self, guild_id: int, user_id: int | None, ids: list[int],
        *, actor_id: int | None = None, actor_name: str | None = None,
    ) -> int:
        """Delete specific lift rows by id. Scoped to (guild_id, user_id)
        for safety when a user id is supplied so a stale reply record can't
        nuke someone else's data."""
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        user_clause = " AND user_id = ?" if user_id is not None else ""
        user_params: list[object] = [user_id] if user_id is not None else []
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM lifts "
                f"WHERE guild_id = ?{user_clause} AND id IN ({placeholders})",
                [guild_id, *user_params, *ids],
            )
            n = cur.rowcount or 0
            if n:
                self._audit_data(
                    c, guild_id, "lift_undo", subject_id=user_id,
                    actor_id=actor_id, actor_name=actor_name,
                    detail=f"undid {n} lift{'s' if n != 1 else ''}",
                )
            return n

    # ------------------------------------------------------------------
    # Revo Fitness portal linking (see app/revo_client.py)
    # ------------------------------------------------------------------

    def link_revo_account(
        self,
        user_id: int,
        email: str,
        password_enc: str,
        member_id: int | None,
        membership_level: int | None,
        favorite_club_id: int | None,
        notify_guild_id: int | None,
        notify_channel_id: int | None,
    ) -> None:
        """Insert or replace a user's Revo credentials.

        ``password_enc`` must already be a Fernet token — this layer never
        sees the plaintext password. Uses INSERT OR REPLACE so re-running
        ``/revo_link`` cleanly updates an existing link (and resets the
        polling cursor, which is the expected behaviour when someone
        re-authenticates).
        """
        ts = _normalize_iso(None)
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO revo_account (
                    user_id, email, password_enc, member_id, membership_level,
                    favorite_club_id, last_ticket_signature, last_streak_weeks,
                    notify_guild_id, notify_channel_id, linked_at, last_polled_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, NULL)
                """,
                (
                    user_id, email, password_enc, member_id, membership_level,
                    favorite_club_id, notify_guild_id, notify_channel_id, ts,
                ),
            )

    def unlink_revo_account(self, user_id: int) -> bool:
        """Remove a user's Revo credentials. Returns True if a row existed."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM revo_account WHERE user_id = ?", (user_id,)
            )
            return (cur.rowcount or 0) > 0

    def get_revo_account(self, user_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM revo_account WHERE user_id = ?", (user_id,)
            ).fetchone()

    def list_revo_accounts(self) -> list[sqlite3.Row]:
        """All linked accounts. Used by the attendance poller."""
        with self._conn() as c:
            return list(c.execute("SELECT * FROM revo_account"))

    # ------------------------------------------------------------------
    # Strava account linking (see app/strava_client.py)
    # ------------------------------------------------------------------

    def create_strava_pending(self, state: str, user_id: int) -> None:
        """Record a pending OAuth handshake so the redirect can be attributed.

        Also opportunistically sweeps handshakes older than an hour — they're
        abandoned link attempts the user never completed in the browser.
        """
        with self._conn() as c:
            c.execute(
                "DELETE FROM strava_pending_auth "
                "WHERE created_at < datetime('now', '-1 hour')"
            )
            c.execute(
                """
                INSERT OR REPLACE INTO strava_pending_auth
                    (state, user_id, created_at)
                VALUES (?, ?, ?)
                """,
                (state, user_id, _normalize_iso(None)),
            )

    def pop_strava_pending(self, state: str) -> int | None:
        """Consume a pending handshake, returning the Discord user id or None."""
        with self._conn() as c:
            row = c.execute(
                "SELECT user_id FROM strava_pending_auth WHERE state = ?",
                (state,),
            ).fetchone()
            if row is None:
                return None
            c.execute(
                "DELETE FROM strava_pending_auth WHERE state = ?", (state,)
            )
            return int(row["user_id"])

    def link_strava_account(
        self,
        user_id: int,
        athlete_id: int | None,
        access_token_enc: str,
        refresh_token_enc: str,
        expires_at: int,
        scope: str | None,
        athlete_name: str | None,
    ) -> None:
        """Insert or replace a user's encrypted Strava tokens.

        Both token columns must already be Fernet tokens — this layer never
        sees plaintext. Re-linking (re-running ``/strava_link``) cleanly
        overwrites the previous link.
        """
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO strava_account (
                    user_id, athlete_id, access_token_enc, refresh_token_enc,
                    expires_at, scope, athlete_name, last_activity_id, linked_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?,
                    (SELECT last_activity_id FROM strava_account WHERE user_id = ?),
                    ?
                )
                """,
                (
                    user_id, athlete_id, access_token_enc, refresh_token_enc,
                    expires_at, scope, athlete_name, user_id, _normalize_iso(None),
                ),
            )

    def update_strava_tokens(
        self,
        user_id: int,
        access_token_enc: str,
        refresh_token_enc: str,
        expires_at: int,
    ) -> None:
        """Persist rotated tokens after a refresh."""
        with self._conn() as c:
            c.execute(
                """
                UPDATE strava_account
                   SET access_token_enc  = ?,
                       refresh_token_enc  = ?,
                       expires_at         = ?
                 WHERE user_id = ?
                """,
                (access_token_enc, refresh_token_enc, expires_at, user_id),
            )

    def update_strava_last_activity(
        self,
        user_id: int,
        activity_id: int,
        message_id: int | None = None,
        channel_id: int | None = None,
    ) -> None:
        """Advance the de-dupe cursor to the most recently announced activity.

        When ``message_id`` is given, also records where the announcement was
        posted so a later rename/delete webhook can edit or remove it.
        """
        with self._conn() as c:
            c.execute(
                """
                UPDATE strava_account
                   SET last_activity_id = ?,
                       last_message_id  = ?,
                       last_channel_id  = ?
                 WHERE user_id = ?
                """,
                (activity_id, message_id, channel_id, user_id),
            )

    def unlink_strava_account(self, user_id: int) -> bool:
        """Remove a user's Strava link. Returns True if a row existed."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM strava_account WHERE user_id = ?", (user_id,)
            )
            return (cur.rowcount or 0) > 0

    def get_strava_account(self, user_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM strava_account WHERE user_id = ?", (user_id,)
            ).fetchone()

    def get_strava_account_by_athlete(
        self, athlete_id: int
    ) -> sqlite3.Row | None:
        """Look up a link by Strava athlete id — the only id a webhook carries."""
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM strava_account WHERE athlete_id = ?", (athlete_id,)
            ).fetchone()

    def list_strava_accounts(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute("SELECT * FROM strava_account"))

    # ------------------------------------------------------------------
    # Hevy (hevyapp.com) linked accounts
    # ------------------------------------------------------------------

    def hevy_link(
        self, user_id: int, guild_id: int, api_key_enc: str,
        hevy_username: str | None = None,
    ) -> None:
        """Link (or re-link) a Hevy account. ``api_key_enc`` must already be a
        Fernet token — this layer never sees the plaintext key."""
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO hevy_account
                    (user_id, guild_id, api_key_enc, hevy_username,
                     last_synced_at, linked_at)
                VALUES (?, ?, ?, ?, NULL, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    guild_id      = excluded.guild_id,
                    api_key_enc   = excluded.api_key_enc,
                    hevy_username = excluded.hevy_username,
                    linked_at     = excluded.linked_at
                """,
                (user_id, guild_id, api_key_enc, hevy_username,
                 _normalize_iso(None)),
            )

    def hevy_get(self, user_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM hevy_account WHERE user_id = ?", (user_id,)
            ).fetchone()

    def list_hevy_accounts(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute("SELECT * FROM hevy_account"))

    def hevy_unlink(self, user_id: int) -> bool:
        """Remove a linked Hevy account and its import history. Returns True if a
        row existed."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM hevy_account WHERE user_id = ?", (user_id,)
            )
            c.execute(
                "DELETE FROM hevy_imported WHERE user_id = ?", (user_id,)
            )
            return (cur.rowcount or 0) > 0

    def hevy_mark_synced(self, user_id: int, at: datetime | None = None) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE hevy_account SET last_synced_at = ? WHERE user_id = ?",
                (_normalize_iso(at), user_id),
            )

    def hevy_mark_backfilled(
        self, user_id: int, at: datetime | None = None,
    ) -> None:
        """Record that this account has had its one-time 50-workout history
        backfill, so routine polls stop re-fetching the full history."""
        with self._conn() as c:
            c.execute(
                "UPDATE hevy_account SET backfilled_at = ? WHERE user_id = ?",
                (_normalize_iso(at), user_id),
            )

    def hevy_workout_imported(self, user_id: int, workout_id: str) -> bool:
        """True if ``workout_id`` has already been imported for this user."""
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM hevy_imported WHERE user_id = ? AND workout_id = ?",
                (user_id, str(workout_id)),
            ).fetchone() is not None

    def hevy_mark_workout(self, user_id: int, workout_id: str) -> bool:
        """Record ``workout_id`` as imported. Returns True if newly recorded
        (False if it was already present), so callers can skip duplicates."""
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO hevy_imported "
                "(user_id, workout_id, imported_at) VALUES (?, ?, ?)",
                (user_id, str(workout_id), _normalize_iso(None)),
            )
            return (cur.rowcount or 0) > 0

    # ------------------------------------------------------------------
    # Home Assistant linked entities + body-composition metrics
    # ------------------------------------------------------------------

    def ha_server_set(
        self, user_id: int, base_url: str, token_enc: str,
        ha_version: str | None = None, location: str | None = None,
    ) -> None:
        """Store (or replace) a member's Home Assistant credential.

        ``token_enc`` must already be a Fernet token — this layer never sees the
        plaintext, matching :meth:`hevy_link`. Re-running /setup_ha against the
        same server replaces the credential without touching which entities the
        member has linked, so rotating a token doesn't cost them their setup."""
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO ha_server
                    (user_id, base_url, token_enc, ha_version, location,
                     verified_at, linked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    base_url    = excluded.base_url,
                    token_enc   = excluded.token_enc,
                    ha_version  = excluded.ha_version,
                    location    = excluded.location,
                    verified_at = excluded.verified_at
                """,
                (user_id, base_url, token_enc, ha_version, location,
                 _normalize_iso(None), _normalize_iso(None)),
            )

    def ha_server_get(self, user_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM ha_server WHERE user_id = ?", (user_id,)
            ).fetchone()

    def ha_server_forget(self, user_id: int) -> bool:
        """Remove a member's stored credential. Returns True if one existed."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM ha_server WHERE user_id = ?", (user_id,)
            )
            return (cur.rowcount or 0) > 0

    def list_ha_synced(self) -> list[sqlite3.Row]:
        """Every member who has BOTH a credential and chosen entities.

        The poll's work list. A member with a server but no entities yet, or
        entities but no credential, is not pollable and is skipped here rather
        than being filtered in the loop."""
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT a.*, s.base_url, s.token_enc
                  FROM ha_account a
                  JOIN ha_server  s ON s.user_id = a.user_id
                """
            ))

    def count_ha_servers(self) -> int:
        with self._conn() as c:
            row = c.execute("SELECT COUNT(*) AS n FROM ha_server").fetchone()
            return int(row["n"]) if row else 0

    def ha_link(
        self, user_id: int, guild_id: int, entity_prefix: str,
        weight_entity: str | None = None, friendly_name: str | None = None,
    ) -> None:
        """Link (or re-link) a member to their Home Assistant entity prefix.

        No credential is stored: the server URL and token are settings, shared by
        everyone. Re-linking keeps ``last_synced_at`` and the import history — a
        member correcting their prefix should not get their whole history
        re-announced — but clears ``backfilled_at`` so the new prefix's history is
        pulled in once.

        Pointing at a *different* prefix also clears the replay-guard columns.
        They describe the previous scale's last reading, and carrying them over
        would let a stale weight silently swallow the new scale's first weigh-in."""
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO ha_account
                    (user_id, guild_id, entity_prefix, weight_entity,
                     friendly_name, alerts_enabled, last_synced_at, linked_at)
                VALUES (?, ?, ?, ?, ?, 1, NULL, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    guild_id       = excluded.guild_id,
                    entity_prefix  = excluded.entity_prefix,
                    weight_entity  = excluded.weight_entity,
                    friendly_name  = excluded.friendly_name,
                    linked_at      = excluded.linked_at,
                    backfilled_at  = NULL,
                    last_reading_at = CASE
                        WHEN ha_account.entity_prefix = excluded.entity_prefix
                        THEN ha_account.last_reading_at ELSE NULL END,
                    last_weight_kg = CASE
                        WHEN ha_account.entity_prefix = excluded.entity_prefix
                        THEN ha_account.last_weight_kg ELSE NULL END
                """,
                (user_id, guild_id, (entity_prefix or "").strip().lower(),
                 (weight_entity or "").strip().lower() or None, friendly_name,
                 _normalize_iso(None)),
            )

    def ha_get(self, user_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM ha_account WHERE user_id = ?", (user_id,)
            ).fetchone()

    def list_ha_accounts(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute("SELECT * FROM ha_account"))

    def ha_prefix_owner(self, entity_prefix: str) -> int | None:
        """The user_id already linked to ``entity_prefix``, or None.

        ``ha_account`` is keyed on user_id, so nothing in the schema stops two
        members claiming the same sensors. That has to be refused at the command
        layer instead — see app/bot.ha_link_cmd for why claiming someone else's
        scale is a privacy problem and not just a data-quality one."""
        prefix = (entity_prefix or "").strip().lower()
        with self._conn() as c:
            row = c.execute(
                "SELECT user_id FROM ha_account WHERE entity_prefix = ?",
                (prefix,),
            ).fetchone()
            return int(row["user_id"]) if row is not None else None

    def ha_unlink(self, user_id: int) -> bool:
        """Remove a link. Returns True if a row existed.

        The recorded weigh-ins and body metrics are kept — unlinking a scale is
        not a request to delete your weight history.

        The ``ha_imported`` ledger is kept too, which is the less obvious half.
        Clearing it would make unlink/relink — the obvious thing to try when sync
        looks stuck — re-import every weigh-in still inside the backfill window,
        and ``set_bodyweight`` always appends, so the member would end up with
        each of those weigh-ins recorded twice and announced again. Keeping the
        ledger costs one small row per weigh-in and makes relinking safe."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM ha_account WHERE user_id = ?", (user_id,)
            )
            return (cur.rowcount or 0) > 0

    def ha_mark_synced(self, user_id: int, at: datetime | None = None) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE ha_account SET last_synced_at = ? WHERE user_id = ?",
                (_normalize_iso(at), user_id),
            )

    def ha_mark_backfilled(
        self, user_id: int, at: datetime | None = None,
    ) -> None:
        """Record that this link has had its one-time history backfill, so
        routine polls stop asking the recorder for old states."""
        with self._conn() as c:
            c.execute(
                "UPDATE ha_account SET backfilled_at = ? WHERE user_id = ?",
                (_normalize_iso(at), user_id),
            )

    def ha_reading_imported(self, user_id: int, reading_key: str) -> bool:
        """True if this weigh-in has already been imported for this user."""
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM ha_imported WHERE user_id = ? AND reading_key = ?",
                (user_id, str(reading_key)),
            ).fetchone() is not None

    def ha_mark_reading(self, user_id: int, reading_key: str) -> bool:
        """Claim ``reading_key`` for this user. Returns True if newly recorded
        (False if already present), so two overlapping polls can't both import
        the same weigh-in — the loser of the INSERT race skips it.

        Claiming deliberately does **not** touch the replay-guard columns. Those
        describe a weigh-in that was actually *written*, and the claim happens
        before the write: setting them here meant a failed write left the guard
        pointing at a weigh-in that does not exist, which then blocked the retry.
        :meth:`ha_note_reading` records them after the write succeeds."""
        key = str(reading_key or "")
        if not key:
            return False
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO ha_imported "
                "(user_id, reading_key, imported_at) VALUES (?, ?, ?)",
                (user_id, key, _normalize_iso(None)),
            )
            return (cur.rowcount or 0) > 0

    def ha_note_reading(
        self, user_id: int, weight_kg: float, measured_at: datetime | None = None,
    ) -> None:
        """Record the weigh-in just written as this account's latest.

        ``last_weight_kg`` is the Home-Assistant-restart replay guard's
        comparison point. ``last_reading_at`` is the *measurement* time — not the
        de-duplication key — because it is also what tells an integration that
        starts publishing its own measurement log which entries were already
        imported under the old timestamp-keyed scheme."""
        with self._conn() as c:
            c.execute(
                "UPDATE ha_account SET last_reading_at = ?, last_weight_kg = ? "
                "WHERE user_id = ?",
                (_normalize_iso(measured_at), float(weight_kg), user_id),
            )

    def ha_release_reading(self, user_id: int, reading_key: str) -> bool:
        """Give back a claim made by :meth:`ha_mark_reading`.

        The claim is taken *before* the weigh-in is written, so that two
        overlapping polls can't both import it. That ordering means a failed
        write would otherwise leave the key claimed and the weigh-in permanently
        lost — the next poll would see it as already imported. Releasing on
        failure makes the retry work instead."""
        key = str(reading_key or "")
        if not key:
            return False
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM ha_imported WHERE user_id = ? AND reading_key = ?",
                (user_id, key),
            )
            return (cur.rowcount or 0) > 0

    def add_body_metrics(
        self, guild_id: int, user_id: int, metrics: Mapping[str, tuple[float, str]],
        recorded_at: datetime | None = None, source: str = "home_assistant",
    ) -> int:
        """Record body-composition metrics for one weigh-in. Returns rows written.

        ``metrics`` maps a metric key (see app/ha_client.METRICS) to
        ``(value, unit)``. Re-importing the same weigh-in is a no-op thanks to the
        UNIQUE key, so callers don't have to check first. Bodyweight is not
        accepted here — it belongs in ``bodyweights`` via
        :meth:`set_bodyweight`, which is the single choke point the protein link
        and the TDEE model hang off.

        A composition row is only meaningful alongside the weigh-in it
        describes.  Requiring that exact row here also makes an undone Home
        Assistant reading stay undone: a later metrics-only poll cannot
        resurrect body fat/BMI while the weight remains deleted."""
        ts = _normalize_iso(recorded_at)
        written = 0
        with self._conn() as c:
            if c.execute(
                "SELECT 1 FROM bodyweights "
                "WHERE user_id = ? AND recorded_at = ? LIMIT 1",
                (user_id, ts),
            ).fetchone() is None:
                return 0
            for metric, pair in metrics.items():
                if metric == "weight":
                    continue
                try:
                    value = float(pair[0])
                except (TypeError, ValueError, IndexError):
                    continue
                if not math.isfinite(value):
                    continue
                unit = str(pair[1]) if len(pair) > 1 and pair[1] else None
                cur = c.execute(
                    "INSERT OR IGNORE INTO body_metrics "
                    "(user_id, guild_id, metric, value, unit, source, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (user_id, guild_id, str(metric), value, unit, source, ts),
                )
                written += cur.rowcount or 0
        return written

    # ------------------------------------------------------------------
    # Undoing an imported weigh-in
    # ------------------------------------------------------------------

    def ha_track_reply(
        self, reply_message_id: int, user_id: int, guild_id: int,
        recorded_ats: "Iterable[str]", *, chart_message_id: int | None = None,
    ) -> None:
        """Remember which weigh-ins an announcement stands for."""
        stamps = ",".join(str(s) for s in recorded_ats if s)
        if not stamps:
            return
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO ha_reply_tracking "
                "(reply_message_id, user_id, guild_id, recorded_ats, "
                " chart_message_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (reply_message_id, user_id, guild_id, stamps,
                 chart_message_id, _normalize_iso(None)),
            )

    def ha_get_reply(self, reply_message_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM ha_reply_tracking WHERE reply_message_id = ?",
                (reply_message_id,),
            ).fetchone()

    def ha_delete_reply(self, reply_message_id: int) -> int:
        """Claim an announcement for undo. Returns rows deleted.

        The caller uses the rowcount as a lock, exactly as the lift path does: if
        two reactions land together only one gets 1 and goes on to delete the
        weigh-in, and the other no-ops instead of double-deleting."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM ha_reply_tracking WHERE reply_message_id = ?",
                (reply_message_id,),
            )
            return cur.rowcount or 0

    def find_bodyweight_near(
        self, weight_kg: float, recorded_at: datetime | str,
        *, window_seconds: int = 5, user_id: int | None = None,
    ) -> sqlite3.Row | None:
        """Find a weigh-in by value and approximate time, for an untracked message.

        Announcements posted before ``ha_reply_tracking`` existed carry no row, so
        an ❌ on one has to identify the weigh-in from what the embed shows: the
        weight and the timestamp. Home Assistant timestamps are microsecond, so
        matching to a few seconds is effectively unique — and it yields the
        ``user_id`` too, which the embed only has as a display name.

        ``user_id`` narrows the search to one member. Callers should pass the
        person reacting, because two members weighing the same at the same instant
        is unlikely but not impossible, and silently resolving to the wrong
        person's row is the one outcome worth engineering against here.
        """
        when = recorded_at if isinstance(recorded_at, datetime) else (
            _parse_iso(recorded_at)
        )
        if when is None:
            return None
        low = _normalize_iso(when - timedelta(seconds=window_seconds))
        high = _normalize_iso(when + timedelta(seconds=window_seconds))
        sql = (
            "SELECT * FROM bodyweights "
            "WHERE recorded_at BETWEEN ? AND ? "
            "  AND ABS(weight_kg - ?) < 0.005 "
        )
        params: list = [low, high, float(weight_kg)]
        if user_id is not None:
            sql += "  AND user_id = ? "
            params.append(int(user_id))
        sql += "ORDER BY recorded_at ASC, id ASC LIMIT 1"
        with self._conn() as c:
            return c.execute(sql, params).fetchone()

    def delete_weighins_at(
        self, user_id: int, recorded_ats: "Iterable[str]",
        guild_id: int = 0, actor_id: int | None = None,
        actor_name: str | None = None,
    ) -> int:
        """Delete weigh-ins by exact timestamp, with their body metrics.

        Returns the number of ``bodyweights`` rows removed. The body-composition
        rows for the same instant go too — they describe the same weigh-in, and
        leaving them would have ``/ha_body`` reporting numbers for a weigh-in that
        no longer exists.

        The ``ha_imported`` ledger entry is deliberately **kept**: somebody who
        undoes an import is saying they don't want it, and releasing the key would
        have the next poll import it straight back.
        """
        stamps = [str(s) for s in recorded_ats if s]
        if not stamps:
            return 0
        placeholders = ",".join("?" * len(stamps))
        removed = 0
        with self._conn() as c:
            rows = c.execute(
                f"SELECT id, weight_kg, recorded_at FROM bodyweights "
                f"WHERE user_id = ? AND recorded_at IN ({placeholders})",
                (user_id, *stamps),
            ).fetchall()
            if not rows:
                return 0
            ids = [int(r["id"]) for r in rows]
            id_places = ",".join("?" * len(ids))
            cur = c.execute(
                f"DELETE FROM bodyweights WHERE id IN ({id_places})", ids,
            )
            removed = cur.rowcount or 0
            c.execute(
                f"DELETE FROM body_metrics WHERE user_id = ? "
                f"AND recorded_at IN ({placeholders})",
                (user_id, *stamps),
            )
            detail = ", ".join(f"{float(r['weight_kg']):g} kg" for r in rows[:4])
            if len(rows) > 4:
                detail += f" and {len(rows) - 4} more"
            self._audit_data(
                c, guild_id, "bodyweight_delete", subject_id=user_id,
                actor_id=actor_id if actor_id is not None else user_id,
                actor_name=actor_name,
                detail=f"removed {detail}",
            )
        return removed

    def web_delete_bodyweight(
        self, guild_id: int, row_id: int, actor_name: str,
    ) -> bool:
        """Delete one weigh-in from the dashboard, with its body metrics.

        Bodyweight is global per user, so a row shown in any guild's dashboard is
        deletable by its id; ``guild_id`` only labels the audit entry."""
        with self._conn() as c:
            row = c.execute(
                "SELECT user_id, weight_kg, recorded_at FROM bodyweights "
                "WHERE id = ?",
                (row_id,),
            ).fetchone()
            if row is None:
                return False
            c.execute("DELETE FROM bodyweights WHERE id = ?", (row_id,))
            # A duplicate timestamp is unusual but legal in the legacy schema.
            # Composition belongs to the measurement instant, so keep it while
            # another parent weigh-in for that user/timestamp still exists.
            if c.execute(
                "SELECT 1 FROM bodyweights "
                "WHERE user_id = ? AND recorded_at = ? LIMIT 1",
                (row["user_id"], row["recorded_at"]),
            ).fetchone() is None:
                c.execute(
                    "DELETE FROM body_metrics "
                    "WHERE user_id = ? AND recorded_at = ?",
                    (row["user_id"], row["recorded_at"]),
                )
            self._audit(
                c, guild_id, "data", "bodyweight_delete",
                actor_name=actor_name, subject_id=row["user_id"],
                detail=f"{float(row['weight_kg']):g} kg (web)",
            )
            return True

    def latest_body_metrics(self, user_id: int) -> dict[str, sqlite3.Row]:
        """Newest row per metric for this user, as ``{metric: row}``.

        Global per user like :meth:`get_latest_bodyweight` — one body, one set of
        numbers, whichever server the scale reading landed in. Orphaned legacy
        rows without their matching weigh-in are ignored defensively."""
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT bm.metric, bm.value, bm.unit, bm.source, bm.recorded_at
                  FROM body_metrics bm
                 WHERE bm.user_id = ?
                   AND EXISTS (
                       SELECT 1 FROM bodyweights bw
                        WHERE bw.user_id = bm.user_id
                          AND bw.recorded_at = bm.recorded_at
                   )
                   AND bm.recorded_at = (
                       SELECT MAX(b2.recorded_at) FROM body_metrics b2
                        WHERE b2.user_id = bm.user_id
                          AND b2.metric = bm.metric
                          AND EXISTS (
                              SELECT 1 FROM bodyweights bw2
                               WHERE bw2.user_id = b2.user_id
                                 AND bw2.recorded_at = b2.recorded_at
                          )
                   )
                """,
                (user_id,),
            ).fetchall()
            return {str(r["metric"]): r for r in rows}

    def latest_body_metric_snapshot(self, user_id: int) -> dict | None:
        """The newest *coherent* smart-scale reading for one user.

        ``latest_body_metrics`` intentionally resolves every metric
        independently, which is useful for discovery but can combine a current
        body-fat value with a months-old BMI.  User-facing cards need one actual
        measurement instead: the weight and only the metrics recorded at that
        exact timestamp.
        """
        with self._conn() as c:
            head = c.execute(
                """
                SELECT bm.recorded_at,
                       (
                           SELECT bw.weight_kg
                             FROM bodyweights bw
                            WHERE bw.user_id = bm.user_id
                              AND bw.recorded_at = bm.recorded_at
                            ORDER BY bw.id DESC
                            LIMIT 1
                       ) AS weight_kg
                  FROM body_metrics bm
                 WHERE bm.user_id = ?
                   AND EXISTS (
                       SELECT 1 FROM bodyweights bw
                        WHERE bw.user_id = bm.user_id
                          AND bw.recorded_at = bm.recorded_at
                   )
                 ORDER BY bm.recorded_at DESC, bm.id DESC
                 LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if head is None:
                return None
            rows = c.execute(
                """
                SELECT metric, value, unit, source, recorded_at
                  FROM body_metrics
                 WHERE user_id = ? AND recorded_at = ?
                 ORDER BY id ASC
                """,
                (user_id, head["recorded_at"]),
            ).fetchall()
            return {
                "recorded_at": str(head["recorded_at"]),
                "weight_kg": float(head["weight_kg"]),
                "metrics": {str(r["metric"]): r for r in rows},
            }

    def body_metric_history(
        self, user_id: int, metric: str, limit: int = 1000,
    ) -> list[sqlite3.Row]:
        """The newest ``limit`` values for one metric, returned oldest-first.

        The inner-descending/outer-ascending shape is deliberate.  Applying
        ``LIMIT`` directly to an ascending query returns the user's *oldest*
        values once a smart scale has more rows than the cap, making every graph
        and coach payload stale.  Orphaned legacy metrics are excluded too.
        """
        return self.body_metric_histories(
            user_id, (str(metric),), limit_per_metric=limit,
        ).get(str(metric), [])

    def body_metric_histories(
        self,
        user_id: int,
        metrics: "Iterable[str]",
        *,
        limit_per_metric: int = 1000,
    ) -> dict[str, list[sqlite3.Row]]:
        """Newest history for several metrics in one transaction.

        The dashboard asks for every scale metric at once. Opening one
        ``BEGIN IMMEDIATE`` transaction per registry entry needlessly
        serialises writers even when a member has no composition data, so a
        partitioned window query applies the cap independently in one read.
        Each returned series remains oldest-first for plotting.
        """
        keys = tuple(dict.fromkeys(str(metric) for metric in metrics if metric))
        cap = max(0, int(limit_per_metric))
        if not keys or cap == 0:
            return {}
        placeholders = ",".join("?" * len(keys))
        with self._conn() as c:
            rows = c.execute(
                f"""
                WITH ranked AS (
                    SELECT bm.id, bm.metric, bm.value, bm.unit, bm.recorded_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY bm.metric
                               ORDER BY bm.recorded_at DESC, bm.id DESC
                           ) AS recency
                      FROM body_metrics bm
                     WHERE bm.user_id = ?
                       AND bm.metric IN ({placeholders})
                       AND EXISTS (
                           SELECT 1 FROM bodyweights bw
                            WHERE bw.user_id = bm.user_id
                              AND bw.recorded_at = bm.recorded_at
                       )
                )
                SELECT metric, value, unit, recorded_at
                  FROM ranked
                 WHERE recency <= ?
                 ORDER BY metric ASC, recorded_at ASC, id ASC
                """,
                (user_id, *keys, cap),
            ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(str(row["metric"]), []).append(row)
        return grouped

    def body_metric_summaries_between(
        self,
        user_id: int,
        metrics: "Iterable[str]",
        start_iso: str,
        end_iso: str,
    ) -> list[sqlite3.Row]:
        """First/latest/count summaries inside an exact UTC time window.

        This is the bounded coach read: SQLite aggregates the full requested
        window without shipping every raw scale row to the model or truncating
        away the beginning of a long window.
        """
        keys = tuple(dict.fromkeys(str(metric) for metric in metrics if metric))
        if not keys:
            return []
        placeholders = ",".join("?" * len(keys))
        with self._conn() as c:
            return c.execute(
                f"""
                WITH eligible AS (
                    SELECT bm.id, bm.metric, bm.value, bm.unit, bm.recorded_at
                      FROM body_metrics bm
                     WHERE bm.user_id = ?
                       AND bm.metric IN ({placeholders})
                       AND bm.recorded_at >= ?
                       AND bm.recorded_at <= ?
                       AND EXISTS (
                           SELECT 1 FROM bodyweights bw
                            WHERE bw.user_id = bm.user_id
                              AND bw.recorded_at = bm.recorded_at
                       )
                ),
                ranked AS (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY metric
                               ORDER BY recorded_at ASC, id ASC
                           ) AS oldest,
                           ROW_NUMBER() OVER (
                               PARTITION BY metric
                               ORDER BY recorded_at DESC, id DESC
                           ) AS newest,
                           COUNT(*) OVER (PARTITION BY metric) AS samples
                      FROM eligible
                )
                SELECT metric,
                       MAX(CASE WHEN oldest = 1 THEN value END) AS first_value,
                       MAX(CASE WHEN newest = 1 THEN value END) AS latest_value,
                       MAX(CASE WHEN oldest = 1 THEN recorded_at END) AS first_at,
                       MAX(CASE WHEN newest = 1 THEN recorded_at END) AS latest_at,
                       MAX(CASE WHEN newest = 1 THEN unit END) AS latest_unit,
                       MAX(samples) AS samples
                  FROM ranked
                 GROUP BY metric
                 ORDER BY metric ASC
                """,
                (user_id, *keys, str(start_iso), str(end_iso)),
            ).fetchall()

    # ------------------------------------------------------------------
    # Bot-wide user nicknames
    # ------------------------------------------------------------------

    def set_user_nickname(self, user_id: int, nickname: str, set_by: int) -> None:
        """Create or replace a bot-wide display nickname for ``user_id``."""
        ts = _normalize_iso(None)
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO user_nicknames (user_id, nickname, set_by, set_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE
                    SET nickname = excluded.nickname,
                        set_by   = excluded.set_by,
                        set_at   = excluded.set_at
                """,
                (user_id, nickname.strip(), set_by, ts),
            )

    def remove_user_nickname(self, user_id: int) -> bool:
        """Delete the nickname for ``user_id``. Returns True if one existed."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM user_nicknames WHERE user_id = ?", (user_id,)
            )
            return (cur.rowcount or 0) > 0

    def get_user_nickname(self, user_id: int) -> str | None:
        """Return the stored nickname for ``user_id``, or None."""
        with self._conn() as c:
            row = c.execute(
                "SELECT nickname FROM user_nicknames WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return row["nickname"] if row else None

    def nickname_owner(self, nickname: str) -> int | None:
        """Return the user_id currently holding ``nickname`` (case-insensitive),
        or None if nobody does.

        Used before auto-assigning a PerfectGym first name so two members who
        share a first name don't end up sharing one nickname — the same table
        drives chat lift attribution (:func:`_resolve_nickname_target`), and a
        shared nickname would silently log one member's lift under the other.
        Matched ``COLLATE NOCASE`` to mirror the resolver's case-insensitive
        prefix match.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT user_id FROM user_nicknames "
                "WHERE nickname = ? COLLATE NOCASE",
                (nickname.strip(),),
            ).fetchone()
            return int(row["user_id"]) if row else None

    def list_user_nicknames(self) -> list[sqlite3.Row]:
        """All rows from ``user_nicknames``, ordered by nickname."""
        with self._conn() as c:
            return list(c.execute(
                "SELECT user_id, nickname, set_by, set_at "
                "FROM user_nicknames ORDER BY nickname COLLATE NOCASE"
            ))

    def update_revo_polling_state(
        self,
        user_id: int,
        last_ticket_signature: str | None,
        last_streak_weeks: int | None,
    ) -> None:
        """Persist the ticket-tally cursor + cached streak after a poll cycle.

        Retained for the ticket-signature cursor; the attendance poller now
        tracks check-ins via :meth:`update_revo_checkin_state` instead.
        """
        ts = _normalize_iso(None)
        with self._conn() as c:
            c.execute(
                """
                UPDATE revo_account
                   SET last_ticket_signature = ?,
                       last_streak_weeks     = ?,
                       last_polled_at        = ?
                 WHERE user_id = ?
                """,
                (last_ticket_signature, last_streak_weeks, ts, user_id),
            )

    def update_revo_checkin_state(
        self,
        user_id: int,
        last_checkin_date: str | None,
        last_streak_weeks: int | None,
    ) -> None:
        """Persist the per-day check-in cursor + cached streak after a poll.

        ``last_checkin_date`` is the most recent attended day as an ISO
        ``YYYY-MM-DD`` string (derived from the streaks calendar), used by the
        attendance poller to detect a *new* check-in since the last cycle.
        """
        ts = _normalize_iso(None)
        with self._conn() as c:
            c.execute(
                """
                UPDATE revo_account
                   SET last_checkin_date = ?,
                       last_streak_weeks = ?,
                       last_polled_at    = ?
                 WHERE user_id = ?
                """,
                (last_checkin_date, last_streak_weeks, ts, user_id),
            )

    def lifts_for_message(
        self, guild_id: int, message_id: int,
    ) -> list[sqlite3.Row]:
        """All rows the bot stored for one source message. Used by the edit
        handler to diff parsed-now vs. stored-then."""
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT id, user_id, username, equipment, weight_kg,
                       bodyweight_add AS bw, reps
                FROM lifts
                WHERE guild_id = ? AND message_id = ?
                """,
                (guild_id, message_id),
            ))

    def update_lift_weight(
        self, lift_id: int, weight_kg: float, bodyweight_add: bool,
        reps: int | None,
    ) -> None:
        """Update one lift row in place. Used when a user edits their gym
        post and the new weight differs from what we previously stored."""
        with self._conn() as c:
            c.execute(
                """
                UPDATE lifts
                SET weight_kg = ?, bodyweight_add = ?, reps = ?
                WHERE id = ?
                """,
                (weight_kg, 1 if bodyweight_add else 0, reps, lift_id),
            )

    # ------------------------------------------------------------------
    # Bodyweight tracking
    # ------------------------------------------------------------------
    def set_bodyweight(
        self, guild_id: int, user_id: int, weight_kg: float,
        recorded_at: datetime | None = None,
        actor_id: int | None = None,
        actor_name: str | None = None,
    ) -> int | None:
        """Record a new bodyweight measurement for a user.

        We append rather than overwrite so the user can see how their
        bodyweight has trended and so historical lifts can in principle
        be re-rendered against the bodyweight that was current at the time.
        ``actor_id``/``actor_name`` credit the logger on proxy
        (``@user bw 80kg``) entries; they default to the target.

        Returns the new protein ceiling (grams) when this weigh-in moved a
        bodyweight-linked target, else None — so the reply sites can append a
        "Protein max updated to X g" line. This is the single choke point every
        bodyweight write goes through, which is why the link auto-update lives
        here rather than in each caller.
        """
        ts = _normalize_iso(recorded_at)
        with self._conn() as c:
            c.execute(
                "INSERT INTO bodyweights (guild_id, user_id, weight_kg, recorded_at) "
                "VALUES (?, ?, ?, ?)",
                (guild_id, user_id, float(weight_kg), ts),
            )
            self._audit_data(
                c, guild_id, "bodyweight_log", subject_id=user_id,
                actor_id=actor_id if actor_id is not None else user_id,
                actor_name=actor_name,
                detail=f"bodyweight {float(weight_kg):g} kg",
            )
            return self._apply_bodyweight_protein_link(
                c, guild_id, user_id, float(weight_kg),
            )

    def _apply_bodyweight_protein_link(
        self, c: sqlite3.Connection, guild_id: int, user_id: int,
        weight_kg: float,
    ) -> int | None:
        """Re-derive a bodyweight-linked protein target after a new weigh-in.

        No-op (returns None) unless the user has a link. When they do, the new
        ceiling is ``round(weight_kg * grams_per_kg)`` and it's only written when
        it differs from what currently resolves — repeated logs of the same
        weight mustn't spam identical effective-dated rows. A derived value above
        :data:`_BW_LINK_MAX_TARGET_G` is dropped rather than written (the
        bodyweight is still recorded), so a fat-fingered weigh-in can't poison
        the ceiling; see that constant for why this is a backstop, not the
        primary guard.

        Because a link neutralises any weekend override when it's created and the
        setup flow refuses to add one while linked, only the ``default`` row
        needs re-deriving here — it resolves on every day, weekday and weekend
        alike.
        """
        link = c.execute(
            "SELECT grams_per_kg, username FROM protein_bw_links "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if link is None:
            return None
        grams = int(round(weight_kg * float(link["grams_per_kg"])))
        if grams <= 0 or grams > _BW_LINK_MAX_TARGET_G:
            return None
        current = targets.resolve(
            self._target_rows(c, user_id), targets.local_today(),
        ).protein.value
        if current is not None and int(round(current)) == grams:
            return None  # same weight (or same rounded target) — no new row
        # Nutrition targets are global but each row records a home guild
        # (nutrition_home_guild = newest set_at). A weigh-in is not a nutrition
        # command, so it must not re-home the user's targets to whichever server
        # the scale reading was typed in. Re-stamp the guild that home already
        # resolves to; only fall back to the weigh-in guild when there's no
        # prior target row to inherit from.
        home = c.execute(
            "SELECT guild_id FROM nutrition_targets WHERE user_id = ? "
            "ORDER BY set_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        target_guild = (
            int(home["guild_id"])
            if home is not None and home["guild_id"] is not None
            else guild_id
        )
        username = link["username"] or ""
        detail = self._protein_goal_write(
            c, target_guild, user_id, username, grams,
        )
        self._audit_data(
            c, guild_id, "protein_goal_set", subject_id=user_id,
            subject_name=username, detail=f"{detail} (tied to bodyweight)",
        )
        return grams

    def get_latest_bodyweight(
        self, guild_id: int, user_id: int,
    ) -> sqlite3.Row | None:
        """Return the most recent bodyweight row for this user, or None.

        Bodyweight is a personal metric and tracked **globally** — a weigh-in
        logged in any server (or DM) is the user's latest everywhere.
        ``guild_id`` is kept for signature compatibility but not filtered on."""
        with self._conn() as c:
            row = c.execute(
                "SELECT weight_kg, recorded_at FROM bodyweights "
                "WHERE user_id = ? "
                "ORDER BY recorded_at DESC, id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            return row

    def bodyweight_history(
        self, guild_id: int, user_id: int, limit: int = 1000,
    ) -> list[sqlite3.Row]:
        """Return this user's ``limit`` most recent measurements, oldest-first.

        Used by ``/bodyweight_history`` and ``/bodyweight_graph`` so the
        timeline plots left-to-right without an extra reverse step. Global:
        weigh-ins from every server are one timeline (``guild_id`` unused).

        The limit takes the **newest** rows, not the oldest. A plain
        ``ORDER BY recorded_at ASC LIMIT ?`` truncates the wrong end, so once a
        user had more rows than the limit every caller silently showed ancient
        data — and app/bot.py's coaching context, which reads ``rows[-1]`` as the
        latest weigh-in, reported a months-old weight as current. Hand-logging
        takes years to reach the limits in use (1000, 400, 60); a smart scale
        syncing through Home Assistant reaches 60 in two months, which is how
        this surfaced.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, weight_kg, recorded_at FROM ("
                "  SELECT weight_kg, recorded_at, id FROM bodyweights "
                "   WHERE user_id = ? "
                "   ORDER BY recorded_at DESC, id DESC LIMIT ?"
                ") ORDER BY recorded_at ASC, id ASC",
                (user_id, int(limit)),
            ).fetchall()
            return rows

    def latest_bodyweights_bulk(
        self, guild_id: int, user_ids: list[int],
    ) -> dict[int, float]:
        """Latest known bodyweight per user_id, as a {user_id: kg} dict.

        Used by `/leaderboard` to compute everyone's true weight without
        issuing one query per row. Users without any bodyweight entry are
        omitted. Global, matching :meth:`get_latest_bodyweight`: each user's
        latest weigh-in regardless of which server it was logged in
        (``guild_id`` kept for signature compatibility, not filtered on).
        """
        if not user_ids:
            return {}
        # Dedupe and parameterise; SQLite has a default limit of 999 host
        # parameters, well above what /leaderboard ever passes (max 25).
        unique = list({int(u) for u in user_ids})
        placeholders = ",".join("?" * len(unique))
        with self._conn() as c:
            # Mirror `get_latest_bodyweight`'s ORDER BY exactly: latest
            # recorded_at wins, with id DESC as the tiebreaker for entries
            # that share a timestamp. Using ROW_NUMBER avoids the join-by-
            # timestamp double-counting that a plain GROUP BY MAX would
            # introduce on ties.
            rows = c.execute(
                f"""
                WITH ranked AS (
                    SELECT user_id, weight_kg,
                           ROW_NUMBER() OVER (
                               PARTITION BY user_id
                               ORDER BY recorded_at DESC, id DESC
                           ) AS rn
                    FROM bodyweights
                    WHERE user_id IN ({placeholders})
                )
                SELECT user_id, weight_kg FROM ranked WHERE rn = 1
                """,
                [*unique],
            ).fetchall()
        return {int(r["user_id"]): float(r["weight_kg"]) for r in rows}

    # ------------------------------------------------------------------
    # Presence tracking
    # ------------------------------------------------------------------
    def presence_track_add(
        self, guild_id: int, user_id: int, started_by: int,
    ) -> bool:
        """Mark ``user_id`` as tracked in ``guild_id``. Returns True if a new
        row was inserted, False if it was already being tracked."""
        ts = _normalize_iso(None)
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO presence_tracked_users "
                "(guild_id, user_id, started_by, started_at) VALUES (?, ?, ?, ?)",
                (guild_id, user_id, started_by, ts),
            )
            return (cur.rowcount or 0) > 0

    def presence_track_remove(
        self, guild_id: int, user_id: int, *, purge: bool = False,
    ) -> bool:
        """Stop tracking ``user_id``. If ``purge`` is True, also delete the
        recorded event history. Returns True if a tracking row existed."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM presence_tracked_users "
                "WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            removed = (cur.rowcount or 0) > 0
            if purge:
                c.execute(
                    "DELETE FROM presence_events "
                    "WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            return removed

    def presence_track_list(self, guild_id: int) -> list[sqlite3.Row]:
        """All users currently being presence-tracked in ``guild_id``."""
        with self._conn() as c:
            return list(c.execute(
                "SELECT user_id, started_by, started_at "
                "FROM presence_tracked_users WHERE guild_id = ? "
                "ORDER BY started_at",
                (guild_id,),
            ))

    def presence_is_tracked(self, guild_id: int, user_id: int) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM presence_tracked_users "
                "WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            return row is not None

    def presence_log_event(
        self, guild_id: int, user_id: int, status: str,
        at: datetime | None = None,
    ) -> bool:
        """Append a presence event, de-duplicating against the most recent
        stored status for this user. Returns True if a row was inserted."""
        ts = _normalize_iso(at)
        with self._conn() as c:
            last = c.execute(
                "SELECT status FROM presence_events "
                "WHERE guild_id = ? AND user_id = ? "
                "ORDER BY at DESC, id DESC LIMIT 1",
                (guild_id, user_id),
            ).fetchone()
            if last is not None and last["status"] == status:
                return False
            c.execute(
                "INSERT INTO presence_events (guild_id, user_id, status, at) "
                "VALUES (?, ?, ?, ?)",
                (guild_id, user_id, status, ts),
            )
            return True

    def presence_events_for(
        self, guild_id: int, user_id: int,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[sqlite3.Row]:
        """Return presence events for ``user_id`` in chronological order.

        Also includes the most recent event strictly before ``since`` (if
        any) so callers can know the user's status at the start of the
        window without needing a separate query.
        """
        with self._conn() as c:
            rows: list[sqlite3.Row] = []
            if since is not None:
                start_iso = _normalize_iso(since)
                prior = c.execute(
                    "SELECT status, at FROM presence_events "
                    "WHERE guild_id = ? AND user_id = ? AND at < ? "
                    "ORDER BY at DESC, id DESC LIMIT 1",
                    (guild_id, user_id, start_iso),
                ).fetchone()
                if prior is not None:
                    rows.append(prior)
                params: list = [guild_id, user_id, start_iso]
                where = "guild_id = ? AND user_id = ? AND at >= ?"
            else:
                params = [guild_id, user_id]
                where = "guild_id = ? AND user_id = ?"
            if until is not None:
                where += " AND at <= ?"
                params.append(_normalize_iso(until))
            rows.extend(c.execute(
                f"SELECT status, at FROM presence_events WHERE {where} "
                "ORDER BY at ASC, id ASC",
                params,
            ).fetchall())
            return rows

    # ------------------------------------------------------------------
    # Activity helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _activity_set(row: sqlite3.Row) -> list[dict]:
        """Decode a row's concurrent-activity snapshot into ``[{"n","i","a"}]``.

        ``a`` is the Discord application id (or None) used to resolve an icon
        for apps that ship no image. Falls back to the legacy single
        ``activity``/``image_url`` columns for rows written before the
        ``activities`` JSON column existed, so old and new data behave the
        same."""
        keys = row.keys()
        raw = row["activities"] if "activities" in keys else None
        if raw:
            try:
                items = json.loads(raw)
            except (ValueError, TypeError):
                items = None
            if isinstance(items, list):
                return [
                    {"n": str(it["n"]), "i": it.get("i"), "a": it.get("a")}
                    for it in items
                    if isinstance(it, dict) and it.get("n")
                ]
        name = row["activity"]
        if name:
            img = row["image_url"] if "image_url" in keys else None
            return [{"n": name, "i": img, "a": None}]
        return []

    @staticmethod
    def _split_activity(item) -> tuple[str, str | None, int | None]:
        """Normalise a logged activity into ``(name, image_url, app_id)``,
        tolerating plain ``(name, image)`` pairs from the legacy wrapper."""
        name = item[0]
        image = item[1] if len(item) > 1 else None
        app_id = item[2] if len(item) > 2 else None
        return str(name), image, app_id

    def activity_log_set(
        self, guild_id: int, user_id: int,
        activities: list[tuple],
        at: datetime | None = None,
    ) -> bool:
        """Append a snapshot of *all* games/apps a user is running at once.

        ``activities`` is an ordered list of ``(name, image_url[, app_id])`` for
        every concurrent activity (empty = stopped everything). De-duplicates
        against the previous set of names regardless of Discord's ordering.
        Late-arriving image/application metadata enriches the existing row in
        place, preserving the real session start time rather than creating a
        false transition. The legacy ``activity``/``image_url`` columns mirror
        the primary (first) entry. Returns True if a row was inserted."""
        ts = _normalize_iso(at)
        parsed = [self._split_activity(a) for a in activities if a and a[0]]
        names = [name for name, _img, _aid in parsed]
        with self._conn() as c:
            last = c.execute(
                "SELECT id, activity, image_url, activities FROM activity_events "
                "WHERE guild_id = ? AND user_id = ? "
                "ORDER BY at DESC, id DESC LIMIT 1",
                (guild_id, user_id),
            ).fetchone()
            if last is None and not names:
                return False
            if last is not None:
                previous = self._activity_set(last)
                last_names = [d["n"] for d in previous]
                if set(last_names) == set(names) and len(last_names) == len(names):
                    # Discord can reorder concurrent activities or deliver rich
                    # presence art/application ids after the activity name.
                    # Preserve the original session boundary and order while
                    # enriching the existing snapshot in place.
                    incoming = {
                        name: {"i": img, "a": aid}
                        for name, img, aid in parsed
                    }
                    enriched = [
                        {
                            "n": item["n"],
                            "i": incoming[item["n"]]["i"] or item.get("i"),
                            "a": incoming[item["n"]]["a"] or item.get("a"),
                        }
                        for item in previous
                    ]
                    if enriched != previous:
                        payload = json.dumps([
                            {
                                "n": item["n"],
                                "i": item["i"],
                                **({"a": item["a"]} if item["a"] else {}),
                            }
                            for item in enriched
                        ]) if enriched else None
                        c.execute(
                            "UPDATE activity_events "
                            "SET image_url = ?, activities = ? WHERE id = ?",
                            (
                                enriched[0]["i"] if enriched else None,
                                payload,
                                last["id"],
                            ),
                        )
                    return False
            primary = names[0] if names else None
            primary_img = parsed[0][1] if names else None
            payload = json.dumps([
                {"n": name, "i": img, **({"a": aid} if aid else {})}
                for name, img, aid in parsed
            ]) if names else None
            c.execute(
                "INSERT INTO activity_events "
                "(guild_id, user_id, activity, image_url, activities, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, user_id, primary, primary_img, payload, ts),
            )
            return True

    def activity_log_event(
        self, guild_id: int, user_id: int, activity: str | None,
        at: datetime | None = None, image_url: str | None = None,
    ) -> bool:
        """Append a single-activity event (back-compat wrapper around
        :meth:`activity_log_set`). ``activity`` of None means stopped."""
        acts = [(activity, image_url)] if activity else []
        return self.activity_log_set(guild_id, user_id, acts, at=at)

    # ---- web dashboard: current presence/activity snapshots --------------

    def presence_current(
        self, guild_id: int, user_id: int,
    ) -> sqlite3.Row | None:
        """Latest known presence status (and when) for a user, or None."""
        with self._conn() as c:
            return c.execute(
                "SELECT status, at FROM presence_events "
                "WHERE guild_id = ? AND user_id = ? "
                "ORDER BY at DESC, id DESC LIMIT 1",
                (guild_id, user_id),
            ).fetchone()

    def activity_current(
        self, guild_id: int, user_id: int,
    ) -> sqlite3.Row | None:
        """Latest activity event for a user (activity may be NULL = stopped).
        Carries the ``activities`` JSON so callers can list every concurrent
        game; :meth:`_activity_set` decodes it."""
        with self._conn() as c:
            return c.execute(
                "SELECT activity, image_url, activities, at FROM activity_events "
                "WHERE guild_id = ? AND user_id = ? "
                "ORDER BY at DESC, id DESC LIMIT 1",
                (guild_id, user_id),
            ).fetchone()

    def activity_current_set(
        self, guild_id: int, user_id: int,
    ) -> tuple[list[dict], str] | None:
        """Latest snapshot decoded as ``(activities, iso_timestamp)`` where
        ``activities`` is a list of ``{"n","i"}`` (empty when stopped), or None
        if the user has no recorded activity events."""
        row = self.activity_current(guild_id, user_id)
        if row is None:
            return None
        return self._activity_set(row), row["at"]

    def activity_image_map(
        self, guild_id: int, user_id: int,
    ) -> dict[str, str]:
        """Best-known image URL per game name for a user (most recent wins).
        Lets the activity feed show art for games whose current event has none
        but an earlier session captured one. Reads every concurrent activity in
        each snapshot, not just the primary."""
        return self.activity_metadata_maps(guild_id, user_id)[0]

    def activity_appid_map(
        self, guild_id: int, user_id: int,
    ) -> dict[str, int]:
        """Best-known Discord application id per activity name (most recent
        wins). Lets the dashboard resolve an icon for apps that ship no image
        but expose an application id."""
        return self.activity_metadata_maps(guild_id, user_id)[1]

    def activity_metadata_maps(
        self, guild_id: int, user_id: int,
    ) -> tuple[dict[str, str], dict[str, int]]:
        """Best-known image and application-id maps from one history scan.

        The live dashboard needs both maps on every refresh. Keeping the
        combined path avoids reading a growing activity history twice per
        tracked member while the individual methods remain as compatibility
        wrappers.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT activity, image_url, activities FROM activity_events "
                "WHERE guild_id = ? AND user_id = ? "
                "ORDER BY at ASC, id ASC",
                (guild_id, user_id),
            )
            images: dict[str, str] = {}
            app_ids: dict[str, int] = {}
            for row in rows:
                for item in self._activity_set(row):
                    if item["i"]:
                        images[item["n"]] = item["i"]
                    if item.get("a"):
                        app_ids[item["n"]] = int(item["a"])
            return images, app_ids

    def activity_events_for(
        self, guild_id: int, user_id: int,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[sqlite3.Row]:
        """Return activity events in chronological order, including the most
        recent event before ``since`` as a carry-in (same pattern as
        ``presence_events_for``)."""
        with self._conn() as c:
            rows: list[sqlite3.Row] = []
            if since is not None:
                start_iso = _normalize_iso(since)
                prior = c.execute(
                    "SELECT activity, image_url, activities, at "
                    "FROM activity_events "
                    "WHERE guild_id = ? AND user_id = ? AND at < ? "
                    "ORDER BY at DESC, id DESC LIMIT 1",
                    (guild_id, user_id, start_iso),
                ).fetchone()
                if prior is not None:
                    rows.append(prior)
                params: list = [guild_id, user_id, start_iso]
                where = "guild_id = ? AND user_id = ? AND at >= ?"
            else:
                params = [guild_id, user_id]
                where = "guild_id = ? AND user_id = ?"
            if until is not None:
                where += " AND at <= ?"
                params.append(_normalize_iso(until))
            rows.extend(c.execute(
                f"SELECT activity, image_url, activities, at "
                f"FROM activity_events WHERE {where} "
                "ORDER BY at ASC, id ASC",
                params,
            ).fetchall())
            return rows

    def activity_sets_for(
        self, guild_id: int, user_id: int,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[tuple[list[str], str]]:
        """Like :meth:`activity_events_for` but each event is decoded into
        ``(active_names, iso_timestamp)`` — the full set of games/apps running
        at that moment. Feeds the overlap-aware activity totals so concurrent
        play (e.g. a game plus a launcher) counts toward every title."""
        return [
            ([d["n"] for d in self._activity_set(r)], r["at"])
            for r in self.activity_events_for(
                guild_id, user_id, since=since, until=until,
            )
        ]

    # ------------------------------------------------------------------
    # Voice-channel tracking
    # ------------------------------------------------------------------

    def voice_log_event(
        self, guild_id: int, user_id: int, event: str,
        channel_id: int | None = None, channel_name: str | None = None,
        at: datetime | None = None,
    ) -> None:
        """Append a voice transition.

        ``event`` is one of 'join'/'leave'/'move' (channel presence) or
        'mute_on'/'mute_off'/'deaf_on'/'deaf_off' (mute/deafen signals) — see
        the ``voice_events`` DDL comment for the full contract.
        """
        with self._conn() as c:
            c.execute(
                "INSERT INTO voice_events "
                "(guild_id, user_id, event, channel_id, channel_name, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, user_id, event, channel_id, channel_name,
                 _normalize_iso(at)),
            )

    def voice_events_recent(
        self, guild_id: int, since: datetime | None = None, limit: int = 100,
    ) -> list[sqlite3.Row]:
        """Recent voice transitions (newest first), capped at ``limit``, each
        carrying the member's mirrored ``display_name`` and ``avatar``.

        Only channel-presence events ('join'/'leave'/'move') are returned — the
        mute/deafen toggles would spam this human-readable feed (people mute and
        unmute constantly), and app.voicetime aggregates them elsewhere.
        """
        with self._conn() as c:
            params: list = [guild_id]
            where = "ve.guild_id = ? AND ve.event IN ('join', 'leave', 'move')"
            if since is not None:
                where += " AND ve.at >= ?"
                params.append(_normalize_iso(since))
            params.append(int(limit))
            return c.execute(
                "SELECT ve.user_id, ve.event, ve.channel_id, ve.channel_name, "
                "ve.at, mem.display_name AS display_name, mem.avatar AS avatar "
                "FROM voice_events ve "
                "LEFT JOIN members mem "
                "  ON mem.guild_id = ve.guild_id AND mem.user_id = ve.user_id "
                f"WHERE {where} "
                "ORDER BY ve.at DESC, ve.id DESC LIMIT ?",
                params,
            ).fetchall()

    def voice_events_for(
        self, guild_id: int, user_id: int,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[sqlite3.Row]:
        """Return one member's voice events (``event``, ``at``) in time order,
        for app.voicetime to reconstruct in-call / muted / deafened durations.

        Includes carry-in so the caller knows the member's state at ``since``
        without processing all of history. Unlike presence — a single signal —
        voice interleaves three independent ones, so we seed carry-in before
        the window. Mute and deafen each seed only their single latest signal.
        Channel presence needs more: app.voicetime re-anchors a stale mute or
        deafen to off *only* on a 'join' (Discord doesn't reliably emit
        mute_off on leave), while a 'move' deliberately preserves mute. If the
        latest pre-window channel event is a 'move', the session's opening
        'join' — the row that clears that stale state — would be lost, letting
        a phantom mute bleed across the whole window. So the channel group
        seeds *every* event since (and including) the most recent 'join', not
        just the latest one. Replayed in timestamp order this reconstructs the
        opening state correctly (a mute predating the last join is overridden
        by that join's reset; a move after it keeps the mute alive).

        Rows are ordered by (at, id) so a 'join' and the 'mute_on' the handler
        writes at the same instant (an already-muted join) stay in insert order.
        """
        with self._conn() as c:
            rows: list[sqlite3.Row] = []
            if since is not None:
                start_iso = _normalize_iso(since)
                # Channel presence: seed every event back to the session's
                # opening 'join' so the mute/deafen reset that join carries is
                # never dropped behind a later 'move' (see docstring). With no
                # prior 'join' to anchor to, a lone leave/move is all we have,
                # so fall back to the single latest channel event.
                last_join = c.execute(
                    "SELECT at FROM voice_events "
                    "WHERE guild_id = ? AND user_id = ? AND at < ? "
                    "AND event = 'join' "
                    "ORDER BY at DESC, id DESC LIMIT 1",
                    (guild_id, user_id, start_iso),
                ).fetchone()
                if last_join is not None:
                    rows.extend(c.execute(
                        "SELECT event, at, id FROM voice_events "
                        "WHERE guild_id = ? AND user_id = ? "
                        "AND at >= ? AND at < ? "
                        "AND event IN ('join', 'leave', 'move') "
                        "ORDER BY at ASC, id ASC",
                        (guild_id, user_id, last_join["at"], start_iso),
                    ).fetchall())
                else:
                    prior = c.execute(
                        "SELECT event, at, id FROM voice_events "
                        "WHERE guild_id = ? AND user_id = ? AND at < ? "
                        "AND event IN ('join', 'leave', 'move') "
                        "ORDER BY at DESC, id DESC LIMIT 1",
                        (guild_id, user_id, start_iso),
                    ).fetchone()
                    if prior is not None:
                        rows.append(prior)
                # Mute and deafen carry in only their single latest signal; a
                # 'join' above overrides any that predates it.
                for group in (
                    ("mute_on", "mute_off"),
                    ("deaf_on", "deaf_off"),
                ):
                    placeholders = ", ".join("?" for _ in group)
                    prior = c.execute(
                        "SELECT event, at, id FROM voice_events "
                        "WHERE guild_id = ? AND user_id = ? AND at < ? "
                        f"AND event IN ({placeholders}) "
                        "ORDER BY at DESC, id DESC LIMIT 1",
                        (guild_id, user_id, start_iso, *group),
                    ).fetchone()
                    if prior is not None:
                        rows.append(prior)
                params: list = [guild_id, user_id, start_iso]
                where = "guild_id = ? AND user_id = ? AND at >= ?"
            else:
                params = [guild_id, user_id]
                where = "guild_id = ? AND user_id = ?"
            if until is not None:
                where += " AND at <= ?"
                params.append(_normalize_iso(until))
            rows.extend(c.execute(
                f"SELECT event, at, id FROM voice_events WHERE {where} "
                "ORDER BY at ASC, id ASC",
                params,
            ).fetchall())
            # Carry-in rows come from separate queries; merge everything into a
            # single chronological stream (id breaks same-timestamp ties).
            rows.sort(key=lambda r: (r["at"], r["id"]))
            return rows

    def voice_user_ids_since(self, guild_id: int, since: datetime) -> list[int]:
        """Distinct user_ids with any voice event at/after ``since`` in a guild.

        Backs the dashboard's per-user voice-time table: it hands back the set
        of members to summarise, and the caller then runs voice_events_for +
        app.voicetime.summarize_voice for each. The (guild_id, user_id, at)
        index — idx_voice_events_user — covers every column this reads, so the
        DISTINCT resolves from the index without touching the table.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT user_id FROM voice_events "
                "WHERE guild_id = ? AND at >= ? "
                "ORDER BY user_id",
                (guild_id, _normalize_iso(since)),
            ).fetchall()
            return [r["user_id"] for r in rows]

    # ------------------------------------------------------------------
    # Message logging (web dashboard activity feed)
    # ------------------------------------------------------------------

    def message_log_add(
        self, guild_id: int, user_id: int, content: str | None,
        channel_id: int | None = None, channel_name: str | None = None,
        message_id: int | None = None, at: datetime | None = None,
        attachments: str | None = None,
    ) -> bool:
        """Append a logged message. Idempotent on ``message_id`` (a re-dispatch
        of the same message won't create a duplicate). ``attachments`` is an
        optional JSON string of media items (images / videos / GIF embeds).
        Returns True if a row was inserted."""
        ts = _normalize_iso(at)
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO message_log "
                "(guild_id, user_id, channel_id, channel_name, "
                " message_id, content, attachments, at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, user_id, channel_id, channel_name,
                 message_id, content, attachments, ts),
            )
            if cur.rowcount > 0:
                return True
            # The message was already logged. If it predates media capture (or
            # was logged text-only) and we now have media, backfill it in — this
            # is how a re-scan adds photos/GIFs to existing rows.
            if attachments and message_id is not None:
                c.execute(
                    "UPDATE message_log SET attachments = ? "
                    "WHERE guild_id = ? AND message_id = ? "
                    "AND attachments IS NULL",
                    (attachments, guild_id, message_id),
                )
            return False

    def message_log_update_content(
        self, guild_id: int, message_id: int,
        content: str | None, attachments: str | None,
        edited_at: datetime | None = None,
    ) -> bool:
        """Overwrite a logged message's content + attachments after an edit and
        stamp ``edited_at``. Unlike :meth:`message_log_add`'s backfill, this
        replaces the stored media (an edit can add or swap images). Returns True
        if a row was updated."""
        ts = _normalize_iso(edited_at)
        with self._conn() as c:
            cur = c.execute(
                "UPDATE message_log SET content = ?, attachments = ?, "
                "edited_at = ? WHERE guild_id = ? AND message_id = ?",
                (content, attachments, ts, guild_id, message_id),
            )
            return cur.rowcount > 0

    def message_log_mark_deleted(
        self, guild_id: int, message_id: int, when: datetime | None = None,
    ) -> bool:
        """Flag a logged message as deleted (keeping its content/media) so the
        dashboard can show a "(deleted)" marker. No-op if it was never logged or
        is already flagged. Returns True if a row was newly flagged."""
        ts = _normalize_iso(when)
        with self._conn() as c:
            cur = c.execute(
                "UPDATE message_log SET deleted_at = ? "
                "WHERE guild_id = ? AND message_id = ? AND deleted_at IS NULL",
                (ts, guild_id, message_id),
            )
            return cur.rowcount > 0

    def message_count_since(
        self, guild_id: int, user_id: int, since: datetime | None = None,
    ) -> int:
        """Number of logged messages for a user, optionally since ``since``."""
        with self._conn() as c:
            if since is not None:
                row = c.execute(
                    "SELECT COUNT(*) AS n FROM message_log "
                    "WHERE guild_id = ? AND user_id = ? AND at >= ?",
                    (guild_id, user_id, _normalize_iso(since)),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT COUNT(*) AS n FROM message_log "
                    "WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                ).fetchone()
            return int(row["n"]) if row else 0

    def message_log_recent(
        self, guild_id: int, user_id: int,
        since: datetime | None = None, limit: int = 30,
    ) -> list[sqlite3.Row]:
        """Most recent logged messages for a user (newest first), optionally
        constrained to ``since`` and capped at ``limit`` rows."""
        with self._conn() as c:
            params: list = [guild_id, user_id]
            where = "guild_id = ? AND user_id = ?"
            if since is not None:
                where += " AND at >= ?"
                params.append(_normalize_iso(since))
            params.append(int(limit))
            return c.execute(
                "SELECT channel_id, channel_name, content, at "
                f"FROM message_log WHERE {where} "
                "ORDER BY at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()

    def message_daily_counts(
        self, guild_id: int, since: datetime,
    ) -> dict[str, int]:
        """Logged-message counts per calendar day (UTC) at/after ``since``,
        as ``{YYYY-MM-DD: count}``. Powers the Overview activity sparkline."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT substr(at, 1, 10) AS d, COUNT(*) AS n FROM message_log "
                "WHERE guild_id = ? AND at >= ? GROUP BY d ORDER BY d",
                (guild_id, _normalize_iso(since)),
            ).fetchall()
            return {r["d"]: int(r["n"]) for r in rows}

    def message_log_latest_at(self, guild_id: int) -> str | None:
        """ISO timestamp of the most recent logged message in ``guild_id``, or
        None if nothing has been logged yet."""
        with self._conn() as c:
            row = c.execute(
                "SELECT MAX(at) AS last_at FROM message_log WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            return row["last_at"] if row and row["last_at"] else None

    def message_active_users(
        self, guild_id: int, since: datetime | None = None,
    ) -> list[sqlite3.Row]:
        """Users who have logged messages (optionally since ``since``), each
        with a message ``count`` and ``last_at`` timestamp. Ordered most
        recently active first — drives the dashboard's whole-server feed."""
        with self._conn() as c:
            params: list = [guild_id]
            where = "guild_id = ?"
            if since is not None:
                where += " AND at >= ?"
                params.append(_normalize_iso(since))
            return c.execute(
                "SELECT user_id, COUNT(*) AS count, MAX(at) AS last_at "
                f"FROM message_log WHERE {where} "
                "GROUP BY user_id ORDER BY last_at DESC",
                params,
            ).fetchall()

    def message_channels(self, guild_id: int) -> list[sqlite3.Row]:
        """Channels that have logged messages, each with a message ``count`` and
        ``last_at``. Most-recently-active channel first — powers the Discord-style
        channel sidebar. Uses the newest seen ``channel_name`` per channel id."""
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT channel_id,
                       (SELECT channel_name FROM message_log m2
                        WHERE m2.guild_id = m.guild_id
                          AND m2.channel_id = m.channel_id
                        ORDER BY at DESC, id DESC LIMIT 1) AS channel_name,
                       COUNT(*) AS count, MAX(at) AS last_at
                FROM message_log m
                WHERE guild_id = ?
                GROUP BY channel_id
                ORDER BY last_at DESC
                """,
                (guild_id,),
            ))

    def message_channel_log(
        self, guild_id: int, channel_id: int, limit: int = 300,
    ) -> list[sqlite3.Row]:
        """The most recent ``limit`` messages in a channel, returned oldest-first
        (chat order). Each row carries the author's mirrored ``display_name`` and
        ``avatar`` (NULL if the member isn't mirrored)."""
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT ml.user_id, ml.content, ml.attachments, ml.at,
                       ml.edited_at, ml.deleted_at,
                       mem.display_name AS display_name, mem.avatar AS avatar
                FROM message_log ml
                LEFT JOIN members mem
                       ON mem.guild_id = ml.guild_id AND mem.user_id = ml.user_id
                WHERE ml.guild_id = ? AND ml.channel_id = ?
                ORDER BY ml.at DESC, ml.id DESC
                LIMIT ?
                """,
                (guild_id, channel_id, int(limit)),
            ).fetchall()
            return list(reversed(rows))

    # ---- message-log blacklist (dashboard-managed) -----------------------

    def message_blacklist_add(
        self, guild_id: int, user_id: int,
        reason: str | None = None, added_by: str | None = None,
    ) -> bool:
        """Blacklist ``user_id`` from contributing to the bot (lifts, calories,
        protein, bodyweight, commands). Their chat is still logged and kept —
        blacklisting does not delete any messages. Upserts (re-adding updates the
        reason). Returns True."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO message_log_blacklist "
                "(guild_id, user_id, reason, added_by, added_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                "reason = excluded.reason, added_by = excluded.added_by, "
                "added_at = excluded.added_at",
                (guild_id, user_id, reason, added_by, _normalize_iso(None)),
            )
            return True

    def message_blacklist_remove(self, guild_id: int, user_id: int) -> bool:
        """Remove ``user_id`` from the blacklist. Returns True if a row existed."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM message_log_blacklist "
                "WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            return (cur.rowcount or 0) > 0

    def message_blacklist_list(self, guild_id: int) -> list[sqlite3.Row]:
        """Blacklisted users in a guild, newest first, with reason/who/when."""
        with self._conn() as c:
            return list(c.execute(
                "SELECT user_id, reason, added_by, added_at "
                "FROM message_log_blacklist WHERE guild_id = ? "
                "ORDER BY added_at DESC",
                (guild_id,),
            ))

    def message_is_blacklisted(self, guild_id: int, user_id: int) -> bool:
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM message_log_blacklist "
                "WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone() is not None

    def message_blacklisted_ids(self, guild_id: int) -> set[int]:
        """All blacklisted user ids for a guild (for bulk filtering)."""
        with self._conn() as c:
            return {
                int(r["user_id"]) for r in c.execute(
                    "SELECT user_id FROM message_log_blacklist "
                    "WHERE guild_id = ?",
                    (guild_id,),
                )
            }

    # ---- per-user auto un-timeout protection -----------------------------

    def auto_untimeout_add(
        self, guild_id: int, user_id: int, added_by: str | None = None,
    ) -> bool:
        """Protect ``user_id`` so the bot auto-removes their timeouts (while the
        AUTO_UNTIMEOUT master switch is on). Idempotent. Returns True if newly
        added (False if already protected)."""
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO auto_untimeout "
                "(guild_id, user_id, added_by, added_at) VALUES (?, ?, ?, ?)",
                (guild_id, user_id, added_by, _normalize_iso(None)),
            )
            return (cur.rowcount or 0) > 0

    def auto_untimeout_remove(self, guild_id: int, user_id: int) -> bool:
        """Stop protecting ``user_id``. Returns True if a row existed."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM auto_untimeout WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            return (cur.rowcount or 0) > 0

    def auto_untimeout_is_protected(self, guild_id: int, user_id: int) -> bool:
        """Whether ``user_id`` is on the guild's auto un-timeout protected list."""
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM auto_untimeout WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone() is not None

    def auto_untimeout_list(self, guild_id: int) -> list[sqlite3.Row]:
        """Protected users in a guild, newest first, with who/when."""
        with self._conn() as c:
            return list(c.execute(
                "SELECT user_id, added_by, added_at FROM auto_untimeout "
                "WHERE guild_id = ? ORDER BY added_at DESC",
                (guild_id,),
            ))

    # ------------------------------------------------------------------
    # Nutrition targets (per-day calorie/protein goals)
    # ------------------------------------------------------------------

    @staticmethod
    def _target_rows(c: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
        """Target rules for one user, on an already-open connection."""
        return list(c.execute(
            "SELECT macro, scope, value, effective_from, set_at, username, "
            "guild_id FROM nutrition_targets WHERE user_id = ? "
            "ORDER BY effective_from, set_at",
            (user_id,),
        ))

    def nutrition_target_rows(self, user_id: int) -> list[sqlite3.Row]:
        """Every target rule for one user — a handful of rows.

        :func:`app.targets.resolve` turns these into the numbers in force on a
        given day. Callers that need a whole week should fetch once and resolve
        per day rather than asking the database seven times.
        """
        with self._conn() as c:
            return self._target_rows(c, user_id)

    def nutrition_targets_on(
        self, user_id: int, day: date | None = None,
    ) -> targets.Resolved:
        """The calorie and protein targets in force for ``user_id`` on ``day``
        (today in DISPLAY_TIMEZONE when omitted)."""
        return targets.resolve(
            self.nutrition_target_rows(user_id), day or targets.local_today(),
        )

    def _target_effective_from(
        self, c: sqlite3.Connection, user_id: int, macro: str,
    ) -> str:
        """When rules written now should start applying.

        A user's *first* rules for a macro backdate to the beginning of time, so
        they cover entries chat-backfill already logged before setup was run.
        Every later edit takes effect today, leaving the days they have already
        lived through resolving against what was true at the time.

        Call this **once** per command and pass the answer to every write it
        makes: a first-time ``/calories setup 1500 weekend:2200`` writes two
        rules, and asking again between them would backdate the weekday rule but
        not the weekend one.
        """
        row = c.execute(
            "SELECT 1 FROM nutrition_targets WHERE user_id = ? AND macro = ? "
            "LIMIT 1",
            (user_id, macro),
        ).fetchone()
        if row is None:
            return targets.BEGINNING_OF_TIME
        return targets.local_today().isoformat()

    def _nutrition_target_write(
        self, c: sqlite3.Connection, guild_id: int, user_id: int, username: str,
        macro: str, scope: str, value: float | None, effective_from: str,
    ) -> None:
        """Upsert one rule. ``value=None`` writes the "sets nothing" tombstone
        that clears an override (or switches a tracker off) without touching the
        rules older days resolve against."""
        c.execute(
            """
            INSERT INTO nutrition_targets
                (user_id, guild_id, username, macro, scope, value,
                 effective_from, set_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id, macro, scope, effective_from) DO UPDATE SET
                guild_id = excluded.guild_id,
                username = excluded.username,
                value    = excluded.value,
                set_at   = excluded.set_at
            """,
            (
                user_id, guild_id, username, macro, scope,
                None if value is None else float(value),
                effective_from, _normalize_iso(None),
            ),
        )

    def _nutrition_tracking_off(
        self, c: sqlite3.Connection, user_id: int, username: str,
        guild_id: int, macro: str,
    ) -> bool:
        """Switch a macro's tracker off by tombstoning every scope that
        currently sets a value. Returns False when there was nothing to stop.

        Every scope has to be tombstoned, not just ``default`` — a weekend
        override outranks the default rule, so nulling only the default would
        leave Saturdays still carrying a target.
        """
        live = targets.resolve(
            self._target_rows(c, user_id), targets.local_today(),
        )
        macro_target = (
            live.kcal if macro == targets.MACRO_KCAL else live.protein
        )
        if macro_target.value is None:
            return False
        scopes = {
            row["scope"] for row in c.execute(
                "SELECT DISTINCT scope FROM nutrition_targets "
                "WHERE user_id = ? AND macro = ? AND value IS NOT NULL",
                (user_id, macro),
            )
        }
        effective_from = self._target_effective_from(c, user_id, macro)
        for scope in scopes:
            self._nutrition_target_write(
                c, guild_id, user_id, username, macro, scope, None,
                effective_from,
            )
        return True

    def _tracked_users(
        self, guild_id: int, macro: str, day: date | None = None,
    ) -> list[dict]:
        """Members of this guild whose ``macro`` tracker is on for ``day``.

        Tracking is global (rules carry whichever server they were last set in),
        so membership is matched against the **members mirror** for this guild;
        otherwise someone who set their goal in another server would silently
        drop out of this guild's report. One query, grouped and resolved in
        Python — a guild has a handful of tracking members, not thousands.
        """
        day = day or targets.local_today()
        with self._conn() as c:
            rows = list(c.execute(
                "SELECT t.user_id, t.username, t.macro, t.scope, t.value, "
                "       t.effective_from, t.set_at "
                "FROM nutrition_targets t "
                "JOIN members m ON m.user_id = t.user_id AND m.guild_id = ? "
                "WHERE m.present = 1 AND t.macro = ? "
                "ORDER BY t.effective_from, t.set_at",
                (guild_id, macro),
            ))
        by_user: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            by_user.setdefault(int(row["user_id"]), []).append(row)

        field = (
            "daily_target_kcal" if macro == targets.MACRO_KCAL
            else "daily_target_g"
        )
        out: list[dict] = []
        for user_id, user_rows in by_user.items():
            resolved = targets.resolve(user_rows, day)
            macro_target = (
                resolved.kcal if macro == targets.MACRO_KCAL
                else resolved.protein
            )
            if macro_target.value is None:
                continue  # tracker switched off
            out.append({
                "user_id": user_id,
                "username": user_rows[-1]["username"],
                field: macro_target.value,
            })
        out.sort(key=lambda r: r["username"])
        return out

    # ------------------------------------------------------------------
    # Calorie tracking
    # ------------------------------------------------------------------

    def calorie_goal_set(
        self, guild_id: int, user_id: int, username: str,
        daily_target_kcal: float,
        weekend_kcal: float | None | _Keep = KEEP,
    ) -> None:
        """Set the user's all-week calorie target, and optionally a weekend one.

        ``daily_target_kcal`` is the number that applies to any day no more
        specific rule covers — so on its own it means the same target seven days
        a week, exactly as before per-day targets existed.

        ``weekend_kcal`` left at :data:`KEEP` leaves any existing weekend
        override alone (re-running setup to nudge the weekday number shouldn't
        quietly discard it); passing ``None`` clears it, and a number sets it.
        Tracking is **per-user / global** — the goal applies in every server and
        in DMs.
        """
        with self._conn() as c:
            effective_from = self._target_effective_from(
                c, user_id, targets.MACRO_KCAL,
            )
            self._nutrition_target_write(
                c, guild_id, user_id, username,
                targets.MACRO_KCAL, targets.SCOPE_DEFAULT, daily_target_kcal,
                effective_from,
            )
            detail = f"calorie target {float(daily_target_kcal):.0f} kcal/day"
            if weekend_kcal is not KEEP:
                self._nutrition_target_write(
                    c, guild_id, user_id, username,
                    targets.MACRO_KCAL, targets.SCOPE_WEEKEND, weekend_kcal,
                    effective_from,
                )
                detail += (
                    " (weekend override cleared)" if weekend_kcal is None
                    else f", weekends {float(weekend_kcal):.0f} kcal/day"
                )
            self._audit_data(
                c, guild_id, "calorie_goal_set",
                subject_id=user_id, subject_name=username, detail=detail,
            )

    def calorie_goal_get(
        self, guild_id: int, user_id: int, day: date | None = None,
    ) -> dict | None:
        """The user's calorie target on ``day`` (today when omitted), or None
        when they aren't tracking calories.

        Keeps the shape the ~40 existing call sites read — ``daily_target_kcal``,
        ``username``, ``set_at`` — and adds ``split``/``label`` for the surfaces
        that show which set is active. ``guild_id`` is ignored: targets resolve
        per-user so they apply in every server and in DMs. It stays in the
        signature because callers pass it.
        """
        day = day or targets.local_today()
        rows = self.nutrition_target_rows(user_id)
        resolved = targets.resolve(rows, day)
        if resolved.kcal.value is None:
            return None
        return {
            "daily_target_kcal": resolved.kcal.value,
            "username": rows[-1]["username"] if rows else "",
            "set_at": rows[-1]["set_at"] if rows else "",
            "split": resolved.kcal.split,
            "label": resolved.label_for(targets.MACRO_KCAL),
        }

    def calorie_goal_remove(self, guild_id: int, user_id: int) -> bool:
        """Stop tracking everywhere (tracking is global).

        Entry history is kept so re-enabling later still has the back data, and
        so are the target rules — a day that has already been logged still knows
        what it was aiming at, so old reports keep reading correctly. Today
        onward simply resolves to "no target".
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT username FROM nutrition_targets WHERE user_id = ? "
                "ORDER BY set_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            username = row["username"] if row else ""
            ok = self._nutrition_tracking_off(
                c, user_id, username, guild_id, targets.MACRO_KCAL,
            )
            if ok:
                self._audit_data(
                    c, guild_id, "calorie_goal_remove", subject_id=user_id,
                    detail="stopped calorie tracking",
                )
            return ok

    def calorie_tracked_users(
        self, guild_id: int, day: date | None = None,
    ) -> list[dict]:
        """Calorie-tracking members of this guild — the weekly AI summary
        iterates this list. ``daily_target_kcal`` is the target in force on
        ``day``; reports spanning a week re-resolve per day."""
        return self._tracked_users(guild_id, targets.MACRO_KCAL, day)

    def calorie_add(
        self, guild_id: int, user_id: int, username: str, kcal: float,
        note: str | None = None, raw: str | None = None,
        logged_at: datetime | None = None,
        message_id: int | None = None,
        actor_id: int | None = None,
        actor_name: str | None = None,
    ) -> int:
        """Insert one intake entry. Returns the new row id, or 0 if a row for
        this ``message_id`` already exists (dedupe, so backfill re-scans are
        safe). ``message_id`` is None for slash-command entries, which never
        dedupe. ``actor_id``/``actor_name`` credit the logger on proxy
        (``@user 650kcal``) entries; they default to the target."""
        act_id = actor_id if actor_id is not None else user_id
        act_name = actor_name if actor_name is not None else username
        with self._conn() as c:
            try:
                cur = c.execute(
                    """
                    INSERT INTO calorie_entries
                        (guild_id, user_id, username, kcal, note, raw,
                         message_id, logged_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        guild_id, user_id, username, float(kcal),
                        note, raw, message_id, _normalize_iso(logged_at),
                    ),
                )
            except sqlite3.IntegrityError:
                # Duplicate (message_id) — already logged this message.
                return 0
            new_id = int(cur.lastrowid or 0)
            if self.audit_live and new_id:
                self._audit(
                    c, guild_id, "data", "calorie_add",
                    actor_id=act_id, actor_name=act_name,
                    subject_id=user_id, subject_name=username,
                    detail=f"+{float(kcal):.0f} kcal"
                    + (f" ({note})" if note else ""),
                )
            return new_id

    def set_calorie_message_id(self, calorie_id: int, message_id: int) -> None:
        """Link an already-inserted intake entry to the bot reply that logged
        it, so a ❌ reaction on that reply can find and remove it. Used by
        slash commands, whose reply id isn't known until after the insert."""
        with self._conn() as c:
            c.execute(
                "UPDATE calorie_entries SET message_id = ? WHERE id = ?",
                (message_id, calorie_id),
            )

    def calorie_pop_last(
        self, guild_id: int, user_id: int,
        *, actor_id: int | None = None, actor_name: str | None = None,
    ) -> sqlite3.Row | None:
        """Delete the user's most recent intake entry and return it.

        Global: undoes the latest entry across **every** server (one shared
        diary), so a pop works regardless of where it was logged.
        ``guild_id`` is kept for the audit record but not used to scope the
        lookup."""
        with self._conn() as c:
            row = c.execute(
                """
                SELECT id, kcal, note, raw, logged_at
                FROM calorie_entries
                WHERE user_id = ?
                ORDER BY logged_at DESC, id DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            c.execute("DELETE FROM calorie_entries WHERE id = ?", (row["id"],))
            self._audit_data(
                c, guild_id, "calorie_undo", subject_id=user_id,
                actor_id=actor_id, actor_name=actor_name,
                detail=f"undid {float(row['kcal']):.0f} kcal",
            )
            return row

    def calorie_update_last(
        self, guild_id: int, user_id: int, kcal: float,
        *, note: str | None = None, raw: str | None = None,
        username: str | None = None,
    ) -> sqlite3.Row | None:
        """Overwrite the user's most recent calorie entry's amount (global).

        Returns the *old* row (id, kcal, note, logged_at) so the caller can show
        the change and know which day it landed on, or None when there's nothing
        to edit. A new ``note`` replaces the old; ``None`` keeps it."""
        with self._conn() as c:
            row = c.execute(
                "SELECT id, kcal, note, logged_at FROM calorie_entries "
                "WHERE user_id = ? "
                "ORDER BY logged_at DESC, id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            c.execute(
                "UPDATE calorie_entries SET kcal = ?, "
                "note = COALESCE(?, note), raw = COALESCE(?, raw) WHERE id = ?",
                (float(kcal), note, raw, row["id"]),
            )
            self._audit_data(
                c, guild_id, "calorie_edit", subject_id=user_id,
                actor_id=user_id, actor_name=username,
                detail=f"edited {float(row['kcal']):.0f} → {float(kcal):.0f} kcal",
            )
            return row

    def track_calorie_reply(
        self,
        reply_message_id: int,
        guild_id: int,
        user_id: int,
        target_user_id: int,
        calorie_id: int,
        original_message_id: int | None = None,
    ) -> None:
        """Record that ``reply_message_id`` is the bot reply for one calorie
        entry, so a ❌ reaction on it can remove that specific entry."""
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO calorie_reply_tracking
                    (reply_message_id, guild_id, user_id, target_user_id,
                     calorie_id, original_message_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reply_message_id, guild_id, user_id, target_user_id,
                    calorie_id, original_message_id, _normalize_iso(None),
                ),
            )

    def track_nutrition_reply(
        self,
        reply_message_id: int,
        channel_id: int,
        target_user_id: int,
        *,
        calorie_id: int = 0,
        protein_id: int = 0,
        headline: str | None = None,
        footnote: str | None = None,
    ) -> None:
        """Remember enough about a posted nutrition reply to restate it.

        Called for every confirmation the bot posts — card or text, chat or
        slash — so that when an earlier entry changes, the later replies can
        have their running totals corrected instead of being left wrong.
        """
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO nutrition_reply_render
                    (reply_message_id, channel_id, target_user_id, calorie_id,
                     protein_id, headline, footnote, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reply_message_id, channel_id, target_user_id,
                    int(calorie_id or 0), int(protein_id or 0),
                    headline, footnote, _normalize_iso(None),
                ),
            )

    def forget_nutrition_reply(self, reply_message_id: int) -> None:
        """Drop a render row — the reply is gone, or has been struck through."""
        with self._conn() as c:
            c.execute(
                "DELETE FROM nutrition_reply_render WHERE reply_message_id = ?",
                (reply_message_id,),
            )

    def nutrition_replies_for_day(
        self, target_user_id: int, start_iso: str, end_iso: str,
    ) -> list[sqlite3.Row]:
        """Tracked replies whose entry falls in [start_iso, end_iso), oldest
        first — the messages an earlier change to that day makes stale.

        A reply whose entries have *both* been deleted drops out on its own:
        the join finds no timestamp, so it fails the range test. That's the
        behaviour we want, since the undo path has already struck that one
        through and restating it would undo the strike.
        """
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT r.*, COALESCE(ce.logged_at, pe.logged_at) AS entry_at
                FROM nutrition_reply_render r
                LEFT JOIN calorie_entries ce ON ce.id = r.calorie_id
                LEFT JOIN protein_entries pe ON pe.id = r.protein_id
                WHERE r.target_user_id = ?
                  AND COALESCE(ce.logged_at, pe.logged_at) >= ?
                  AND COALESCE(ce.logged_at, pe.logged_at) <  ?
                ORDER BY entry_at ASC, r.reply_message_id ASC
                """,
                (target_user_id, start_iso, end_iso),
            ))

    def get_calorie_reply(self, reply_message_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM calorie_reply_tracking WHERE reply_message_id = ?",
                (reply_message_id,),
            ).fetchone()

    def delete_calorie_reply(self, reply_message_id: int) -> int:
        """Delete a calorie reply-tracking row. Returns rowcount so concurrent
        ❌ reactions race-protect (only the first delete returns 1)."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM calorie_reply_tracking WHERE reply_message_id = ?",
                (reply_message_id,),
            )
            return cur.rowcount or 0

    def get_calorie_entry_by_message(
        self, guild_id: int, message_id: int,
    ) -> sqlite3.Row | None:
        """The intake entry created from a given source chat message, or None.

        Lets ❌ reaction-undo work on *legacy* logs (made before reply-tracking
        existed) by following the bot reply's reference back to the original
        message. message_id is globally unique per entry (partial unique index),
        so a ``guild_id`` of 0 — a ❌ reaction in a DM, where the gateway gives
        no guild id — matches regardless of the guild the entry was stored
        under; a non-zero ``guild_id`` still scopes strictly as before."""
        with self._conn() as c:
            return c.execute(
                "SELECT id, user_id, kcal, note, logged_at FROM calorie_entries "
                "WHERE message_id = ? AND (? = 0 OR guild_id = ?)",
                (message_id, guild_id, guild_id),
            ).fetchone()

    def update_calorie_entry(
        self, calorie_id: int, kcal: float,
        note: str | None = None, raw: str | None = None,
    ) -> None:
        """Update an entry in place — used when the source message is edited
        (e.g. a `1730c` typo corrected to `1730kj`)."""
        with self._conn() as c:
            c.execute(
                "UPDATE calorie_entries SET kcal = ?, note = ?, raw = ? "
                "WHERE id = ?",
                (float(kcal), note, raw, calorie_id),
            )

    def get_calorie_reply_by_original(
        self, original_message_id: int,
    ) -> sqlite3.Row | None:
        """Find the bot's reply-tracking row for a given source message, so an
        edit can refresh the reply text it posted."""
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM calorie_reply_tracking "
                "WHERE original_message_id = ?",
                (original_message_id,),
            ).fetchone()

    def delete_calorie_entry(
        self, guild_id: int, target_user_id: int, calorie_id: int,
        *, actor_id: int | None = None, actor_name: str | None = None,
    ) -> sqlite3.Row | None:
        """Delete one intake entry by id, scoped to (guild, user) for safety.
        Returns the deleted row (kcal/note/logged_at) or None if it was already
        gone. ``logged_at`` comes back so the caller knows which day's other
        confirmations now show a stale running total."""
        with self._conn() as c:
            row = c.execute(
                "SELECT id, kcal, note, logged_at FROM calorie_entries "
                "WHERE id = ? AND guild_id = ? AND user_id = ?",
                (calorie_id, guild_id, target_user_id),
            ).fetchone()
            if row is None:
                return None
            c.execute("DELETE FROM calorie_entries WHERE id = ?", (calorie_id,))
            self._audit_data(
                c, guild_id, "calorie_undo", subject_id=target_user_id,
                actor_id=actor_id, actor_name=actor_name,
                detail=f"undid {float(row['kcal']):.0f} kcal",
            )
            return row

    def calorie_entries_between(
        self, guild_id: int, user_id: int, start_iso: str, end_iso: str,
    ) -> list[sqlite3.Row]:
        """All intake entries in [start_iso, end_iso), oldest first. Day
        bucketing happens in the caller against DISPLAY_TIMEZONE — the
        stored timestamps are UTC and the local day boundary isn't
        substr-able here.

        Tracking is global, so this returns the user's entries from **every
        server** (one shared diary). ``guild_id`` is accepted for signature
        compatibility but intentionally not filtered on."""
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT id, kcal, note, raw, logged_at
                FROM calorie_entries
                WHERE user_id = ?
                  AND logged_at >= ? AND logged_at < ?
                ORDER BY logged_at ASC, id ASC
                """,
                (user_id, start_iso, end_iso),
            ))

    def calorie_logged_days(
        self, guild_id: int, user_id: int, start_iso: str, end_iso: str,
    ) -> list[str]:
        """Distinct calendar dates (YYYY-MM-DD) with a calorie entry in the
        window. Lets callers tell 'logged 0' from 'didn't track'.

        Global: counts a day as logged if an entry was made in **any** server.
        ``guild_id`` is kept for signature compatibility but not filtered on."""
        with self._conn() as c:
            return [r[0] for r in c.execute(
                "SELECT DISTINCT substr(logged_at, 1, 10) FROM calorie_entries "
                "WHERE user_id = ? "
                "AND logged_at >= ? AND logged_at < ?",
                (user_id, start_iso, end_iso),
            )]

    def calorie_total_between(
        self, guild_id: int, user_id: int, start_iso: str, end_iso: str,
    ) -> tuple[float, int]:
        """Sum of kcal and entry count in [start_iso, end_iso).

        Tracking is global, so this aggregates the user's intake across **every
        server** (a calorie logged in any server / DM counts toward the same
        daily total). ``guild_id`` is accepted for signature compatibility but
        intentionally not filtered on."""
        with self._conn() as c:
            row = c.execute(
                """
                SELECT COALESCE(SUM(kcal), 0) AS total, COUNT(*) AS n
                FROM calorie_entries
                WHERE user_id = ?
                  AND logged_at >= ? AND logged_at < ?
                """,
                (user_id, start_iso, end_iso),
            ).fetchone()
            return float(row["total"] or 0.0), int(row["n"] or 0)

    # ---- protein (grams) -------------------------------------------------

    def _protein_goal_write(
        self, c: sqlite3.Connection, guild_id: int, user_id: int, username: str,
        daily_target_g: float,
        weekend_g: float | None | _Keep = KEEP,
    ) -> str:
        """Write the effective-dated protein target rows and return the audit
        detail string. The all-week ``default`` row always, plus the ``weekend``
        override when ``weekend_g`` isn't :data:`KEEP` (``None`` tombstones it).

        Split out from :meth:`protein_goal_set` so the bodyweight-link
        auto-update in :meth:`set_bodyweight` re-derives a target through the
        exact same row-writing convention (one ``_target_effective_from`` call,
        history-preserving new rows) instead of duplicating it. Takes an already
        open connection so the caller controls the transaction and can audit
        with its own context.
        """
        effective_from = self._target_effective_from(
            c, user_id, targets.MACRO_PROTEIN,
        )
        self._nutrition_target_write(
            c, guild_id, user_id, username,
            targets.MACRO_PROTEIN, targets.SCOPE_DEFAULT, daily_target_g,
            effective_from,
        )
        detail = f"protein max {float(daily_target_g):.0f} g/day"
        if weekend_g is not KEEP:
            self._nutrition_target_write(
                c, guild_id, user_id, username,
                targets.MACRO_PROTEIN, targets.SCOPE_WEEKEND, weekend_g,
                effective_from,
            )
            detail += (
                " (weekend override cleared)" if weekend_g is None
                else f", weekends {float(weekend_g):.0f} g/day"
            )
        return detail

    def protein_goal_set(
        self, guild_id: int, user_id: int, username: str,
        daily_target_g: float,
        weekend_g: float | None | _Keep = KEEP,
    ) -> None:
        """Set the user's all-week protein ceiling (grams), optionally with a
        separate weekend one.

        ``daily_target_g`` covers any day no more specific rule matches, so on
        its own it means the same ceiling seven days a week. ``weekend_g`` at
        :data:`KEEP` leaves an existing weekend override alone, ``None`` clears
        it, a number sets it. Per-user / global, like its calorie twin."""
        with self._conn() as c:
            detail = self._protein_goal_write(
                c, guild_id, user_id, username, daily_target_g, weekend_g,
            )
            self._audit_data(
                c, guild_id, "protein_goal_set",
                subject_id=user_id, subject_name=username, detail=detail,
            )

    # ---- protein <-> bodyweight link ------------------------------------

    def protein_bw_link_set(
        self, user_id: int, username: str | None = None,
        grams_per_kg: float = 1.0,
    ) -> None:
        """Tie this user's protein ceiling to their bodyweight at
        ``grams_per_kg`` g/kg (upsert). Recording the link does *not* itself
        write a target row — the caller sets the initial target and every later
        :meth:`set_bodyweight` re-derives it. Global per user."""
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO protein_bw_links
                    (user_id, grams_per_kg, username, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (user_id) DO UPDATE SET
                    grams_per_kg = excluded.grams_per_kg,
                    username     = excluded.username
                """,
                (user_id, float(grams_per_kg), username, _normalize_iso(None)),
            )

    def protein_bw_link_get(self, user_id: int) -> sqlite3.Row | None:
        """The user's protein<->bodyweight link row, or None when not linked."""
        with self._conn() as c:
            return c.execute(
                "SELECT user_id, grams_per_kg, username, created_at "
                "FROM protein_bw_links WHERE user_id = ?",
                (user_id,),
            ).fetchone()

    def protein_bw_link_remove(self, user_id: int) -> bool:
        """Drop the link. Returns True when one existed (so callers can say so
        in their reply). Leaves the protein target itself in place — unlinking
        just stops future weigh-ins from moving it."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM protein_bw_links WHERE user_id = ?", (user_id,),
            )
            return cur.rowcount > 0

    def protein_goal_get(
        self, guild_id: int, user_id: int, day: date | None = None,
    ) -> dict | None:
        """The user's protein ceiling on ``day`` (today when omitted), or None
        when they aren't tracking protein.

        Same shape as :meth:`calorie_goal_get`: the ``daily_target_g`` key every
        existing call site reads, plus ``split``/``label`` for surfaces that show
        which set is active."""
        day = day or targets.local_today()
        rows = self.nutrition_target_rows(user_id)
        resolved = targets.resolve(rows, day)
        if resolved.protein.value is None:
            return None
        return {
            "daily_target_g": resolved.protein.value,
            "username": rows[-1]["username"] if rows else "",
            "set_at": rows[-1]["set_at"] if rows else "",
            "split": resolved.protein.split,
            "label": resolved.label_for(targets.MACRO_PROTEIN),
        }

    def protein_goal_remove(self, guild_id: int, user_id: int) -> bool:
        """Stop protein tracking everywhere (global); logged history and the
        target rules behind it are kept, so old reports still read correctly."""
        with self._conn() as c:
            row = c.execute(
                "SELECT username FROM nutrition_targets WHERE user_id = ? "
                "ORDER BY set_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            username = row["username"] if row else ""
            ok = self._nutrition_tracking_off(
                c, user_id, username, guild_id, targets.MACRO_PROTEIN,
            )
            if ok:
                self._audit_data(
                    c, guild_id, "protein_goal_remove", subject_id=user_id,
                    detail="stopped protein tracking",
                )
            return ok

    def protein_tracked_users(
        self, guild_id: int, day: date | None = None,
    ) -> list[dict]:
        """Protein-tracking members of this guild. Like its calorie twin,
        tracking is global so membership is matched against the members mirror
        for this guild rather than the rule's stored ``guild_id``."""
        return self._tracked_users(guild_id, targets.MACRO_PROTEIN, day)

    def protein_add(
        self, guild_id: int, user_id: int, username: str, grams: float,
        note: str | None = None, raw: str | None = None,
        logged_at: datetime | None = None,
        message_id: int | None = None,
        actor_id: int | None = None,
        actor_name: str | None = None,
    ) -> int:
        """Insert one protein entry. Returns the new row id, or 0 if a row for
        this ``message_id`` already exists (dedupe). ``actor_id``/``actor_name``
        credit the logger on proxy entries; they default to the target."""
        act_id = actor_id if actor_id is not None else user_id
        act_name = actor_name if actor_name is not None else username
        with self._conn() as c:
            try:
                cur = c.execute(
                    """
                    INSERT INTO protein_entries
                        (guild_id, user_id, username, grams, note, raw,
                         message_id, logged_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        guild_id, user_id, username, float(grams),
                        note, raw, message_id, _normalize_iso(logged_at),
                    ),
                )
            except sqlite3.IntegrityError:
                return 0
            new_id = int(cur.lastrowid or 0)
            if self.audit_live and new_id:
                self._audit(
                    c, guild_id, "data", "protein_add",
                    actor_id=act_id, actor_name=act_name,
                    subject_id=user_id, subject_name=username,
                    detail=f"+{float(grams):.0f} g"
                    + (f" ({note})" if note else ""),
                )
            return new_id

    def set_protein_message_id(self, protein_id: int, message_id: int) -> None:
        """Link an already-inserted protein entry to the bot reply that logged
        it (slash-command counterpart to reply-tracking), so a ❌ reaction on
        that reply removes it."""
        with self._conn() as c:
            c.execute(
                "UPDATE protein_entries SET message_id = ? WHERE id = ?",
                (message_id, protein_id),
            )

    def protein_pop_last(
        self, guild_id: int, user_id: int,
        *, actor_id: int | None = None, actor_name: str | None = None,
    ) -> sqlite3.Row | None:
        """Delete the user's most recent protein entry and return it.

        Global: undoes the latest entry across **every** server. ``guild_id``
        is kept for the audit record but not used to scope the lookup."""
        with self._conn() as c:
            row = c.execute(
                """
                SELECT id, grams, note, raw, logged_at
                FROM protein_entries
                WHERE user_id = ?
                ORDER BY logged_at DESC, id DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            c.execute("DELETE FROM protein_entries WHERE id = ?", (row["id"],))
            self._audit_data(
                c, guild_id, "protein_undo", subject_id=user_id,
                actor_id=actor_id, actor_name=actor_name,
                detail=f"undid {float(row['grams']):.0f} g protein",
            )
            return row

    def protein_update_last(
        self, guild_id: int, user_id: int, grams: float,
        *, note: str | None = None, raw: str | None = None,
        username: str | None = None,
    ) -> sqlite3.Row | None:
        """Overwrite the user's most recent protein entry's amount (global).
        Returns the *old* row (incl. ``logged_at``, so the caller knows which
        day to restate) or None when there's nothing to edit."""
        with self._conn() as c:
            row = c.execute(
                "SELECT id, grams, note, logged_at FROM protein_entries "
                "WHERE user_id = ? "
                "ORDER BY logged_at DESC, id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            c.execute(
                "UPDATE protein_entries SET grams = ?, "
                "note = COALESCE(?, note), raw = COALESCE(?, raw) WHERE id = ?",
                (float(grams), note, raw, row["id"]),
            )
            self._audit_data(
                c, guild_id, "protein_edit", subject_id=user_id,
                actor_id=user_id, actor_name=username,
                detail=f"edited {float(row['grams']):.0f} → {float(grams):.0f} g",
            )
            return row

    def protein_logged_days(
        self, guild_id: int, user_id: int, start_iso: str, end_iso: str,
    ) -> list[str]:
        """Distinct calendar dates (YYYY-MM-DD) with a protein entry in window.

        Global: counts a day as logged if an entry was made in **any** server.
        ``guild_id`` is kept for signature compatibility but not filtered on."""
        with self._conn() as c:
            return [r[0] for r in c.execute(
                "SELECT DISTINCT substr(logged_at, 1, 10) FROM protein_entries "
                "WHERE user_id = ? "
                "AND logged_at >= ? AND logged_at < ?",
                (user_id, start_iso, end_iso),
            )]

    def protein_total_between(
        self, guild_id: int, user_id: int, start_iso: str, end_iso: str,
    ) -> tuple[float, int]:
        """Sum of grams and entry count in [start_iso, end_iso).

        Aggregated across **every server** (tracking is global). ``guild_id`` is
        accepted for signature compatibility but intentionally not filtered on."""
        with self._conn() as c:
            row = c.execute(
                """
                SELECT COALESCE(SUM(grams), 0) AS total, COUNT(*) AS n
                FROM protein_entries
                WHERE user_id = ?
                  AND logged_at >= ? AND logged_at < ?
                """,
                (user_id, start_iso, end_iso),
            ).fetchone()
            return float(row["total"] or 0.0), int(row["n"] or 0)

    def get_protein_entry_by_message(
        self, guild_id: int, message_id: int,
    ) -> sqlite3.Row | None:
        """The protein entry created from a given source chat message, or None
        (used by ❌ reaction-undo on protein/combined replies). ``guild_id`` of
        0 (a ❌ reaction in a DM) matches on the globally-unique message_id
        regardless of guild; a non-zero ``guild_id`` scopes strictly."""
        with self._conn() as c:
            return c.execute(
                "SELECT id, user_id, grams, note FROM protein_entries "
                "WHERE message_id = ? AND (? = 0 OR guild_id = ?)",
                (message_id, guild_id, guild_id),
            ).fetchone()

    def delete_protein_entry(
        self, guild_id: int, target_user_id: int, protein_id: int,
        *, actor_id: int | None = None, actor_name: str | None = None,
    ) -> sqlite3.Row | None:
        """Delete one protein entry by id, scoped to (guild, user). Returns the
        deleted row (incl. ``logged_at``, so the caller can restate that day's
        other confirmations) or None if already gone."""
        with self._conn() as c:
            row = c.execute(
                "SELECT id, grams, note, logged_at FROM protein_entries "
                "WHERE id = ? AND guild_id = ? AND user_id = ?",
                (protein_id, guild_id, target_user_id),
            ).fetchone()
            if row is None:
                return None
            c.execute("DELETE FROM protein_entries WHERE id = ?", (protein_id,))
            self._audit_data(
                c, guild_id, "protein_undo", subject_id=target_user_id,
                actor_id=actor_id, actor_name=actor_name,
                detail=f"undid {float(row['grams']):.0f} g protein",
            )
            return row

    def protein_entries_between(
        self, guild_id: int, user_id: int, start_iso: str, end_iso: str,
    ) -> list[sqlite3.Row]:
        """All protein entries in [start_iso, end_iso), oldest first.

        Global: returns the user's entries from **every** server (one shared
        diary). ``guild_id`` is kept for signature compatibility but not
        filtered on."""
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT id, grams, note, raw, logged_at
                FROM protein_entries
                WHERE user_id = ?
                  AND logged_at >= ? AND logged_at < ?
                ORDER BY logged_at, id
                """,
                (user_id, start_iso, end_iso),
            ))

    # ---- saved foods -----------------------------------------------------

    def calorie_food_set(
        self, guild_id: int, user_id: int, name: str, display: str,
        kcal: float, protein_g: float | None = None,
    ) -> None:
        """Create or update a saved food shortcut. ``name`` must already be
        normalized (lowercased/whitespace-collapsed).

        ``protein_g`` is optional grams-of-protein per serving. On an update,
        passing ``None`` *preserves* any protein already stored (so re-saving a
        food with only a new calorie amount doesn't wipe its protein); pass a
        number — including ``0`` — to set it explicitly.

        Saved foods are **per-user and shared across every server + DMs**: setting
        a food consolidates it to a single row (any copy under another guild is
        cleared first), so it resolves the same everywhere.
        """
        with self._conn() as c:
            if protein_g is None:
                # Preserve protein across guilds (the prior copy may live under a
                # different server we're about to consolidate away).
                prev = c.execute(
                    "SELECT protein_g FROM calorie_foods "
                    "WHERE user_id = ? AND name = ? AND protein_g IS NOT NULL "
                    "ORDER BY set_at DESC LIMIT 1",
                    (user_id, name),
                ).fetchone()
                if prev is not None:
                    protein_g = prev["protein_g"]
            c.execute(
                "DELETE FROM calorie_foods "
                "WHERE user_id = ? AND name = ? AND guild_id <> ?",
                (user_id, name, guild_id),
            )
            c.execute(
                """
                INSERT INTO calorie_foods
                    (guild_id, user_id, name, display, kcal, protein_g, set_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (guild_id, user_id, name) DO UPDATE SET
                    display   = excluded.display,
                    kcal      = excluded.kcal,
                    protein_g = COALESCE(excluded.protein_g, calorie_foods.protein_g),
                    set_at    = excluded.set_at
                """,
                (
                    guild_id, user_id, name, display, float(kcal),
                    None if protein_g is None else float(protein_g),
                    _normalize_iso(None),
                ),
            )

    def calorie_food_get(
        self, guild_id: int, user_id: int, name: str,
    ) -> sqlite3.Row | None:
        """A saved food, resolved **per-user** so one set in any server (or via
        DM) is found everywhere. Prefers the current guild's copy, else the most
        recently saved.

        Falls back to the user's alias table, then to a plural/singular swap, so
        a food saved as "boneless wicked wing" still answers to "boneless wicked
        wings" without anyone configuring it.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT name, display, kcal, protein_g FROM calorie_foods "
                "WHERE user_id = ? AND name = ? "
                "ORDER BY (guild_id = ?) DESC, set_at DESC LIMIT 1",
                (user_id, name, guild_id),
            ).fetchone()
            if row is not None:
                return row
            for candidate in self._food_name_candidates(c, user_id, name):
                row = c.execute(
                    "SELECT name, display, kcal, protein_g FROM calorie_foods "
                    "WHERE user_id = ? AND name = ? "
                    "ORDER BY (guild_id = ?) DESC, set_at DESC LIMIT 1",
                    (user_id, candidate, guild_id),
                ).fetchone()
                if row is not None:
                    return row
        return None

    @staticmethod
    def _food_name_candidates(
        c: sqlite3.Connection, user_id: int, name: str,
    ) -> list[str]:
        """Other names to try for *name*, best first.

        An explicit alias always wins over the plural guess — the guess is a
        convenience, the alias is the user's stated intent.
        """
        out: list[str] = []
        alias = c.execute(
            "SELECT name FROM calorie_food_aliases "
            "WHERE user_id = ? AND alias = ?",
            (user_id, name),
        ).fetchone()
        if alias is not None:
            out.append(alias[0])
        # Cheap English plural swap, both directions. "wings"→"wing" covers the
        # common case; "y"→"ies" is deliberately not attempted (too many false
        # friends for food names).
        if name.endswith("es") and len(name) > 3:
            out.append(name[:-2])
        if name.endswith("s") and not name.endswith("ss") and len(name) > 2:
            out.append(name[:-1])
        else:
            out.append(name + "s")
        return [n for n in out if n and n != name]

    def calorie_food_alias_set(
        self, user_id: int, alias: str, name: str,
    ) -> None:
        """Point ``alias`` at the saved food ``name`` (both normalized)."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO calorie_food_aliases (user_id, alias, name, set_at)"
                " VALUES (?, ?, ?, ?) ON CONFLICT(user_id, alias) DO UPDATE SET"
                " name = excluded.name, set_at = excluded.set_at",
                (user_id, alias, name, _normalize_iso(None)),
            )

    def calorie_food_alias_remove(self, user_id: int, alias: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM calorie_food_aliases WHERE user_id = ? AND alias = ?",
                (user_id, alias),
            )
            return (cur.rowcount or 0) > 0

    def calorie_food_aliases(self, user_id: int) -> list[sqlite3.Row]:
        """Every alias the user has, alphabetical."""
        with self._conn() as c:
            return list(c.execute(
                "SELECT alias, name FROM calorie_food_aliases "
                "WHERE user_id = ? ORDER BY name, alias",
                (user_id,),
            ))

    def calorie_food_remove(
        self, guild_id: int, user_id: int, name: str,
    ) -> bool:
        """Remove a saved food for the user **everywhere** (foods are shared
        across servers, so deletion isn't guild-scoped).

        Aliases pointing at it go too — a shortcut that resolves to a food that
        no longer exists would silently stop logging with nothing to show why.
        """
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM calorie_foods WHERE user_id = ? AND name = ?",
                (user_id, name),
            )
            c.execute(
                "DELETE FROM calorie_food_aliases WHERE user_id = ? AND name = ?",
                (user_id, name),
            )
            return (cur.rowcount or 0) > 0

    def calorie_food_list(
        self, guild_id: int, user_id: int,
    ) -> list[sqlite3.Row]:
        """All of a user's saved foods (shared across servers), one row per name
        — the current guild's copy wins, else the most recent."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT name, display, kcal, protein_g FROM calorie_foods "
                "WHERE user_id = ? "
                "ORDER BY (guild_id = ?) DESC, set_at DESC",
                (user_id, guild_id),
            ).fetchall()
        seen: dict[str, sqlite3.Row] = {}
        for r in rows:
            seen.setdefault(r["name"], r)
        return sorted(seen.values(), key=lambda r: (r["display"] or "").lower())

    # ---- saved meals (bundles of saved foods) ------------------------------

    def calorie_meal_set(
        self, user_id: int, name: str, display: str,
        items: list[tuple[int, str]],
    ) -> None:
        """Create or update a saved meal. ``name`` must already be normalized;
        ``items`` is [(servings, normalized_food_name), ...] — validated by
        the caller against the user's saved foods. Global per user."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO calorie_meals (user_id, name, display, items, set_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (user_id, name) DO UPDATE SET "
                "display = excluded.display, items = excluded.items, "
                "set_at = excluded.set_at",
                (
                    user_id, name, display,
                    json.dumps([[int(n), s] for n, s in items]),
                    _normalize_iso(None),
                ),
            )

    def calorie_meal_get(
        self, user_id: int, name: str,
    ) -> tuple[str, list[tuple[int, str]]] | None:
        """Return ``(display, items)`` for a saved meal, or None. Items whose
        JSON doesn't decode cleanly are dropped rather than crashing the
        caller (the row can only get that way via manual DB edits)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT display, items FROM calorie_meals "
                "WHERE user_id = ? AND name = ?",
                (user_id, name),
            ).fetchone()
        if row is None:
            return None
        try:
            raw = json.loads(row["items"])
            items = [
                (int(pair[0]), str(pair[1]))
                for pair in raw
                if isinstance(pair, (list, tuple)) and len(pair) == 2
            ]
        except (ValueError, TypeError):
            return None
        return (row["display"], items) if items else None

    def calorie_meal_remove(self, user_id: int, name: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM calorie_meals WHERE user_id = ? AND name = ?",
                (user_id, name),
            )
            return (cur.rowcount or 0) > 0

    def calorie_meal_list(self, user_id: int) -> list[sqlite3.Row]:
        """All of a user's saved meals, alphabetical by display name."""
        with self._conn() as c:
            return c.execute(
                "SELECT name, display, items FROM calorie_meals "
                "WHERE user_id = ? ORDER BY LOWER(display)",
                (user_id,),
            ).fetchall()

    # ---- streak-saver reminder prefs ---------------------------------------

    def calorie_reminder_set(
        self, user_id: int, hour: int, minute: int = 0,
    ) -> None:
        """Opt a user into the evening streak-saver DM at hour:minute local
        (DISPLAY_TIMEZONE). Re-setting updates the time and re-arms today."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO calorie_reminder_prefs "
                "(user_id, hour, minute, last_sent, updated_at) "
                "VALUES (?, ?, ?, NULL, ?) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "hour = excluded.hour, minute = excluded.minute, "
                "last_sent = NULL, updated_at = excluded.updated_at",
                (user_id, int(hour), int(minute), _normalize_iso(None)),
            )

    def calorie_reminder_get(self, user_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                "SELECT user_id, hour, minute, last_sent "
                "FROM calorie_reminder_prefs WHERE user_id = ?",
                (user_id,),
            ).fetchone()

    def calorie_reminder_remove(self, user_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM calorie_reminder_prefs WHERE user_id = ?",
                (user_id,),
            )
            return (cur.rowcount or 0) > 0

    def calorie_reminder_list(self) -> list[sqlite3.Row]:
        """Everyone opted into the streak-saver, for the reminder loop."""
        with self._conn() as c:
            return c.execute(
                "SELECT user_id, hour, minute, last_sent "
                "FROM calorie_reminder_prefs",
            ).fetchall()

    def calorie_reminder_mark_sent(self, user_id: int, day: str) -> None:
        """Record that today's nudge went out (``day`` is local YYYY-MM-DD)."""
        with self._conn() as c:
            c.execute(
                "UPDATE calorie_reminder_prefs SET last_sent = ? "
                "WHERE user_id = ?",
                (day, user_id),
            )

    # ---- bodyweight goals ---------------------------------------------------

    def bodyweight_goal_set(
        self, user_id: int, username: str, target_kg: float,
    ) -> None:
        """Set (or move) a user's bodyweight target. Global like bodyweights."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO bodyweight_goals (user_id, username, target_kg, set_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "username = excluded.username, target_kg = excluded.target_kg, "
                "set_at = excluded.set_at",
                (user_id, username, float(target_kg), _normalize_iso(None)),
            )

    def bodyweight_goal_get(self, user_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                "SELECT user_id, username, target_kg, set_at "
                "FROM bodyweight_goals WHERE user_id = ?",
                (user_id,),
            ).fetchone()

    def bodyweight_goal_remove(self, user_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM bodyweight_goals WHERE user_id = ?",
                (user_id,),
            )
            return (cur.rowcount or 0) > 0

    # ====================================================================
    # Web dashboard: audit log, member/role mirror, and editing helpers.
    # ====================================================================

    def _audit(
        self,
        c: sqlite3.Connection,
        guild_id: int,
        category: str,
        action: str,
        *,
        actor_id: int | None = None,
        actor_name: str | None = None,
        subject_id: int | None = None,
        subject_name: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Insert one audit row using an existing connection/transaction.

        Defensive by design: a failure to write an audit row must never break
        the real mutation it accompanies, so all errors are swallowed.
        """
        try:
            c.execute(
                """
                INSERT INTO audit_log
                    (guild_id, at, category, action, actor_id, actor_name,
                     subject_id, subject_name, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id, _normalize_iso(None), category, action,
                    actor_id, actor_name, subject_id, subject_name, detail,
                ),
            )
        except sqlite3.Error:  # pragma: no cover - audit is best-effort
            pass

    def add_audit(
        self,
        guild_id: int,
        category: str,
        action: str,
        *,
        actor_id: int | None = None,
        actor_name: str | None = None,
        subject_id: int | None = None,
        subject_name: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Public, self-contained audit insert (its own transaction). Used by
        the bot's gateway-event handlers and the web dashboard's edits."""
        with self._conn() as c:
            self._audit(
                c, guild_id, category, action,
                actor_id=actor_id, actor_name=actor_name,
                subject_id=subject_id, subject_name=subject_name,
                detail=detail,
            )

    def _audit_data(
        self,
        c: sqlite3.Connection,
        guild_id: int,
        action: str,
        *,
        subject_id: int | None = None,
        subject_name: str | None = None,
        detail: str | None = None,
        actor_id: int | None = None,
        actor_name: str | None = None,
    ) -> None:
        """Audit a ``data`` mutation, but only once live (so the startup
        backfill doesn't flood the log). Centralises the ``audit_live`` gate
        used by every lift/calorie/protein/goal/bodyweight write."""
        if self.audit_live:
            self._audit(
                c, guild_id, "data", action,
                actor_id=actor_id, actor_name=actor_name,
                subject_id=subject_id, subject_name=subject_name, detail=detail,
            )

    def list_audit(
        self,
        guild_id: int,
        *,
        category: str | None = None,
        subject_id: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        """Most-recent-first slice of the audit log, optionally filtered by
        category and/or subject user. Left-joins the member mirror so each row
        carries the subject's avatar for the dashboard."""
        sql = (
            "SELECT a.id, a.guild_id, a.at, a.category, a.action, "
            "       a.actor_id, "
            "       COALESCE(a.actor_name, am.display_name) AS actor_name, "
            "       a.subject_id, "
            "       COALESCE(a.subject_name, m.display_name) AS subject_name, "
            "       a.detail, m.avatar AS subject_avatar "
            "FROM audit_log a "
            "LEFT JOIN members m "
            "  ON m.guild_id = a.guild_id AND m.user_id = a.subject_id "
            "LEFT JOIN members am "
            "  ON am.guild_id = a.guild_id AND am.user_id = a.actor_id "
            "WHERE a.guild_id = ?"
        )
        params: list[object] = [guild_id]
        if category:
            sql += " AND a.category = ?"
            params.append(category)
        if subject_id is not None:
            sql += " AND a.subject_id = ?"
            params.append(subject_id)
        sql += " ORDER BY a.at DESC, a.id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._conn() as c:
            return list(c.execute(sql, params))

    def count_audit(
        self,
        guild_id: int,
        *,
        category: str | None = None,
        subject_id: int | None = None,
    ) -> int:
        sql = "SELECT COUNT(*) FROM audit_log WHERE guild_id = ?"
        params: list[object] = [guild_id]
        if category:
            sql += " AND category = ?"
            params.append(category)
        if subject_id is not None:
            sql += " AND subject_id = ?"
            params.append(subject_id)
        with self._conn() as c:
            row = c.execute(sql, params).fetchone()
            return int(row[0]) if row else 0

    # ---- global key/value bookkeeping -----------------------------------

    def meta_get(self, key: str) -> str | None:
        """Read a value from the global ``app_meta`` kv store, or None."""
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM app_meta WHERE key = ?", (key,),
            ).fetchone()
            return row["value"] if row else None

    def meta_set(self, key: str, value: str) -> None:
        """Upsert a value into the global ``app_meta`` kv store."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO app_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ---- remembered web-dashboard sessions -----------------------------

    def web_session_add(
        self, token_hash: str, expires_at: datetime,
    ) -> None:
        """Persist a remembered dashboard token digest and absolute expiry."""
        now = _normalize_iso(None)
        expiry = _normalize_iso(expires_at)
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO web_sessions "
                "(token_hash, created_at, expires_at) VALUES (?, ?, ?)",
                (str(token_hash), now, expiry),
            )
            c.execute(
                "DELETE FROM web_sessions WHERE expires_at <= ?", (now,),
            )

    def web_session_valid(
        self, token_hash: str, *, now: datetime | None = None,
    ) -> bool:
        """Whether a remembered token digest exists and has not expired."""
        now_iso = _normalize_iso(now)
        with self._conn() as c:
            c.execute(
                "DELETE FROM web_sessions WHERE expires_at <= ?", (now_iso,),
            )
            row = c.execute(
                "SELECT 1 FROM web_sessions "
                "WHERE token_hash = ? AND expires_at > ?",
                (str(token_hash), now_iso),
            ).fetchone()
            return row is not None

    def web_session_remove(self, token_hash: str) -> bool:
        """Revoke one remembered token digest."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM web_sessions WHERE token_hash = ?",
                (str(token_hash),),
            )
            return bool(cur.rowcount)

    def web_sessions_clear(self) -> int:
        """Revoke every remembered login, used during password rotation."""
        with self._conn() as c:
            cur = c.execute("DELETE FROM web_sessions")
            return int(cur.rowcount or 0)

    def web_sessions_prune(self, *, now: datetime | None = None) -> int:
        """Remove expired remembered logins and return the number deleted."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM web_sessions WHERE expires_at <= ?",
                (_normalize_iso(now),),
            )
            return int(cur.rowcount or 0)

    # ---- operator settings ----------------------------------------------
    #
    # Backing store for app/config.py. Everything here deals in raw strings;
    # coercion, precedence and validation all live in the config module, which
    # has no database dependency so it can be tested on its own.

    #: How many history rows to retain. A convenience log, not an audit record
    #: -- audit_log remains the durable trail for data mutations.
    SETTINGS_HISTORY_KEEP = 2000

    def settings_all(
        self, *, decrypt: "Callable[[str], str] | None" = None,
    ) -> dict[str, str | None]:
        """Every stored setting as ``{key: value}``.

        ``decrypt`` is applied to rows flagged encrypted. Pass None to get the
        ciphertext -- which an export must never do, and which is why the
        dashboard read path goes through :meth:`settings_rows` and redacts.
        """
        out: dict[str, str | None] = {}
        with self._conn() as c:
            for row in c.execute(
                "SELECT key, value, encrypted FROM app_settings"
            ):
                value = row["value"]
                if value is not None and row["encrypted"] and decrypt is not None:
                    value = decrypt(value)
                out[row["key"]] = value
        return out

    def settings_rows(self) -> list[sqlite3.Row]:
        """Raw rows including metadata, for the Settings tab. Never decrypts."""
        with self._conn() as c:
            return list(c.execute(
                "SELECT key, value, encrypted, updated_at, updated_by "
                "FROM app_settings ORDER BY key"
            ))

    def settings_rev(self) -> int:
        """The current configuration revision, incremented on every write."""
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM app_meta WHERE key = 'config_rev'"
            ).fetchone()
        try:
            return int(row["value"]) if row and row["value"] else 0
        except (TypeError, ValueError):
            return 0

    def settings_set(
        self, key: str, value: str | None, *, encrypted: bool = False,
        actor: str | None = None,
    ) -> int:
        """Upsert one setting and append its history row atomically.

        ``value=None`` means "explicitly cleared" -- the row stays so the
        environment seed's INSERT OR IGNORE can never resurrect it, but
        config.load() falls through to the code default.

        Deliberately does NOT call ``add_audit``: that opens its own
        transaction and ``_conn`` is not reentrant, so nesting them raises
        "cannot start a transaction within a transaction".

        Returns the new revision number.
        """
        now = _normalize_iso(None)
        with self._conn() as c:
            prev = c.execute(
                "SELECT value, encrypted FROM app_settings WHERE key = ?",
                (key,),
            ).fetchone()
            rev_row = c.execute(
                "SELECT value FROM app_meta WHERE key = 'config_rev'"
            ).fetchone()
            try:
                rev = int(rev_row["value"]) + 1 if rev_row and rev_row["value"] else 1
            except (TypeError, ValueError):
                rev = 1

            c.execute(
                "INSERT INTO app_settings "
                "(key, value, encrypted, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, encrypted = excluded.encrypted, "
                "updated_at = excluded.updated_at, "
                "updated_by = excluded.updated_by",
                (key, value, int(encrypted), now, actor),
            )

            # Redaction follows the key's DECLARED sensitivity, not whether
            # encryption happened to succeed.
            #
            # Deriving it from the ``encrypted`` flag alone was a real leak: if
            # the SecretBox is unavailable -- no cryptography package, or a
            # zero-byte / unreadable /data/.secret_key -- SettingsService.set
            # falls back to storing the value in the clear with encrypted=0.
            # The first write of DISCORD_TOKEN would then land with redacted=0,
            # putting the raw bot token into app_settings_history.new_value and
            # rendering it in the dashboard's "Recent changes" table, on the
            # same page that masks the field itself.
            #
            # The check lives here rather than in the caller because
            # settings_set is also reached from revert_to_last_good and
            # bootstrap_fernet, and the invariant belongs with the write.
            spec = _config.SPEC.get(key)
            redacted = int(
                bool(encrypted)
                or bool(prev and prev["encrypted"])
                or bool(spec is not None and spec.secret)
            )
            c.execute(
                "INSERT INTO app_settings_history "
                "(rev, key, old_value, new_value, redacted, at, actor) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    rev, key,
                    None if redacted else (prev["value"] if prev else None),
                    None if redacted else value,
                    redacted, now, actor,
                ),
            )
            c.execute(
                "INSERT INTO app_meta (key, value) VALUES ('config_rev', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(rev),),
            )
            c.execute(
                "DELETE FROM app_settings_history WHERE id <= "
                "(SELECT MAX(id) - ? FROM app_settings_history)",
                (self.SETTINGS_HISTORY_KEEP,),
            )
        return rev

    def settings_seed(
        self, pairs: "Iterable[tuple[str, str | None, bool]]", *,
        actor: str = "env-seed",
    ) -> int:
        """INSERT OR IGNORE a batch of ``(key, value, encrypted)`` triples.

        Never overwrites an existing row, so a re-run after a partial failure
        is safe and an operator's dashboard edit can never be clobbered by a
        later seed. Returns how many rows were actually inserted.
        """
        now = _normalize_iso(None)
        inserted = 0
        with self._conn() as c:
            for key, value, encrypted in pairs:
                cur = c.execute(
                    "INSERT OR IGNORE INTO app_settings "
                    "(key, value, encrypted, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (key, value, int(encrypted), now, actor),
                )
                inserted += cur.rowcount or 0
        return inserted

    def settings_history(self, limit: int = 200) -> list[sqlite3.Row]:
        """Most recent settings changes, newest first."""
        with self._conn() as c:
            return list(c.execute(
                "SELECT id, rev, key, old_value, new_value, redacted, at, actor "
                "FROM app_settings_history ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            ))

    def settings_since(self, rev: int) -> list[sqlite3.Row]:
        """History rows written after ``rev``, oldest first.

        Used by the revert button to replay a bad batch back to the last
        revision that produced a bot which actually started.
        """
        with self._conn() as c:
            return list(c.execute(
                "SELECT id, rev, key, old_value, new_value, redacted, at, actor "
                "FROM app_settings_history WHERE rev > ? ORDER BY id ASC",
                (int(rev),),
            ))

    # ---- role / member mirror -------------------------------------------

    def set_guild_meta(
        self, guild_id: int, name: str, member_count: int = 0,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO guild_meta (guild_id, name, member_count, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    name = excluded.name,
                    member_count = excluded.member_count,
                    updated_at = excluded.updated_at
                """,
                (guild_id, name, int(member_count), _normalize_iso(None)),
            )

    def list_guilds(self) -> list[sqlite3.Row]:
        """Guild picker data: every known guild_id with a name when we have one.

        Left-joins the metadata table over the union of guilds that appear in
        any tracked table, so a guild shows up even before its first sync.
        """
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT g.guild_id,
                       COALESCE(gm.name, '') AS name,
                       COALESCE(gm.member_count, 0) AS member_count
                FROM (
                    SELECT guild_id FROM members
                    UNION SELECT guild_id FROM lifts
                    UNION SELECT guild_id FROM guild_meta
                ) g
                LEFT JOIN guild_meta gm ON gm.guild_id = g.guild_id
                ORDER BY name COLLATE NOCASE, g.guild_id
                """
            ))

    def sync_guild_roles(
        self, guild_id: int, roles: list[dict],
    ) -> None:
        """Replace the stored role list for a guild with ``roles`` (each a dict
        with id/name/color/position/managed). Idempotent full refresh used on
        startup; individual gateway events use ``upsert_role``/``delete_role``."""
        ts = _normalize_iso(None)
        with self._conn() as c:
            c.execute("DELETE FROM guild_roles WHERE guild_id = ?", (guild_id,))
            for r in roles:
                c.execute(
                    """
                    INSERT INTO guild_roles
                        (guild_id, role_id, name, color, position, managed,
                         updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        guild_id, int(r["id"]), str(r["name"]),
                        int(r.get("color", 0)), int(r.get("position", 0)),
                        1 if r.get("managed") else 0, ts,
                    ),
                )

    def upsert_role(
        self, guild_id: int, role_id: int, name: str,
        color: int = 0, position: int = 0, managed: bool = False,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO guild_roles
                    (guild_id, role_id, name, color, position, managed,
                     updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, role_id) DO UPDATE SET
                    name = excluded.name,
                    color = excluded.color,
                    position = excluded.position,
                    managed = excluded.managed,
                    updated_at = excluded.updated_at
                """,
                (
                    guild_id, role_id, name, int(color), int(position),
                    1 if managed else 0, _normalize_iso(None),
                ),
            )

    def delete_role(self, guild_id: int, role_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "DELETE FROM guild_roles WHERE guild_id = ? AND role_id = ?",
                (guild_id, role_id),
            )
            c.execute(
                "DELETE FROM member_roles WHERE guild_id = ? AND role_id = ?",
                (guild_id, role_id),
            )

    def list_guild_roles(self, guild_id: int) -> list[sqlite3.Row]:
        """Roles in a guild with a live member count, ordered like Discord
        (highest position first)."""
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT g.role_id, g.name, g.color, g.position, g.managed,
                       COUNT(mr.user_id) AS members
                FROM guild_roles g
                LEFT JOIN member_roles mr
                    ON mr.guild_id = g.guild_id AND mr.role_id = g.role_id
                WHERE g.guild_id = ?
                GROUP BY g.role_id
                ORDER BY g.position DESC, g.name COLLATE NOCASE
                """,
                (guild_id,),
            ))

    def upsert_member(
        self, guild_id: int, user_id: int, username: str,
        display_name: str, is_bot: bool = False, present: bool = True,
        joined_at: str | None = None, avatar: str | None = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO members
                    (guild_id, user_id, username, display_name, avatar, is_bot,
                     present, joined_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    username = excluded.username,
                    display_name = excluded.display_name,
                    avatar = COALESCE(excluded.avatar, members.avatar),
                    is_bot = excluded.is_bot,
                    present = excluded.present,
                    joined_at = COALESCE(excluded.joined_at, members.joined_at),
                    updated_at = excluded.updated_at
                """,
                (
                    guild_id, user_id, username, display_name, avatar,
                    1 if is_bot else 0, 1 if present else 0, joined_at,
                    _normalize_iso(None),
                ),
            )

    def set_member_present(self, guild_id: int, user_id: int, present: bool) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE members SET present = ?, updated_at = ? "
                "WHERE guild_id = ? AND user_id = ?",
                (1 if present else 0, _normalize_iso(None), guild_id, user_id),
            )

    def set_member_roles(
        self, guild_id: int, user_id: int, role_ids: list[int],
    ) -> None:
        """Replace a member's role edges wholesale with ``role_ids``."""
        with self._conn() as c:
            c.execute(
                "DELETE FROM member_roles WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            for rid in role_ids:
                c.execute(
                    "INSERT OR IGNORE INTO member_roles "
                    "(guild_id, user_id, role_id) VALUES (?, ?, ?)",
                    (guild_id, user_id, int(rid)),
                )

    def get_member(self, guild_id: int, user_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM members WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()

    def member_present(self, guild_id: int, user_id: int) -> bool:
        """True if the user is a current (present=1) member of the guild.

        Backs the cross-server privacy guard: you may only look up another
        user's info when they share the (effective) guild with you.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT present FROM members "
                "WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
        return bool(row) and bool(row["present"])

    def member_guild_ids(self, user_id: int) -> list[int]:
        """Guild IDs where this user is mirrored as a present member.

        Fallback for DM guild-resolution when the live member cache is sparse
        (members intent off). Ordered for stable single-match behaviour.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT guild_id FROM members "
                "WHERE user_id = ? AND present = 1 ORDER BY guild_id",
                (user_id,),
            ).fetchall()
        return [int(r["guild_id"]) for r in rows]

    def nutrition_home_guild(self, user_id: int) -> int | None:
        """The guild a user's (global) nutrition targets are filed under.

        Targets are global, but each rule still carries the guild it was last
        set in. DM logging uses this to attribute a global entry to the same
        server as the user's other data when they haven't pinned one with
        ``/server``. Returns None if they aren't tracking anything."""
        with self._conn() as c:
            row = c.execute(
                "SELECT guild_id FROM nutrition_targets WHERE user_id = ? "
                "ORDER BY set_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        return None if row is None else int(row["guild_id"])

    def dm_guild_get(self, user_id: int) -> int | None:
        """The user's stored default guild for DM commands, if any."""
        with self._conn() as c:
            row = c.execute(
                "SELECT default_guild_id FROM user_dm_prefs WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None or row["default_guild_id"] is None:
            return None
        return int(row["default_guild_id"])

    def dm_guild_set(self, user_id: int, guild_id: int | None) -> None:
        """Set (or clear, with ``None``) the user's default DM guild."""
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO user_dm_prefs (user_id, default_guild_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    default_guild_id = excluded.default_guild_id,
                    updated_at = excluded.updated_at
                """,
                (user_id, guild_id, _normalize_iso(None)),
            )

    def list_members(
        self, guild_id: int, include_absent: bool = True,
    ) -> list[sqlite3.Row]:
        """All known members with their role count, ordered by display name."""
        sql = """
            SELECT m.user_id, m.username, m.display_name, m.avatar, m.is_bot,
                   m.present, m.joined_at,
                   COUNT(mr.role_id) AS role_count
            FROM members m
            LEFT JOIN member_roles mr
                ON mr.guild_id = m.guild_id AND mr.user_id = m.user_id
            WHERE m.guild_id = ?
        """
        if not include_absent:
            sql += " AND m.present = 1"
        sql += " GROUP BY m.user_id ORDER BY m.display_name COLLATE NOCASE"
        with self._conn() as c:
            return list(c.execute(sql, (guild_id,)))

    def member_role_names(
        self, guild_id: int, user_id: int,
    ) -> list[sqlite3.Row]:
        """A member's roles (id/name/color), highest position first."""
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT g.role_id, g.name, g.color, g.position
                FROM member_roles mr
                JOIN guild_roles g
                    ON g.guild_id = mr.guild_id AND g.role_id = mr.role_id
                WHERE mr.guild_id = ? AND mr.user_id = ?
                ORDER BY g.position DESC, g.name COLLATE NOCASE
                """,
                (guild_id, user_id),
            ))

    def members_with_role(
        self, guild_id: int, role_id: int,
    ) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT m.user_id, m.username, m.display_name, m.avatar, m.present
                FROM member_roles mr
                JOIN members m
                    ON m.guild_id = mr.guild_id AND m.user_id = mr.user_id
                WHERE mr.guild_id = ? AND mr.role_id = ?
                ORDER BY m.display_name COLLATE NOCASE
                """,
                (guild_id, role_id),
            ))

    def known_guild_ids(self) -> list[int]:
        """Every guild_id that appears across the dashboard-relevant tables.

        Lets the web UI offer a guild picker without the bot having to inject
        its live guild list. Unions the member mirror with the lifts table so a
        guild shows up even before the member sync has run.
        """
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT guild_id FROM members
                UNION
                SELECT guild_id FROM lifts
                """
            )
            return [int(r[0]) for r in rows]

    # ---- dashboard data browsing + editing ------------------------------

    def web_list_lifts(
        self, guild_id: int, user_id: int | None = None,
        limit: int = 100, offset: int = 0,
    ) -> list[sqlite3.Row]:
        """Lifts for the dashboard. Lifts are global per-user, so a specific
        user's list spans **every** server; the guild-wide list (no user) shows
        lifts from that guild's current members."""
        if user_id is not None:
            sql = (
                "SELECT id, user_id, username, equipment, weight_kg, "
                "bodyweight_add AS bw, reps, logged_at FROM lifts "
                "WHERE user_id = ? "
                "ORDER BY logged_at DESC, id DESC LIMIT ? OFFSET ?"
            )
            params: list[object] = [user_id, limit, offset]
        else:
            sql = (
                "SELECT l.id, l.user_id, l.username, l.equipment, l.weight_kg, "
                "l.bodyweight_add AS bw, l.reps, l.logged_at FROM lifts l "
                "JOIN members m ON m.user_id = l.user_id AND m.guild_id = ? "
                "WHERE m.present = 1 "
                "ORDER BY l.logged_at DESC, l.id DESC LIMIT ? OFFSET ?"
            )
            params = [guild_id, limit, offset]
        with self._conn() as c:
            return list(c.execute(sql, params))

    def web_delete_lift(
        self, guild_id: int, lift_id: int, actor_name: str,
    ) -> bool:
        """Delete one lift by id and audit it. Returns True if a row was removed.

        Lifts are global, so a row shown in any guild's dashboard is deletable by
        its id; ``guild_id`` only labels the audit entry."""
        with self._conn() as c:
            row = c.execute(
                "SELECT user_id, username, equipment, weight_kg "
                "FROM lifts WHERE id = ?",
                (lift_id,),
            ).fetchone()
            if row is None:
                return False
            c.execute("DELETE FROM lifts WHERE id = ?", (lift_id,))
            self._audit(
                c, guild_id, "data", "lift_delete",
                actor_name=actor_name,
                subject_id=row["user_id"], subject_name=row["username"],
                detail=f"{row['equipment']} {row['weight_kg']:g}kg (web)",
            )
            return True

    def web_update_lift(
        self, guild_id: int, lift_id: int, *,
        weight_kg: float, reps: int | None, equipment: str,
        actor_name: str,
    ) -> bool:
        """Edit a lift's weight/reps/equipment from the dashboard and audit it.

        By id (lifts are global) — ``guild_id`` only labels the audit entry."""
        with self._conn() as c:
            row = c.execute(
                "SELECT user_id, username, equipment, weight_kg, reps "
                "FROM lifts WHERE id = ?",
                (lift_id,),
            ).fetchone()
            if row is None:
                return False
            c.execute(
                "UPDATE lifts SET weight_kg = ?, reps = ?, equipment = ? "
                "WHERE id = ?",
                (float(weight_kg), reps, equipment, lift_id),
            )
            self._audit(
                c, guild_id, "data", "lift_edit",
                actor_name=actor_name,
                subject_id=row["user_id"], subject_name=row["username"],
                detail=(
                    f"{row['equipment']} {row['weight_kg']:g}kg → "
                    f"{equipment} {float(weight_kg):g}kg (web)"
                ),
            )
            return True

    def web_list_calories(
        self, guild_id: int, user_id: int | None = None,
        limit: int = 100, offset: int = 0,
    ) -> list[sqlite3.Row]:
        """Calorie entries for the dashboard. Tracking is global: a specific
        user's list spans **every** server, and the guild-wide list (no user)
        shows entries from that guild's current members."""
        if user_id is not None:
            sql = (
                "SELECT id, user_id, username, kcal, note, logged_at "
                "FROM calorie_entries WHERE user_id = ? "
                "ORDER BY logged_at DESC, id DESC LIMIT ? OFFSET ?"
            )
            params: list[object] = [user_id, limit, offset]
        else:
            sql = (
                "SELECT e.id, e.user_id, e.username, e.kcal, e.note, e.logged_at "
                "FROM calorie_entries e "
                "JOIN members m ON m.user_id = e.user_id AND m.guild_id = ? "
                "WHERE m.present = 1 "
                "ORDER BY e.logged_at DESC, e.id DESC LIMIT ? OFFSET ?"
            )
            params = [guild_id, limit, offset]
        with self._conn() as c:
            return list(c.execute(sql, params))

    def web_delete_calorie(
        self, guild_id: int, entry_id: int, actor_name: str,
    ) -> bool:
        # Entries are global, so a row shown in any guild's dashboard is deletable
        # by its id; ``guild_id`` only labels the audit entry.
        with self._conn() as c:
            row = c.execute(
                "SELECT user_id, username, kcal FROM calorie_entries "
                "WHERE id = ?",
                (entry_id,),
            ).fetchone()
            if row is None:
                return False
            c.execute("DELETE FROM calorie_entries WHERE id = ?", (entry_id,))
            self._audit(
                c, guild_id, "data", "calorie_delete",
                actor_name=actor_name,
                subject_id=row["user_id"], subject_name=row["username"],
                detail=f"{row['kcal']:.0f} kcal (web)",
            )
            return True

    def web_list_protein(
        self, guild_id: int, user_id: int | None = None,
        limit: int = 100, offset: int = 0,
    ) -> list[sqlite3.Row]:
        """Protein entries for the dashboard. Global, mirroring
        :meth:`web_list_calories`: per-user spans every server; the guild-wide
        list shows that guild's current members' entries."""
        if user_id is not None:
            sql = (
                "SELECT id, user_id, username, grams, note, logged_at "
                "FROM protein_entries WHERE user_id = ? "
                "ORDER BY logged_at DESC, id DESC LIMIT ? OFFSET ?"
            )
            params: list[object] = [user_id, limit, offset]
        else:
            sql = (
                "SELECT e.id, e.user_id, e.username, e.grams, e.note, e.logged_at "
                "FROM protein_entries e "
                "JOIN members m ON m.user_id = e.user_id AND m.guild_id = ? "
                "WHERE m.present = 1 "
                "ORDER BY e.logged_at DESC, e.id DESC LIMIT ? OFFSET ?"
            )
            params = [guild_id, limit, offset]
        with self._conn() as c:
            return list(c.execute(sql, params))

    def web_delete_protein(
        self, guild_id: int, entry_id: int, actor_name: str,
    ) -> bool:
        # Entries are global — deletable by id from any guild's dashboard;
        # ``guild_id`` only labels the audit entry.
        with self._conn() as c:
            row = c.execute(
                "SELECT user_id, username, grams FROM protein_entries "
                "WHERE id = ?",
                (entry_id,),
            ).fetchone()
            if row is None:
                return False
            c.execute("DELETE FROM protein_entries WHERE id = ?", (entry_id,))
            self._audit(
                c, guild_id, "data", "protein_delete",
                actor_name=actor_name,
                subject_id=row["user_id"], subject_name=row["username"],
                detail=f"{row['grams']:.0f} g (web)",
            )
            return True

    def web_member_overview(
        self, guild_id: int, user_id: int,
    ) -> dict:
        """Compact per-member snapshot for the dashboard member page: lift
        counters, latest bodyweight, and total nutrition counts.

        Lifts, nutrition and bodyweight are all global (they span every
        server), so these counters/latest are summed by user across all
        guilds."""
        with self._conn() as c:
            lifts = c.execute(
                """
                SELECT COUNT(*) AS n, COUNT(DISTINCT equipment) AS equip,
                       MAX(logged_at) AS last_at
                FROM lifts WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            bw = c.execute(
                "SELECT weight_kg, recorded_at FROM bodyweights "
                "WHERE user_id = ? "
                "ORDER BY recorded_at DESC, id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            cal = c.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(kcal),0) AS total "
                "FROM calorie_entries WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            pro = c.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(grams),0) AS total "
                "FROM protein_entries WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return {
                "lifts": dict(lifts) if lifts else {},
                "bodyweight": dict(bw) if bw else None,
                "calories": dict(cal) if cal else {},
                "protein": dict(pro) if pro else {},
            }

    def web_food_set(
        self, guild_id: int, user_id: int, username: str, *,
        name: str, display: str, kcal: float,
        protein_g: float | None, actor_name: str,
    ) -> None:
        """Create/update a saved food from the dashboard, and audit it.
        ``name`` must already be normalized."""
        self.calorie_food_set(guild_id, user_id, name, display, kcal, protein_g)
        detail = f"{display} = {kcal:.0f} kcal"
        if protein_g is not None:
            detail += f", {protein_g:.0f}g protein"
        self.add_audit(
            guild_id, "data", "food_set",
            actor_name=actor_name, subject_id=user_id, subject_name=username,
            detail=detail + " (web)",
        )

    def web_nutrition_targets_set(
        self, guild_id: int, user_id: int, username: str, *,
        kcal: float | None, weekend_kcal: float | None,
        protein_g: float | None, weekend_protein_g: float | None,
        actor_name: str,
    ) -> None:
        """Save all four target fields from the dashboard, and audit it.

        Unlike the Discord commands, the dashboard form shows every field at
        once, so every field is explicit: a blank weekday value means "stop
        tracking this macro", and a blank weekend value means "no separate
        weekend target" — not "leave whatever was there". That's why this takes
        plain values rather than :data:`KEEP`.
        """
        with self._conn() as c:
            # The dashboard form is an explicit, full declaration of the protein
            # target (there is no "bodyweight" option here), so any save breaks a
            # bodyweight link — exactly like Discord's `/protein setup <n>` and
            # `/protein stop` do. Without this, the link survives a web edit and
            # the next weigh-in silently re-arms or overwrites what the user just
            # set here. Done directly on the open connection because _conn isn't
            # reentrant (its BEGIN IMMEDIATE would clash with a nested call).
            c.execute(
                "DELETE FROM protein_bw_links WHERE user_id = ?", (user_id,),
            )
            pairs = (
                (targets.MACRO_KCAL, kcal, weekend_kcal),
                (targets.MACRO_PROTEIN, protein_g, weekend_protein_g),
            )
            for macro, base, weekend in pairs:
                if base is None:
                    self._nutrition_tracking_off(
                        c, user_id, username, guild_id, macro,
                    )
                    continue
                # Only tombstone the weekend rule when one is actually live —
                # otherwise a plain weekday-only save litters the table with
                # NULL rows that mean nothing. Read before writing the default.
                live = targets.resolve(
                    self._target_rows(c, user_id), targets.local_today(),
                )
                effective_from = self._target_effective_from(c, user_id, macro)
                self._nutrition_target_write(
                    c, guild_id, user_id, username,
                    macro, targets.SCOPE_DEFAULT, base, effective_from,
                )
                if weekend is not None or live.macro(macro).split:
                    self._nutrition_target_write(
                        c, guild_id, user_id, username,
                        macro, targets.SCOPE_WEEKEND, weekend, effective_from,
                    )

        def _fmt(value: float | None, unit: str) -> str:
            return "off" if value is None else f"{value:.0f}{unit}"

        self.add_audit(
            guild_id, "data", "nutrition_targets_set",
            actor_name=actor_name, subject_id=user_id, subject_name=username,
            detail=(
                f"calories {_fmt(kcal, ' kcal')} / "
                f"{_fmt(weekend_kcal, ' kcal')} weekend, protein "
                f"{_fmt(protein_g, 'g')} / "
                f"{_fmt(weekend_protein_g, 'g')} weekend (web)"
            ),
        )

    def web_food_delete(
        self, guild_id: int, user_id: int, username: str,
        name: str, actor_name: str,
    ) -> bool:
        ok = self.calorie_food_remove(guild_id, user_id, name)
        if ok:
            self.add_audit(
                guild_id, "data", "food_delete",
                actor_name=actor_name, subject_id=user_id,
                subject_name=username, detail=f"{name} (web)",
            )
        return ok
