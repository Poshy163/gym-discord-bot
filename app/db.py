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
