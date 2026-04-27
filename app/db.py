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
            self._recanonicalize_equipment()

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
                SELECT l.username,
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
        self, guild_id: int, user_id: int
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
            return row

    def pop_last_n_for_user(
        self, guild_id: int, user_id: int, n: int,
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
                       bodyweight_add AS bw, logged_at
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
                    SELECT l.username, l.equipment, l.weight_kg,
                           l.bodyweight_add AS bw, l.logged_at,
                           (
                               SELECT MAX(prev.weight_kg)
                               FROM lifts prev
                               WHERE prev.guild_id = l.guild_id
                                 AND prev.user_id = l.user_id
                                 AND prev.equipment = l.equipment
                                 AND prev.id < l.id
                           ) AS prev_best
                    FROM lifts l
                    WHERE l.guild_id = ?
                      AND l.logged_at >= ?
                      AND l.logged_at < ?
                )
                SELECT username, equipment, weight_kg, bw, logged_at, prev_best
                FROM period
                WHERE weight_kg > 0
                  AND (prev_best IS NULL OR weight_kg > prev_best)
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
        """All distinct YYYY-MM-DD dates on which the user logged at least
        one lift, ordered ascending."""
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
            return cur.rowcount or 0

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
        self, guild_id: int, user_id: int, message_id: int
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
            return cur.rowcount or 0

    def delete_lifts_by_ids(
        self, guild_id: int, user_id: int | None, ids: list[int]
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
            return cur.rowcount or 0

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
