"""One-shot maintenance: list or delete specific lift rows.

Usage (inside the container so DB_PATH resolves to /data/gym.sqlite3):

  # See recent rows for a user + equipment
  python -m scripts.cleanup_lift list --user-id 123 --equipment "bench press"

  # Delete a specific row by id (also adds backfill suppression so the
  # source post can't be resurrected on reboot)
  python -m scripts.cleanup_lift delete --id 456
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys


def _connect() -> sqlite3.Connection:
    path = os.getenv("DB_PATH", "/data/gym.sqlite3")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def cmd_list(args: argparse.Namespace) -> int:
    sql = (
        "SELECT id, guild_id, user_id, username, equipment, weight_kg, "
        "bodyweight_add, message_id, logged_at "
        "FROM lifts WHERE 1=1"
    )
    params: list[object] = []
    if args.user_id is not None:
        sql += " AND user_id = ?"
        params.append(args.user_id)
    if args.equipment:
        sql += " AND equipment = ?"
        params.append(args.equipment.lower())
    sql += " ORDER BY logged_at DESC LIMIT ?"
    params.append(args.limit)

    with _connect() as c:
        for row in c.execute(sql, params):
            print(dict(row))
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    with _connect() as c:
        row = c.execute(
            "SELECT id, guild_id, user_id, equipment, weight_kg, message_id "
            "FROM lifts WHERE id = ?",
            (args.id,),
        ).fetchone()
        if row is None:
            print(f"No lift with id={args.id}", file=sys.stderr)
            return 1
        print("About to delete:", dict(row))
        if not args.yes:
            print("Re-run with --yes to confirm.", file=sys.stderr)
            return 2
        c.execute("DELETE FROM lifts WHERE id = ?", (args.id,))
        if row["message_id"] is not None:
            c.execute(
                "INSERT OR REPLACE INTO suppressed_messages "
                "(guild_id, message_id, suppressed_at) "
                "VALUES (?, ?, datetime('now'))",
                (row["guild_id"], row["message_id"]),
            )
            print(f"Suppressed source message {row['message_id']}.")
        print("Deleted.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list")
    pl.add_argument("--user-id", type=int)
    pl.add_argument("--equipment")
    pl.add_argument("--limit", type=int, default=20)
    pl.set_defaults(func=cmd_list)

    pd = sub.add_parser("delete")
    pd.add_argument("--id", type=int, required=True)
    pd.add_argument("--yes", action="store_true")
    pd.set_defaults(func=cmd_delete)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
