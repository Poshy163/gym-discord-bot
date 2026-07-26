"""Container entrypoint: owns the database, the dashboard, and the bot process.

``python -m app.supervisor`` is PID 1. It opens and migrates
``/data/gym.sqlite3``, resolves configuration, serves the operator dashboard,
runs the nightly backup and the game-icon refresh, and spawns ``python -m
app.bot`` as a child whenever a Discord token is configured.

The point of the split is that the dashboard must work when the bot does not.
Setup with no token, a token Discord rejects, a privileged intent that is not
enabled, a crash loop, and every reconfiguration all become the same state --
"the worker isn't running right now" -- and the surface you fix it from stays
up in all of them. A single-process design cannot offer that: with no token
there is no process, so there is nothing to enter a token into.

``python -m app.bot`` still runs standalone for development and tests. It only
serves RPC when ``GYMBOT_ROLE=worker`` is in its environment, which only the
supervisor sets.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as config_mod
from . import dayspans, game_icons, secretbox, settings_service, webui, workerlink
from .db import Database
from .settings_service import DbAuth, SettingsService

LOG = logging.getLogger("gymbot.supervisor")

#: Exit codes the worker uses to explain itself. They are what let the
#: dashboard say "Discord rejected your token" instead of showing an opaque
#: crash loop.
EX_INTENT = 76   # a privileged intent is requested but not enabled
EX_AUTH = 77     # Discord rejected the token
EX_CONFIG = 78   # nothing to run yet (no token) -- an idle state, not a failure

#: Respawn backoff after an unexpected exit.
BACKOFF_SECONDS = (2, 5, 10, 30, 60)

#: A worker that survives this long is considered to have started successfully,
#: which marks its configuration good and resets the backoff.
HEALTHY_AFTER_SECONDS = 30

#: Two consecutive fast failures quarantine the worker rather than crash-loop.
QUARANTINE_AFTER_FAILURES = 2

#: Restart requests arriving within this window coalesce, so an operator
#: setting four reminder fields gets one bounce rather than four -- each of
#: which would otherwise re-run the startup backfill and command sync.
RESTART_DEBOUNCE_SECONDS = 5.0

STDERR_TAIL_LINES = 200
SHUTDOWN_GRACE_SECONDS = 20


class WorkerState:
    NOT_CONFIGURED = "not_configured"
    STARTING = "starting"
    RUNNING = "running"
    RESTARTING = "restarting"
    STOPPED = "stopped"
    QUARANTINED = "quarantined"


class WorkerSupervisor:
    """Spawns, stops and restarts the bot process, and explains what it did."""

    def __init__(self, db: Database, settings: SettingsService, *,
                 sock_path: str, run_dir: Path) -> None:
        self._db = db
        self._settings = settings
        self._sock_path = sock_path
        self._run_dir = run_dir
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._restart_handle: asyncio.TimerHandle | None = None
        self._stopping = False
        self._consecutive_failures = 0
        self._backoff_index = 0
        self.state = WorkerState.NOT_CONFIGURED
        self.detail = ""
        self.last_exit_code: int | None = None
        self.stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self.link = workerlink.WorkerLink(sock_path)
        self._pending_rev: int | None = None

    # -- status ------------------------------------------------------------

    def status(self) -> dict:
        return {
            "state": self.state,
            "detail": self.detail,
            "exit_code": self.last_exit_code,
            "headline": self._headline(),
            "log": list(self.stderr_tail) if self.state == WorkerState.QUARANTINED
            else [],
            "can_retry": self.state == WorkerState.QUARANTINED,
        }

    def _headline(self) -> str:
        if self.state == WorkerState.NOT_CONFIGURED:
            return "No Discord token yet — add one below to start the bot."
        if self.state == WorkerState.QUARANTINED:
            if self.last_exit_code == EX_AUTH:
                return ("Discord rejected the bot token. Paste a fresh one from "
                        "the Developer Portal.")
            if self.last_exit_code == EX_INTENT:
                return ("Discord refused the connection: a privileged intent is "
                        "requested but not enabled. Open discord.com/developers "
                        "→ your app → Bot → Privileged Gateway Intents and turn "
                        "on the ones named in the log below.")
            return "The bot failed to start twice in a row and has been paused."
        if self.state == WorkerState.RUNNING:
            return "Bot running."
        if self.state == WorkerState.STARTING:
            return "Bot starting…"
        if self.state == WorkerState.RESTARTING:
            return "Bot restarting…"
        return "Bot stopped."

    # -- lifecycle ---------------------------------------------------------

    async def reconcile(self) -> None:
        """Start the worker if it should be running and isn't."""
        if self._stopping or self.state == WorkerState.QUARANTINED:
            return
        cfg = self._settings.current()
        if not cfg["DISCORD_TOKEN"]:
            if self.state != WorkerState.NOT_CONFIGURED:
                self.state = WorkerState.NOT_CONFIGURED
            LOG.info(
                "Discord bot idle — no token configured. Set one in the "
                "dashboard under Settings → Discord."
            )
            return
        if self._proc is None:
            await self._spawn(cfg)

    async def _spawn(self, cfg: config_mod.Config) -> None:
        env = dict(os.environ)
        env.update(cfg.as_child_env())
        env[workerlink.ROLE_ENV] = "worker"
        env[workerlink.SOCKET_ENV] = self._sock_path

        self.state = WorkerState.STARTING
        self._pending_rev = self._db.settings_rev()
        LOG.info("Starting the bot process.")
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.bot",
            env=env,
            stdout=None,          # inherit: worker logs go straight to the
            stderr=asyncio.subprocess.PIPE,   # container log, unmodified
        )
        self._task = asyncio.create_task(self._watch(self._proc))

    async def _watch(self, proc: asyncio.subprocess.Process) -> None:
        started = asyncio.get_running_loop().time()
        healthy = asyncio.create_task(self._mark_healthy_after(proc))
        try:
            await asyncio.gather(
                self._pump_stderr(proc),
                proc.wait(),
            )
        finally:
            healthy.cancel()

        code = proc.returncode
        self.last_exit_code = code
        self._proc = None
        self.link.up = False
        ran_for = asyncio.get_running_loop().time() - started

        if self._stopping:
            self.state = WorkerState.STOPPED
            return

        if code == EX_CONFIG:
            # "No token yet" is a quiet idle state, not a failure -- never
            # quarantine on it, or clearing a token would look like a crash.
            self.state = WorkerState.NOT_CONFIGURED
            self.detail = "Waiting for a Discord token."
            return

        if code == 0:
            LOG.info("Bot exited cleanly; restarting.")
            self._consecutive_failures = 0
            self._backoff_index = 0
            await self._respawn_after(0)
            return

        if ran_for < HEALTHY_AFTER_SECONDS:
            self._consecutive_failures += 1
        else:
            self._consecutive_failures = 1

        if code in (EX_AUTH, EX_INTENT) or \
                self._consecutive_failures >= QUARANTINE_AFTER_FAILURES:
            self.state = WorkerState.QUARANTINED
            self.detail = self._headline()
            LOG.error(
                "Bot exited with code %s. Pausing restarts — fix the "
                "configuration in the dashboard, then press Retry. "
                "Backups and the dashboard keep running.", code,
            )
            return

        delay = BACKOFF_SECONDS[min(self._backoff_index, len(BACKOFF_SECONDS) - 1)]
        self._backoff_index += 1
        LOG.warning("Bot exited with code %s; retrying in %ss.", code, delay)
        await self._respawn_after(delay)

    async def _mark_healthy_after(self, proc: asyncio.subprocess.Process) -> None:
        """Flip to RUNNING and bank the config once the worker has survived."""
        await asyncio.sleep(HEALTHY_AFTER_SECONDS)
        if proc.returncode is not None:
            return
        self.state = WorkerState.RUNNING
        self.detail = ""
        self._consecutive_failures = 0
        self._backoff_index = 0
        if self._pending_rev is not None:
            self._settings.mark_good(self._pending_rev)
            self._pending_rev = None
        await self.link.reset()
        await self.link.ping()

    async def _pump_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """Forward the worker's stderr byte-for-byte, keeping a bounded tail.

        Deliberately does not re-format or annotate: LOG_FORMAT=json must stay
        parseable, and wrapping JSON lines in more JSON breaks every downstream
        log shipper.
        """
        if proc.stderr is None:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip("\n")
            self.stderr_tail.append(text)
            print(text, file=sys.stderr, flush=True)

    async def _respawn_after(self, delay: float) -> None:
        if delay:
            await asyncio.sleep(delay)
        if not self._stopping:
            await self.reconcile()

    async def stop(self, *, graceful: bool = True) -> None:
        """SIGTERM, then SIGKILL after the grace period."""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        if graceful:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), SHUTDOWN_GRACE_SECONDS)
                return
            except asyncio.TimeoutError:
                LOG.warning("Bot did not exit within %ss; killing it.",
                            SHUTDOWN_GRACE_SECONDS)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()

    async def restart(self, reason: str = "") -> None:
        """Bounce the worker now, clearing any quarantine."""
        LOG.info("Restarting the bot%s.", f" ({reason})" if reason else "")
        self.state = WorkerState.RESTARTING
        self._consecutive_failures = 0
        self._backoff_index = 0
        await self.stop()
        # Clear a socket a killed worker may have left behind, so the next
        # bind does not fail with EADDRINUSE and get misread as a crash loop.
        with contextlib.suppress(FileNotFoundError, OSError):
            os.unlink(self._sock_path)
        self._settings.clear_pending()
        await self.reconcile()

    def request_restart(self, reason: str = "") -> None:
        """Schedule a debounced restart."""
        loop = asyncio.get_running_loop()
        if self._restart_handle is not None:
            self._restart_handle.cancel()
        self._restart_handle = loop.call_later(
            RESTART_DEBOUNCE_SECONDS,
            lambda: asyncio.create_task(self.restart(reason)),
        )

    async def reload_hot(self) -> bool:
        """Push hot-reloadable settings into a running worker."""
        try:
            await self.link.call("reload_config")
            return True
        except Exception as exc:  # noqa: BLE001
            LOG.debug("Hot reload skipped (%s).", exc)
            return False

    async def shutdown(self) -> None:
        self._stopping = True
        if self._restart_handle is not None:
            self._restart_handle.cancel()
        await self.stop()
        await self.link.close()


# ---------------------------------------------------------------------------
# Schedulers that belong to the supervisor, not the bot
# ---------------------------------------------------------------------------

async def _sleep_until(hour: int, minute: int, tz) -> None:
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    await asyncio.sleep(max(1.0, (target - now).total_seconds()))


async def backup_loop(db: Database, settings: SettingsService) -> None:
    """Nightly snapshot into BACKUP_DIR.

    Moved out of the bot because it has no Discord dependency and must not stop
    when the bot does -- a quarantined bot is exactly when you most want your
    backups still running. Re-reads config each night, which is also why the
    backup settings can be changed without a restart.
    """
    while True:
        cfg = settings.current()
        tz = _tz_of(cfg)
        await _sleep_until(cfg["BACKUP_HOUR"], cfg["BACKUP_MINUTE"], tz)

        cfg = settings.current()
        keep = cfg["BACKUP_KEEP"]
        if keep <= 0:
            continue
        backup_dir = Path(cfg["BACKUP_DIR"])
        stamp = datetime.now(_tz_of(cfg)).strftime("%Y%m%d")
        dest = backup_dir / f"gym-{stamp}.sqlite3"
        try:
            await asyncio.to_thread(db.backup_to, dest)
        except Exception:
            LOG.exception("Nightly DB backup failed")
            continue
        try:
            ok, detail = await asyncio.to_thread(db.verify_snapshot, dest)
        except Exception:
            LOG.exception("DB backup verification raised: %s", dest)
        else:
            if ok:
                LOG.info("DB backup written and verified: %s", dest)
            else:
                LOG.error("DB backup FAILED verification (%s): %s", detail, dest)
        try:
            snaps = sorted(backup_dir.glob("gym-*.sqlite3"))
            for old in snaps[: max(0, len(snaps) - keep)]:
                old.unlink()
                LOG.info("Pruned old DB backup: %s", old)
        except OSError:
            LOG.exception("Backup rotation failed")


async def game_icon_loop(settings: SettingsService) -> None:
    """Refresh the game-icon map daily. Belongs with the dashboard that reads it."""
    while True:
        cfg = settings.current()
        try:
            await game_icons.refresh(
                cfg["GAME_ICONS_CACHE"],
                max_age_days=cfg["GAME_ICONS_REFRESH_DAYS"],
                force=cfg["GAME_ICONS_REFRESH_DAYS"] <= 0,
            )
        except Exception:
            LOG.exception("Game-icon refresh failed")
        await asyncio.sleep(24 * 3600)


async def unclaimed_warning_loop(auth: DbAuth) -> None:
    """Keep the open-claim window visible in the logs until someone claims it."""
    while not auth.is_claimed():
        auth.warn_if_unclaimed()
        await asyncio.sleep(60)


def _tz_of(cfg: config_mod.Config):
    from datetime import timezone as _tz
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(cfg["DISPLAY_TIMEZONE"])
    except Exception:  # noqa: BLE001
        return _tz.utc


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _acquire_lock(run_dir: Path):
    """Refuse to run two supervisors against one volume.

    Best-effort: flock semantics are unreliable on NFS, so a compose file
    copied to a second host sharing a mount is not caught here.
    """
    if sys.platform == "win32":
        return None
    import fcntl

    run_dir.mkdir(parents=True, exist_ok=True)
    handle = open(run_dir / "supervisor.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(
            "Another supervisor is already running against this data volume. "
            "Refusing to start a second one."
        )
    return handle


async def _run() -> int:
    db_path = config_mod.bootstrap_db_path()
    run_dir = Path(db_path).parent / "run"
    lock = _acquire_lock(run_dir)  # noqa: F841 - held for process lifetime

    db = Database(db_path, migrate=True, busy_timeout_ms=5000)
    box = secretbox.SecretBox.open_at(db_path)

    # Order matters enormously: never generate a Fernet key before checking
    # whether one already exists in the environment or the database, or every
    # existing deployment loses the ability to decrypt what it stored.
    secretbox.bootstrap_fernet(db, box)
    settings_service.seed_from_env_once(db, box)

    settings = SettingsService(db, box)
    cfg = settings.current()
    config_mod.install_logging(cfg)

    bad = settings_service.undecryptable_keys(db, box)
    if bad:
        LOG.error(
            "=" * 72 + "\n"
            "  ENCRYPTION KEY MISMATCH\n\n"
            "  These stored secrets cannot be decrypted with the current key:\n"
            "    %s\n\n"
            "  If you restored a database backup, restore %s alongside it.\n"
            "  Until then those integrations will report themselves "
            "unconfigured.\n" + "=" * 72,
            ", ".join(bad), box.key_path,
        )

    if cfg["WEBUI_DISABLED"]:
        LOG.warning("Dashboard disabled by WEBUI_DISABLED. Configuration can "
                    "then only come from environment variables.")

    auth = DbAuth(db, os.environ)
    sock_path = str(run_dir / "worker.sock")
    supervisor = WorkerSupervisor(db, settings, sock_path=sock_path,
                                  run_dir=run_dir)

    tz_of = lambda: _tz_of(settings.current())  # noqa: E731

    app = webui.build_app(
        db=db,
        auth=auth,
        settings=settings,
        supervisor=supervisor,
        # Computed locally from SQLite -- see app/dayspans.py for why these
        # three must never become RPC hops.
        today_window=lambda: dayspans.today_window(tz_of()),
        calorie_streak=lambda uid: dayspans.calorie_streak(db, uid, tz_of()),
        protein_streak=lambda uid: dayspans.protein_streak(db, uid, tz_of()),
        # Everything below genuinely needs a live gateway.
        resync=supervisor.link.proxy("resync"),
        list_channels=supervisor.link.proxy("list_channels"),
        invite_user=supervisor.link.proxy("invite_user"),
        set_member_role=supervisor.link.proxy("set_member_role"),
        member_moderation=supervisor.link.proxy("member_moderation"),
        remove_timeout=supervisor.link.proxy("remove_timeout"),
        set_auto_untimeout=supervisor.link.proxy("set_auto_untimeout"),
        announce_blacklist=supervisor.link.proxy("announce_blacklist"),
        voice_snapshot=supervisor.link.proxy("voice_snapshot"),
        presence_track=supervisor.link.proxy("presence_track"),
        presence_enabled=bool(cfg["ENABLE_PRESENCE_TRACKING"]),
        media_dir=cfg["MEDIA_DIR"] if cfg["ENABLE_MEDIA_DOWNLOAD"] else None,
        display_tz=_tz_of(cfg),
    )

    runner = None
    if not cfg["WEBUI_DISABLED"]:
        runner = await webui.start_server(
            app, cfg["WEBUI_BIND_HOST"], cfg["WEBUI_PORT"],
        )
        LOG.info("Dashboard listening on %s:%s",
                 cfg["WEBUI_BIND_HOST"], cfg["WEBUI_PORT"])
        if not auth.is_claimed():
            LOG.warning(
                "\n" + "=" * 72 + "\n"
                "  GYM BOT — FIRST-TIME SETUP\n\n"
                "  Open http://<this-host>:%s and set a dashboard password.\n\n"
                "  Until you do, ANYONE who can reach that port can claim this\n"
                "  instance. Do not expose it to the public internet.\n"
                + "=" * 72, cfg["WEBUI_PORT"],
            )

    await supervisor.link.start()
    await supervisor.reconcile()

    tasks = [
        asyncio.create_task(backup_loop(db, settings)),
        asyncio.create_task(game_icon_loop(settings)),
        asyncio.create_task(unclaimed_warning_loop(auth)),
        asyncio.create_task(_link_poll_loop(supervisor)),
    ]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    LOG.info("Shutting down.")
    for task in tasks:
        task.cancel()
    await supervisor.shutdown()
    if runner is not None:
        await runner.cleanup()
    db.close()
    return 0


async def _link_poll_loop(supervisor: WorkerSupervisor) -> None:
    """Keep the worker-link liveness flag fresh for the dashboard's status card."""
    while True:
        if supervisor.state in (WorkerState.RUNNING, WorkerState.STARTING):
            await supervisor.link.ping()
        await asyncio.sleep(15)


def _reset_password(db_path: str) -> int:
    """`python -m app.supervisor reset-password` -- clears the stored password.

    Anyone who can run this already has shell access to the container and its
    data, so it adds no exposure; it just avoids requiring sqlite3 surgery on
    the volume when someone locks themselves out.
    """
    db = Database(db_path, migrate=True)
    db.meta_set(settings_service.META_PASSWORD_HASH, "")
    with contextlib.suppress(Exception):
        db._connection.execute(  # noqa: SLF001 - admin path
            "DELETE FROM app_meta WHERE key = ?",
            (settings_service.META_PASSWORD_HASH,),
        )
    db.close()
    print("Dashboard password cleared. Open the dashboard to set a new one.")
    print("WARNING: until you do, anyone who can reach the port can claim it.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.supervisor")
    parser.add_argument("command", nargs="?", default="run",
                        choices=("run", "reset-password"))
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "reset-password":
        raise SystemExit(_reset_password(config_mod.bootstrap_db_path()))

    try:
        raise SystemExit(asyncio.run(_run()))
    except KeyboardInterrupt:  # pragma: no cover
        raise SystemExit(0)


if __name__ == "__main__":
    main()
