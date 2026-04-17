# SQLite storage for lift entries.

from __future__ import annotations

import sqlite3
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
    raw           TEXT
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

-- Server-local alias table: lets admins teach the bot nicknames the built-in
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
    message_id       INTEGER,
    lift_ids         TEXT,
    created_at       TEXT    NOT NULL
);
"""


@dataclass
class LiftRow:
    username: str
    equipment: str
    weight_kg: float
    bodyweight_add: bool
    logged_at: str


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

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
        (duplicates from the same message are ignored)."""
        if not lifts:
            return 0
        ts = (logged_at or datetime.now(timezone.utc)).isoformat()
        inserted = 0
        with self._conn() as c:
            for lift in lifts:
                try:
                    c.execute(
                        """
                        INSERT INTO lifts
                        (guild_id, user_id, username, equipment, weight_kg,
                         bodyweight_add, message_id, channel_id, logged_at, raw)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            guild_id, user_id, username,
                            lift.equipment, lift.weight_kg,
                            1 if lift.bodyweight_add else 0,
                            message_id, channel_id, ts, lift.raw,
                        ),
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    # dedupe on (message_id, equipment)
                    continue
        return inserted

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
                SELECT substr(l.logged_at, 1, 7) AS month,
                       MAX(l.weight_kg)          AS best,
                       MAX(l.bodyweight_add)     AS bw,
                       MIN(l.logged_at)          AS first_seen
                FROM lifts l
                WHERE l.guild_id = ? AND l.user_id = ? AND l.equipment = ?
                GROUP BY month
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
        self, guild_id: int, src: str, dst: str
    ) -> int:
        """Re-label every row from equipment=src to equipment=dst. Returns
        the number of rows affected. The unique (message_id, equipment) index
        is respected: if the destination already exists for a given message,
        the duplicate source row is dropped instead of renamed."""
        with self._conn() as c:
            # Remove rows that would collide with the dedupe index after rename.
            c.execute(
                """
                DELETE FROM lifts
                WHERE guild_id = ? AND equipment = ?
                  AND message_id IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM lifts b
                      WHERE b.guild_id = lifts.guild_id
                        AND b.message_id = lifts.message_id
                        AND b.equipment = ?
                  )
                """,
                (guild_id, src, dst),
            )
            cur = c.execute(
                "UPDATE lifts SET equipment = ? WHERE guild_id = ? AND equipment = ?",
                (dst, guild_id, src),
            )
            return cur.rowcount or 0

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

    def history(
        self, guild_id: int, user_id: int, equipment: str, limit: int = 25
    ) -> list[sqlite3.Row]:
        """Chronological per-entry history for one user/equipment."""
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT weight_kg, bodyweight_add AS bw, logged_at
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
                    datetime.now(timezone.utc).isoformat(),
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
                    added_by, datetime.now(timezone.utc).isoformat(),
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
    ) -> None:
        ids_str = ",".join(str(i) for i in (lift_ids or [])) or None
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO reply_tracking
                    (reply_message_id, guild_id, user_id,
                     message_id, lift_ids, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    reply_message_id, guild_id, user_id,
                    message_id, ids_str,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def get_reply(
        self, reply_message_id: int
    ) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                """
                SELECT reply_message_id, guild_id, user_id,
                       message_id, lift_ids, created_at
                FROM reply_tracking
                WHERE reply_message_id = ?
                """,
                (reply_message_id,),
            ).fetchone()

    def delete_reply(self, reply_message_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "DELETE FROM reply_tracking WHERE reply_message_id = ?",
                (reply_message_id,),
            )

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
        self, guild_id: int, user_id: int, ids: list[int]
    ) -> int:
        """Delete specific lift rows by id. Scoped to (guild_id, user_id)
        for safety so a stale reply record can't nuke someone else's data."""
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._conn() as c:
            cur = c.execute(
                f"DELETE FROM lifts "
                f"WHERE guild_id = ? AND user_id = ? AND id IN ({placeholders})",
                [guild_id, user_id, *ids],
            )
            return cur.rowcount or 0
