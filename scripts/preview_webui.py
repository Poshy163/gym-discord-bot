"""Run a disposable, local-only dashboard preview with fake data.

This starts the WebUI without the Discord worker or any external credentials.
Its SQLite database and encryption key live in a temporary directory and are
removed when the process exits. It is for browser/UI review only.

    python scripts/preview_webui.py
    python scripts/preview_webui.py --port 8082

Sign in with ``preview-only-password``.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import Database
from app.secretbox import SecretBox
from app.settings_service import DbAuth, SettingsService
from app.webui import build_app, start_server


PASSWORD = "preview-only-password"
GUILD_ID = 1001
ALICE_ID = 2001


def _seed(db: Database) -> None:
    """Provide enough non-sensitive data to exercise the dashboard routes."""
    db.set_guild_meta(GUILD_ID, "Preview Gym", 3)
    db.sync_guild_roles(GUILD_ID, [
        {"id": 1, "name": "Coach", "color": 0x6366F1, "position": 2,
         "managed": False},
        {"id": 2, "name": "Member", "color": 0x22D3EE, "position": 1,
         "managed": False},
    ])
    db.upsert_member(GUILD_ID, ALICE_ID, "alice", "Alice Example")
    db.upsert_member(GUILD_ID, 2002, "ben", "Ben Example")
    db.upsert_member(GUILD_ID, 2003, "bot", "Gym Bot", is_bot=True)
    db.set_member_roles(GUILD_ID, ALICE_ID, [1, 2])
    db.set_member_roles(GUILD_ID, 2002, [2])
    db.add_audit(GUILD_ID, "member", "join", subject_id=ALICE_ID,
                 subject_name="Alice Example", detail="joined the preview server")
    db.add_audit(GUILD_ID, "data", "lift_add", subject_id=2002,
                 subject_name="Ben Example", detail="logged a preview lift")
    # This is deliberately a non-usable placeholder, never a credential. The
    # WebUI only exposes the safe linked/poll metadata below.
    db.link_revo_account(ALICE_ID, "preview@example.invalid", "preview-token",
                         None, None, None, GUILD_ID, None)
    db.update_revo_checkin_state(ALICE_ID, "2026-09-06", 4)


async def _run(port: int) -> None:
    with tempfile.TemporaryDirectory(prefix="gym-dashboard-preview-") as tmp:
        path = Path(tmp) / "preview.sqlite3"
        db = Database(path)
        try:
            _seed(db)
            box = SecretBox.open_at(str(path))
            # Seed the same environment-backed password path used by an
            # existing deployment. That makes the advertised credential work
            # immediately and keeps /setup closed in the preview too.
            env = {"WEBUI_PASSWORD": PASSWORD}
            auth = DbAuth(db, env)
            settings = SettingsService(db, box, env)
            app = build_app(db=db, auth=auth, settings=settings)
            runner = await start_server(app, "127.0.0.1", port)
            print(f"Preview: http://127.0.0.1:{port}/")
            print(f"Password: {PASSWORD}")
            print("Local-only fake data; press Ctrl+C to remove it.")
            try:
                await asyncio.Event().wait()
            finally:
                await runner.cleanup()
        finally:
            db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8082)
    args = parser.parse_args()
    asyncio.run(_run(args.port))


if __name__ == "__main__":
    main()
