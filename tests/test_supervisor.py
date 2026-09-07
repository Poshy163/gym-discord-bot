from __future__ import annotations

import asyncio
import threading

import pytest

from app import supervisor


def test_failed_backup_verification_preserves_known_good_snapshots(
    tmp_path, monkeypatch,
):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    old_one = backup_dir / "gym-20260101.sqlite3"
    old_two = backup_dir / "gym-20260102.sqlite3"
    old_one.write_bytes(b"known-good-one")
    old_two.write_bytes(b"known-good-two")

    class StopLoop(Exception):
        pass

    class Settings:
        calls = 0

        def current(self):
            self.calls += 1
            if self.calls > 2:
                raise StopLoop
            return {
                "BACKUP_HOUR": 3,
                "BACKUP_MINUTE": 30,
                "BACKUP_KEEP": 1,
                "BACKUP_DIR": str(backup_dir),
                "DISPLAY_TIMEZONE": "UTC",
            }

    class BrokenBackup:
        def backup_to(self, dest):
            dest.write_bytes(b"invalid-new-snapshot")

        @staticmethod
        def verify_snapshot(_dest):
            return False, "simulated corruption"

    async def no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(supervisor, "_sleep_until", no_sleep)
    with pytest.raises(StopLoop):
        asyncio.run(supervisor.backup_loop(BrokenBackup(), Settings()))

    assert old_one.read_bytes() == b"known-good-one"
    assert old_two.read_bytes() == b"known-good-two"
    assert sorted(backup_dir.iterdir()) == [old_one, old_two]


def test_shutdown_drains_blocked_backup_thread_before_database_close(
    tmp_path, monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    ordering = []

    class Settings:
        def current(self):
            return {
                "BACKUP_HOUR": 3,
                "BACKUP_MINUTE": 30,
                "BACKUP_KEEP": 1,
                "BACKUP_DIR": str(tmp_path),
                "DISPLAY_TIMEZONE": "UTC",
            }

    class BlockingDatabase:
        def backup_to(self, _dest):
            entered.set()
            assert release.wait(5)
            ordering.append("backup-finished")
            finished.set()

        def close(self):
            assert finished.is_set()
            ordering.append("database-closed")

    async def no_sleep(*_args, **_kwargs):
        return None

    async def exercise():
        db = BlockingDatabase()
        task = asyncio.create_task(supervisor.backup_loop(db, Settings()))
        assert await asyncio.to_thread(entered.wait, 5)
        draining = asyncio.create_task(supervisor._cancel_and_drain([task]))
        await asyncio.sleep(0)
        assert not draining.done()
        release.set()
        await draining
        db.close()

    monkeypatch.setattr(supervisor, "_sleep_until", no_sleep)
    asyncio.run(exercise())
    assert ordering == ["backup-finished", "database-closed"]
