# SQLite storage for lift entries.

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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

-- Activity tracking. ``activity`` is the game/app name, or NULL when the
-- user stops playing. We de-dupe consecutive identical values. ``image_url``
-- is the Discord rich-presence large image for that activity when one is
-- available (many plain "playing X" presences have none) — used by the web
-- dashboard to show real game art.
CREATE TABLE IF NOT EXISTS activity_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    activity  TEXT,
    image_url TEXT,
    at        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_activity_events_user
    ON activity_events (guild_id, user_id, at);

-- Voice-channel tracking. Append-only log of voice transitions: ``event`` is
-- one of 'join', 'leave', 'move'. ``channel_id`` / ``channel_name`` is the
-- channel involved (the destination for join/move, the channel left for leave),
-- snapshotted at write time so the dashboard needn't resolve channels.
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
    at           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_message_log_user
    ON message_log (guild_id, user_id, at);

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

-- Calorie tracking. ``calorie_goals`` holds each user's daily intake target
-- (stored in kcal — the bot converts kJ on the way in). Having a row here is
-- what marks a user as "doing" calorie tracking for the weekly AI summary.
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

CREATE UNIQUE INDEX IF NOT EXISTS idx_protein_entries_dedupe
    ON protein_entries (message_id) WHERE message_id IS NOT NULL;

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
    linked_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS hevy_imported (
    user_id     INTEGER NOT NULL,
    workout_id  TEXT    NOT NULL,
    imported_at TEXT    NOT NULL,
    PRIMARY KEY (user_id, workout_id)
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
"""


def _normalize_iso(dt: datetime | None) -> str:
    """Always store timestamps as UTC ISO-8601, regardless of caller tz."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


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

    def __init__(self, path: str | Path) -> None:
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
        self._connection.executescript(SCHEMA)
        self._migrate()

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
            self._consolidate_global_goals()
            self._recanonicalize_equipment()

    def _consolidate_global_goals(self) -> None:
        """Collapse calorie/protein goals to one row per user.

        Tracking is per-user/global, but databases written before that change
        could hold a separate goal row per server. Logged *entries* are already
        read globally (by user_id), so no entry migration is needed — only the
        goal rows (the opt-in marker + daily target) need de-duplicating. We
        keep each user's most-recently-set row and drop the rest. Idempotent:
        a DB that already has one row per user is left untouched.
        """
        for table in ("calorie_goals", "protein_goals"):
            self._connection.execute(
                # The inner GROUP BY with a single MAX() picks, per user, the
                # rowid belonging to the latest set_at (a documented SQLite
                # bare-column guarantee); everything else for that user is
                # deleted.
                f"DELETE FROM {table} WHERE rowid NOT IN ("
                f"  SELECT rowid FROM ("
                f"    SELECT rowid, MAX(set_at) FROM {table} GROUP BY user_id"
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
    ) -> int:
        """Insert lifts. Returns the number of rows actually inserted
        (duplicates from the same message are ignored).

        The whole batch runs inside a single transaction (see ``_conn``),
        so a mid-batch failure won't leave half the lifts persisted.
        """
        if not lifts:
            return 0
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
                            actor_id=user_id, actor_name=username,
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
    ) -> list[int]:
        """Same as ``add_lifts`` but returns the row ids that were inserted.

        Used by ``/log``-style flows where there's no source message_id, so
        the reaction-undo path can target the exact rows we just created.
        """
        if not lifts:
            return []
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
                                actor_id=user_id, actor_name=username,
                                subject_id=user_id, subject_name=username,
                                detail=f"{lift.equipment} {lift.weight_kg:g}kg",
                            )
                except sqlite3.IntegrityError:
                    continue
        return ids

    def personal_bests(self, guild_id: int, user_id: int) -> list[sqlite3.Row]:
        # For each equipment, pick the row with the highest weight_kg, and
        # return the date that PR was set on (earliest date at that weight).
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
                    WHERE guild_id = ? AND user_id = ?
                    GROUP BY equipment
                ) m ON m.equipment = l.equipment AND m.mx = l.weight_kg
                WHERE l.guild_id = ? AND l.user_id = ?
                GROUP BY l.equipment
                ORDER BY l.equipment
                """,
                (guild_id, user_id, guild_id, user_id),
            ))

    def leaderboard(self, guild_id: int, equipment: str) -> list[sqlite3.Row]:
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
                    SELECT user_id, MAX(weight_kg) AS mx
                    FROM lifts
                    WHERE guild_id = ? AND equipment = ?
                    GROUP BY user_id
                ) m ON m.user_id = l.user_id AND m.mx = l.weight_kg
                WHERE l.guild_id = ? AND l.equipment = ?
                GROUP BY l.user_id
                ORDER BY best DESC
                LIMIT 25
                """,
                (guild_id, equipment, guild_id, equipment),
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
                    WHERE guild_id = ? AND user_id = ? AND equipment = ?
                )
                SELECT month,
                       weight_kg      AS best,
                       bodyweight_add AS bw,
                       logged_at      AS first_seen
                FROM ranked
                WHERE rn = 1
                ORDER BY month
                """,
                (guild_id, user_id, equipment),
            ))

    def known_equipment(self, guild_id: int) -> list[str]:
        with self._conn() as c:
            return [r[0] for r in c.execute(
                "SELECT DISTINCT equipment FROM lifts WHERE guild_id = ? ORDER BY equipment",
                (guild_id,),
            )]

    def delete_equipment(self, guild_id: int, equipment: str) -> int:
        """Delete every row for a given equipment name in a guild. Returns
        the number of rows removed."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM lifts WHERE guild_id = ? AND equipment = ?",
                (guild_id, equipment),
            )
            return cur.rowcount or 0

    def rename_equipment(
        self, guild_id: int, src: str, dst: str,
        user_id: int | None = None,
    ) -> int:
        """Re-label every row from equipment=src to equipment=dst. Returns
        the number of rows affected. The unique (message_id, equipment) index
        is respected: if the destination already exists for a given message,
        the duplicate source row is dropped instead of renamed.

        If ``user_id`` is provided, the rename is scoped to only that user's
        rows — useful when one lifter mislabels their entry without affecting
        anyone else's history.

        When the rename is guild-wide (no user filter), any custom_aliases
        whose canonical pointed at ``src`` are repointed at ``dst`` so the
        alias table doesn't go stale. Per-user renames don't touch aliases
        because aliases are guild-scoped, not user-scoped.
        """
        user_clause = " AND user_id = ?" if user_id is not None else ""
        user_params: tuple[object, ...] = (
            (user_id,) if user_id is not None else ()
        )
        with self._conn() as c:
            # Remove rows that would collide with the dedupe index after rename.
            # When scoped to a single user, the collision check is also scoped
            # so we don't drop someone else's row.
            c.execute(
                f"""
                DELETE FROM lifts
                WHERE guild_id = ? AND equipment = ?{user_clause}
                  AND message_id IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM lifts b
                      WHERE b.guild_id = lifts.guild_id
                        AND b.message_id = lifts.message_id
                        AND b.equipment = ?
                        AND b.user_id = lifts.user_id
                  )
                """,
                (guild_id, src, *user_params, dst),
            )
            cur = c.execute(
                "UPDATE lifts SET equipment = ? "
                f"WHERE guild_id = ? AND equipment = ?{user_clause}",
                (dst, guild_id, src, *user_params),
            )
            if user_id is None:
                # Repoint guild aliases so future parses land on the new
                # canonical instead of the old one.
                c.execute(
                    "UPDATE custom_aliases SET canonical = ? "
                    "WHERE guild_id = ? AND canonical = ?",
                    (dst, guild_id, src),
                )
            goal_sql = (
                "SELECT user_id, equipment, target_kg, bodyweight_add, set_at "
                "FROM goals WHERE guild_id = ? AND equipment IN (?, ?)"
            )
            goal_params: list[object] = [guild_id, src, dst]
            if user_id is not None:
                goal_sql += " AND user_id = ?"
                goal_params.append(user_id)
            goals_by_user: dict[int, dict[str, sqlite3.Row]] = {}
            for row in c.execute(goal_sql, goal_params):
                goals_by_user.setdefault(row["user_id"], {})[row["equipment"]] = row
            for goal_user_id, goals in goals_by_user.items():
                src_goal = goals.get(src)
                if src_goal is None:
                    continue
                dst_goal = goals.get(dst)
                if dst_goal is None:
                    c.execute(
                        "UPDATE goals SET equipment = ? "
                        "WHERE guild_id = ? AND user_id = ? AND equipment = ?",
                        (dst, guild_id, goal_user_id, src),
                    )
                    continue
                if src_goal["target_kg"] > dst_goal["target_kg"]:
                    c.execute(
                        """
                        UPDATE goals
                        SET target_kg = ?, bodyweight_add = ?, set_at = ?
                        WHERE guild_id = ? AND user_id = ? AND equipment = ?
                        """,
                        (
                            src_goal["target_kg"],
                            src_goal["bodyweight_add"],
                            src_goal["set_at"],
                            guild_id,
                            goal_user_id,
                            dst,
                        ),
                    )
                c.execute(
                    "DELETE FROM goals "
                    "WHERE guild_id = ? AND user_id = ? AND equipment = ?",
                    (guild_id, goal_user_id, src),
                )
            return cur.rowcount or 0

    def count_equipment_rows(
        self, guild_id: int, equipment: str,
        user_id: int | None = None,
    ) -> int:
        """How many rows match equipment (optionally for one user). Used for
        rename previews / dry-runs."""
        sql = (
            "SELECT COUNT(*) FROM lifts "
            "WHERE guild_id = ? AND equipment = ?"
        )
        params: list[object] = [guild_id, equipment]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        with self._conn() as c:
            row = c.execute(sql, params).fetchone()
            return int(row[0]) if row else 0

    def delete_entry(
        self,
        guild_id: int,
        equipment: str,
        date: str,
        user_id: int | None = None,
    ) -> int:
        """Delete entries matching equipment + YYYY-MM-DD date, optionally
        scoped to a specific user. Returns rows deleted."""
        sql = (
            "DELETE FROM lifts "
            "WHERE guild_id = ? AND equipment = ? "
            "AND substr(logged_at, 1, 10) = ?"
        )
        params: list[object] = [guild_id, equipment, date]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        with self._conn() as c:
            cur = c.execute(sql, params)
            return cur.rowcount or 0

    def delete_entry_between(
        self,
        guild_id: int,
        equipment: str,
        start_iso: str,
        end_iso: str,
        user_id: int | None = None,
    ) -> int:
        """Delete entries matching equipment inside a UTC timestamp range."""
        sql = (
            "DELETE FROM lifts "
            "WHERE guild_id = ? AND equipment = ? "
            "AND logged_at >= ? AND logged_at < ?"
        )
        params: list[object] = [guild_id, equipment, start_iso, end_iso]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        with self._conn() as c:
            cur = c.execute(sql, params)
            return cur.rowcount or 0

    def _latest_lift(
        self,
        c: sqlite3.Connection,
        guild_id: int,
        user_id: int,
        equipment: str,
        start_iso: str | None = None,
        end_iso: str | None = None,
    ) -> sqlite3.Row | None:
        sql = (
            "SELECT id, equipment, weight_kg, bodyweight_add AS bw, reps, logged_at "
            "FROM lifts WHERE guild_id = ? AND user_id = ? AND equipment = ?"
        )
        params: list[object] = [guild_id, user_id, equipment]
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
        """Chronological per-entry history for one user/equipment."""
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT weight_kg, bodyweight_add AS bw, logged_at, reps
                FROM lifts
                WHERE guild_id = ? AND user_id = ? AND equipment = ?
                ORDER BY logged_at
                LIMIT ?
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
        params: list[object] = [guild_id, user_id]
        sql = (
            "SELECT COALESCE(SUM(weight_kg), 0) AS total, COUNT(*) AS n "
            "FROM lifts WHERE guild_id = ? AND user_id = ?"
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
                WHERE guild_id = ? AND user_id = ?
                ORDER BY logged_at DESC, id DESC
                LIMIT 1
                """,
                (guild_id, user_id),
            ).fetchone()
            if not row:
                return None, []
            day = row["d"]
            rows = list(c.execute(
                """
                SELECT equipment, weight_kg, bodyweight_add AS bw,
                       logged_at, reps
                FROM lifts
                WHERE guild_id = ? AND user_id = ?
                  AND substr(logged_at, 1, 10) = ?
                ORDER BY logged_at ASC, id ASC
                """,
                (guild_id, user_id, day),
            ))
            return day, rows

    def machine_history(
        self, guild_id: int, equipment: str, limit: int = 50
    ) -> list[sqlite3.Row]:
        """Chronological timeline of all users' entries on one equipment."""
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT username, weight_kg, bodyweight_add AS bw, logged_at
                FROM lifts
                WHERE guild_id = ? AND equipment = ?
                ORDER BY logged_at
                LIMIT ?
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
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
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
                WHERE guild_id = ? AND user_id = ?
                GROUP BY equipment
                ORDER BY best DESC
                LIMIT ?
                """,
                (guild_id, user_id, limit),
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
                WHERE guild_id = ? AND user_id = ?
                GROUP BY equipment
                ORDER BY n DESC, equipment
                LIMIT ?
                """,
                (guild_id, user_id, limit),
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
                    WHERE guild_id = ? AND user_id = ?
                ),
                lasts AS (
                    SELECT equipment,
                           weight_kg AS last_w,
                           logged_at AS last_at,
                           ROW_NUMBER() OVER (PARTITION BY equipment
                                              ORDER BY logged_at DESC) AS rn
                    FROM lifts
                    WHERE guild_id = ? AND user_id = ?
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
                (guild_id, user_id, guild_id, user_id, limit),
            ))

    def recent_user_equipment(
        self, guild_id: int, user_id: int, limit: int = 25
    ) -> list[str]:
        with self._conn() as c:
            return [r[0] for r in c.execute(
                """
                SELECT equipment FROM lifts
                WHERE guild_id = ? AND user_id = ?
                GROUP BY equipment
                ORDER BY MAX(logged_at) DESC
                LIMIT ?
                """,
                (guild_id, user_id, limit),
            )]

    def previous_best(
        self, guild_id: int, user_id: int, equipment: str,
        before_id: int | None = None,
    ) -> float | None:
        """Highest weight the user had recorded for this equipment, optionally
        strictly before a given row id. Returns None if no prior entry."""
        sql = (
            "SELECT MAX(weight_kg) AS best FROM lifts "
            "WHERE guild_id = ? AND user_id = ? AND equipment = ?"
        )
        params: list[object] = [guild_id, user_id, equipment]
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
                WHERE guild_id = ? AND user_id = ?
                ORDER BY logged_at DESC, id DESC
                LIMIT ?
                """,
                (guild_id, user_id, limit),
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
                WHERE guild_id = ? AND user_id = ?
                ORDER BY logged_at, id
                """,
                (guild_id, user_id),
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
                WHERE guild_id = ? AND user_id = ?
                  AND reps IS NOT NULL AND reps > 0 AND weight_kg > 0
                ORDER BY equipment, logged_at, id
                """,
                (guild_id, user_id),
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
                    WHERE guild_id = ? AND user_id = ?
                )
                SELECT equipment, weight_kg, bw, logged_at, n
                FROM ranked
                WHERE rn = 1
                ORDER BY logged_at ASC, equipment
                """,
                (guild_id, user_id),
            ))

    def pop_last_for_user(
        self, guild_id: int, user_id: int,
        *, actor_id: int | None = None, actor_name: str | None = None,
    ) -> sqlite3.Row | None:
        """Delete the user's most recently logged row and return it. Returns
        None if they have no entries."""
        with self._conn() as c:
            row = c.execute(
                """
                SELECT id, equipment, weight_kg,
                       bodyweight_add AS bw, logged_at
                FROM lifts
                WHERE guild_id = ? AND user_id = ?
                ORDER BY logged_at DESC, id DESC
                LIMIT 1
                """,
                (guild_id, user_id),
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
                WHERE guild_id = ? AND user_id = ?
                ORDER BY logged_at DESC, id DESC
                LIMIT ?
                """,
                (guild_id, user_id, n),
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
                WHERE guild_id = ? AND user_id = ?
                ORDER BY 1
                """,
                (guild_id, user_id),
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
                WHERE guild_id = ? AND user_id = ?
                ORDER BY logged_at
                """,
                (guild_id, user_id),
            )]

    # ---- goals -----------------------------------------------------------

    def goal_set(
        self, guild_id: int, user_id: int, equipment: str,
        target_kg: float, bodyweight_add: bool,
    ) -> None:
        with self._conn() as c:
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
        with self._conn() as c:
            cur = c.execute(
                """
                DELETE FROM goals
                WHERE guild_id = ? AND user_id = ? AND equipment = ?
                """,
                (guild_id, user_id, equipment),
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
        with self._conn() as c:
            return c.execute(
                """
                SELECT equipment, target_kg, bodyweight_add AS bw, set_at
                FROM goals
                WHERE guild_id = ? AND user_id = ? AND equipment = ?
                """,
                (guild_id, user_id, equipment),
            ).fetchone()

    def goal_list(
        self, guild_id: int, user_id: int
    ) -> list[sqlite3.Row]:
        """Each goal joined with the user's current best on that equipment."""
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT g.equipment, g.target_kg,
                       g.bodyweight_add AS bw,
                       g.set_at,
                       COALESCE(
                           (SELECT MAX(weight_kg) FROM lifts l
                            WHERE l.guild_id = g.guild_id
                              AND l.user_id  = g.user_id
                              AND l.equipment = g.equipment),
                           0
                       ) AS current_best
                FROM goals g
                WHERE g.guild_id = ? AND g.user_id = ?
                ORDER BY g.equipment
                """,
                (guild_id, user_id),
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
    ) -> None:
        """Record a new bodyweight measurement for a user.

        We append rather than overwrite so the user can see how their
        bodyweight has trended and so historical lifts can in principle
        be re-rendered against the bodyweight that was current at the time.
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
                detail=f"bodyweight {float(weight_kg):g} kg",
            )

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
        """Return this user's bodyweight measurements oldest-first.

        Used by ``/bodyweight_history`` and ``/bodyweight_graph`` so the
        timeline plots left-to-right without an extra reverse step. Global:
        weigh-ins from every server are one timeline (``guild_id`` unused).
        """
        with self._conn() as c:
            return c.execute(
                "SELECT weight_kg, recorded_at FROM bodyweights "
                "WHERE user_id = ? "
                "ORDER BY recorded_at ASC, id ASC LIMIT ?",
                (user_id, int(limit)),
            ).fetchall()

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

    def activity_log_event(
        self, guild_id: int, user_id: int, activity: str | None,
        at: datetime | None = None, image_url: str | None = None,
    ) -> bool:
        """Append an activity event (game/app name or None = stopped).
        De-duplicates against the most recent stored value (on name only, so a
        late-arriving image for the same game doesn't spam a new row). Returns
        True if a row was inserted."""
        ts = _normalize_iso(at)
        with self._conn() as c:
            last = c.execute(
                "SELECT activity FROM activity_events "
                "WHERE guild_id = ? AND user_id = ? "
                "ORDER BY at DESC, id DESC LIMIT 1",
                (guild_id, user_id),
            ).fetchone()
            if last is None and activity is None:
                return False
            if last is not None and last["activity"] == activity:
                return False
            c.execute(
                "INSERT INTO activity_events "
                "(guild_id, user_id, activity, image_url, at) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild_id, user_id, activity, image_url, ts),
            )
            return True

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
        """Latest activity event for a user (activity may be NULL = stopped)."""
        with self._conn() as c:
            return c.execute(
                "SELECT activity, image_url, at FROM activity_events "
                "WHERE guild_id = ? AND user_id = ? "
                "ORDER BY at DESC, id DESC LIMIT 1",
                (guild_id, user_id),
            ).fetchone()

    def activity_image_map(
        self, guild_id: int, user_id: int,
    ) -> dict[str, str]:
        """Best-known image URL per game name for a user (most recent wins).
        Lets the activity feed show art for games whose current event has none
        but an earlier session captured one."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT activity, image_url FROM activity_events "
                "WHERE guild_id = ? AND user_id = ? "
                "AND activity IS NOT NULL AND image_url IS NOT NULL "
                "ORDER BY at ASC, id ASC",
                (guild_id, user_id),
            )
            return {r["activity"]: r["image_url"] for r in rows}

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
                    "SELECT activity, at FROM activity_events "
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
                f"SELECT activity, at FROM activity_events WHERE {where} "
                "ORDER BY at ASC, id ASC",
                params,
            ).fetchall())
            return rows

    # ------------------------------------------------------------------
    # Voice-channel tracking
    # ------------------------------------------------------------------

    def voice_log_event(
        self, guild_id: int, user_id: int, event: str,
        channel_id: int | None = None, channel_name: str | None = None,
        at: datetime | None = None,
    ) -> None:
        """Append a voice transition ('join' / 'leave' / 'move')."""
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
        carrying the member's mirrored ``display_name`` and ``avatar``."""
        with self._conn() as c:
            params: list = [guild_id]
            where = "ve.guild_id = ?"
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

    # ------------------------------------------------------------------
    # Calorie tracking
    # ------------------------------------------------------------------

    def calorie_goal_set(
        self, guild_id: int, user_id: int, username: str,
        daily_target_kcal: float,
    ) -> None:
        """Create or update a user's daily calorie target (kcal).

        Tracking is **per-user / global**: setting it consolidates to a single
        row (any copy under another server is cleared first), so the goal applies
        in every server and in DMs.
        """
        with self._conn() as c:
            c.execute(
                "DELETE FROM calorie_goals WHERE user_id = ? AND guild_id <> ?",
                (user_id, guild_id),
            )
            c.execute(
                """
                INSERT INTO calorie_goals
                    (guild_id, user_id, username, daily_target_kcal, set_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (guild_id, user_id) DO UPDATE SET
                    username          = excluded.username,
                    daily_target_kcal = excluded.daily_target_kcal,
                    set_at            = excluded.set_at
                """,
                (
                    guild_id, user_id, username,
                    float(daily_target_kcal), _normalize_iso(None),
                ),
            )
            self._audit_data(
                c, guild_id, "calorie_goal_set",
                subject_id=user_id, subject_name=username,
                detail=f"calorie target {float(daily_target_kcal):.0f} kcal/day",
            )

    def calorie_goal_get(
        self, guild_id: int, user_id: int,
    ) -> sqlite3.Row | None:
        """The user's calorie goal, resolved **per-user** so it applies in every
        server + DMs (prefers the current guild's row, else the most recent)."""
        with self._conn() as c:
            return c.execute(
                "SELECT username, daily_target_kcal, set_at "
                "FROM calorie_goals WHERE user_id = ? "
                "ORDER BY (guild_id = ?) DESC, set_at DESC LIMIT 1",
                (user_id, guild_id),
            ).fetchone()

    def calorie_goal_remove(self, guild_id: int, user_id: int) -> bool:
        """Stop tracking everywhere (tracking is global). Entry history is kept
        so re-enabling later still has the back data; only the goal row (the
        opt-in marker) goes."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM calorie_goals WHERE user_id = ?",
                (user_id,),
            )
            ok = (cur.rowcount or 0) > 0
            if ok:
                self._audit_data(
                    c, guild_id, "calorie_goal_remove", subject_id=user_id,
                    detail="stopped calorie tracking",
                )
            return ok

    def calorie_tracked_users(self, guild_id: int) -> list[sqlite3.Row]:
        """Calorie-tracking members of this guild — the weekly AI summary
        iterates this list.

        Tracking is global (one goal row per user, tagged with whichever server
        they last set it in), so membership is matched against the **members
        mirror** for this guild rather than the goal's stored ``guild_id``;
        otherwise someone who set their goal in another server would silently
        drop out of this guild's report."""
        with self._conn() as c:
            return list(c.execute(
                "SELECT g.user_id, g.username, g.daily_target_kcal "
                "FROM calorie_goals g "
                "JOIN members m ON m.user_id = g.user_id AND m.guild_id = ? "
                "WHERE m.present = 1 "
                "ORDER BY g.username",
                (guild_id,),
            ))

    def calorie_add(
        self, guild_id: int, user_id: int, username: str, kcal: float,
        note: str | None = None, raw: str | None = None,
        logged_at: datetime | None = None,
        message_id: int | None = None,
    ) -> int:
        """Insert one intake entry. Returns the new row id, or 0 if a row for
        this ``message_id`` already exists (dedupe, so backfill re-scans are
        safe). ``message_id`` is None for slash-command entries, which never
        dedupe."""
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
                    actor_id=user_id, actor_name=username,
                    subject_id=user_id, subject_name=username,
                    detail=f"+{float(kcal):.0f} kcal"
                    + (f" ({note})" if note else ""),
                )
            return new_id

    def calorie_pop_last(
        self, guild_id: int, user_id: int,
        *, actor_id: int | None = None, actor_name: str | None = None,
    ) -> sqlite3.Row | None:
        """Delete the user's most recent intake entry and return it.

        Global: undoes the latest entry across **every** server (one shared
        diary), so `/calories undo` works regardless of where it was logged.
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

        Returns the *old* row (id, kcal, note) so the caller can show the change,
        or None when there's nothing to edit. A new ``note`` replaces the old;
        ``None`` keeps it."""
        with self._conn() as c:
            row = c.execute(
                "SELECT id, kcal, note FROM calorie_entries WHERE user_id = ? "
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
        message. message_id is unique per entry (partial unique index)."""
        with self._conn() as c:
            return c.execute(
                "SELECT id, user_id, kcal, note FROM calorie_entries "
                "WHERE guild_id = ? AND message_id = ?",
                (guild_id, message_id),
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
        Returns the deleted row (kcal/note) or None if it was already gone."""
        with self._conn() as c:
            row = c.execute(
                "SELECT id, kcal, note FROM calorie_entries "
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

    def protein_goal_set(
        self, guild_id: int, user_id: int, username: str,
        daily_target_g: float,
    ) -> None:
        """Create or update a user's daily protein ceiling (grams).

        Tracking is **per-user / global**: setting it consolidates to a single
        row, so the goal applies in every server and in DMs."""
        with self._conn() as c:
            c.execute(
                "DELETE FROM protein_goals WHERE user_id = ? AND guild_id <> ?",
                (user_id, guild_id),
            )
            c.execute(
                """
                INSERT INTO protein_goals
                    (guild_id, user_id, username, daily_target_g, set_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (guild_id, user_id) DO UPDATE SET
                    username       = excluded.username,
                    daily_target_g = excluded.daily_target_g,
                    set_at         = excluded.set_at
                """,
                (
                    guild_id, user_id, username,
                    float(daily_target_g), _normalize_iso(None),
                ),
            )
            self._audit_data(
                c, guild_id, "protein_goal_set",
                subject_id=user_id, subject_name=username,
                detail=f"protein max {float(daily_target_g):.0f} g/day",
            )

    def protein_goal_get(
        self, guild_id: int, user_id: int,
    ) -> sqlite3.Row | None:
        """The user's protein goal, resolved **per-user** so it applies in every
        server + DMs (prefers the current guild's row, else the most recent)."""
        with self._conn() as c:
            return c.execute(
                "SELECT username, daily_target_g, set_at "
                "FROM protein_goals WHERE user_id = ? "
                "ORDER BY (guild_id = ?) DESC, set_at DESC LIMIT 1",
                (user_id, guild_id),
            ).fetchone()

    def protein_goal_remove(self, guild_id: int, user_id: int) -> bool:
        """Stop protein tracking everywhere (global); logged history is kept."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM protein_goals WHERE user_id = ?",
                (user_id,),
            )
            ok = (cur.rowcount or 0) > 0
            if ok:
                self._audit_data(
                    c, guild_id, "protein_goal_remove", subject_id=user_id,
                    detail="stopped protein tracking",
                )
            return ok

    def protein_tracked_users(self, guild_id: int) -> list[sqlite3.Row]:
        """Protein-tracking members of this guild. Like its calorie twin,
        tracking is global so membership is matched against the members mirror
        for this guild rather than the goal's stored ``guild_id``."""
        with self._conn() as c:
            return list(c.execute(
                "SELECT g.user_id, g.username, g.daily_target_g "
                "FROM protein_goals g "
                "JOIN members m ON m.user_id = g.user_id AND m.guild_id = ? "
                "WHERE m.present = 1 "
                "ORDER BY g.username",
                (guild_id,),
            ))

    def protein_add(
        self, guild_id: int, user_id: int, username: str, grams: float,
        note: str | None = None, raw: str | None = None,
        logged_at: datetime | None = None,
        message_id: int | None = None,
    ) -> int:
        """Insert one protein entry. Returns the new row id, or 0 if a row for
        this ``message_id`` already exists (dedupe)."""
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
                    actor_id=user_id, actor_name=username,
                    subject_id=user_id, subject_name=username,
                    detail=f"+{float(grams):.0f} g"
                    + (f" ({note})" if note else ""),
                )
            return new_id

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
        Returns the *old* row or None when there's nothing to edit."""
        with self._conn() as c:
            row = c.execute(
                "SELECT id, grams, note FROM protein_entries WHERE user_id = ? "
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
        (used by ❌ reaction-undo on protein/combined replies)."""
        with self._conn() as c:
            return c.execute(
                "SELECT id, user_id, grams, note FROM protein_entries "
                "WHERE guild_id = ? AND message_id = ?",
                (guild_id, message_id),
            ).fetchone()

    def delete_protein_entry(
        self, guild_id: int, target_user_id: int, protein_id: int,
        *, actor_id: int | None = None, actor_name: str | None = None,
    ) -> sqlite3.Row | None:
        """Delete one protein entry by id, scoped to (guild, user). Returns the
        deleted row or None if already gone."""
        with self._conn() as c:
            row = c.execute(
                "SELECT id, grams, note FROM protein_entries "
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
        recently saved."""
        with self._conn() as c:
            return c.execute(
                "SELECT name, display, kcal, protein_g FROM calorie_foods "
                "WHERE user_id = ? AND name = ? "
                "ORDER BY (guild_id = ?) DESC, set_at DESC LIMIT 1",
                (user_id, name, guild_id),
            ).fetchone()

    def calorie_food_remove(
        self, guild_id: int, user_id: int, name: str,
    ) -> bool:
        """Remove a saved food for the user **everywhere** (foods are shared
        across servers, so deletion isn't guild-scoped)."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM calorie_foods WHERE user_id = ? AND name = ?",
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
        """The guild a user's (global) calorie/protein goal row is filed under.

        Tracking is global, but the goal row still carries the guild it was last
        set in. DM logging uses this to attribute a global entry to the same
        server as the user's other data when they haven't pinned one with
        ``/server``. Returns None if they aren't tracking anything."""
        with self._conn() as c:
            for table in ("calorie_goals", "protein_goals"):
                row = c.execute(
                    f"SELECT guild_id FROM {table} WHERE user_id = ? "
                    "ORDER BY set_at DESC LIMIT 1",
                    (user_id,),
                ).fetchone()
                if row is not None:
                    return int(row["guild_id"])
        return None

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
        sql = (
            "SELECT id, user_id, username, equipment, weight_kg, "
            "bodyweight_add AS bw, reps, logged_at FROM lifts WHERE guild_id = ?"
        )
        params: list[object] = [guild_id]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY logged_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._conn() as c:
            return list(c.execute(sql, params))

    def web_delete_lift(
        self, guild_id: int, lift_id: int, actor_name: str,
    ) -> bool:
        """Delete one lift by id (scoped to guild) and audit it. Returns True
        if a row was removed."""
        with self._conn() as c:
            row = c.execute(
                "SELECT user_id, username, equipment, weight_kg "
                "FROM lifts WHERE id = ? AND guild_id = ?",
                (lift_id, guild_id),
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
        """Edit a lift's weight/reps/equipment from the dashboard and audit it."""
        with self._conn() as c:
            row = c.execute(
                "SELECT user_id, username, equipment, weight_kg, reps "
                "FROM lifts WHERE id = ? AND guild_id = ?",
                (lift_id, guild_id),
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

        Nutrition and bodyweight are global (they span every server), so those
        totals/latest are by user across all guilds; lifts stay guild-scoped."""
        with self._conn() as c:
            lifts = c.execute(
                """
                SELECT COUNT(*) AS n, COUNT(DISTINCT equipment) AS equip,
                       MAX(logged_at) AS last_at
                FROM lifts WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
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
