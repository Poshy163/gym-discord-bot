"""Dashboard-facing settings operations and dashboard authentication.

Two things live here because they share the same store and the same actor
plumbing:

* :class:`SettingsService` -- read/write/validate/export the settings the
  Settings tab edits, deciding for each write whether it takes effect
  immediately, needs the bot to bounce, or is inert because the environment is
  pinning that key.
* :class:`DbAuth` -- the dashboard password: a PBKDF2 hash in ``app_meta``,
  plus the first-boot claim flow.

Neither imports ``app.bot``; both are usable from the supervisor alone.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from typing import Any, Mapping

from . import config as config_mod
from .secretbox import SecretBox, SecretBoxUnavailable

LOG = logging.getLogger("gymbot.settings")

#: app_meta keys owned by this module. The password hash deliberately does NOT
#: live in app_settings, so it can never be enumerated by GET /api/settings nor
#: land in an export.
META_PASSWORD_HASH = "webui_password_hash"
META_SEEDED = "settings_seeded_v1"
META_CONFIG_REV_GOOD = "config_rev_good"

PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 12


# ---------------------------------------------------------------------------
# Password hashing (stdlib only -- no new dependency)
# ---------------------------------------------------------------------------

def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>``."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "$".join((
        "pbkdf2_sha256",
        str(iterations),
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    ))


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of ``password`` against a stored hash."""
    try:
        scheme, raw_iters, salt_b64, hash_b64 = encoded.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(raw_iters)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(digest, expected)


class DbAuth:
    """Dashboard password, stored hashed, with a first-boot claim flow.

    An instance is "claimed" once either a password hash exists in the database
    or ``WEBUI_PASSWORD`` is set in the environment. Until then the dashboard
    binds its port but refuses every route except the claim page -- safety
    comes from this object, not from declining to listen.

    The claim itself is deliberately open: whoever reaches the dashboard first
    sets the password. That is a race an internet-reachable deployment can lose
    to a port scanner, so the unclaimed state is logged loudly and the claiming
    client's IP is recorded. Put the dashboard behind a VPN or reverse proxy if
    the host is public.
    """

    def __init__(self, db: Any, env: Mapping[str, str] | None = None) -> None:
        self._db = db
        self._env = os.environ if env is None else env
        self._claimed_by: str | None = None

    # -- state -------------------------------------------------------------

    def env_password(self) -> str:
        return (self._env.get("WEBUI_PASSWORD") or "").strip()

    def stored_hash(self) -> str | None:
        try:
            return self._db.meta_get(META_PASSWORD_HASH)
        except Exception as exc:  # noqa: BLE001
            LOG.error("Could not read the dashboard password hash (%s).", exc)
            return None

    def is_claimed(self) -> bool:
        return bool(self.env_password()) or bool(self.stored_hash())

    # -- use ---------------------------------------------------------------

    def verify(self, supplied: str) -> bool:
        """True when ``supplied`` matches the environment pin or the stored hash.

        Both are accepted when both exist, so an operator can set a password in
        the dashboard, confirm it works, and only then remove the environment
        pin from their compose file.
        """
        env_password = self.env_password()
        if env_password and hmac.compare_digest(supplied, env_password):
            return True
        stored = self.stored_hash()
        return bool(stored) and verify_password(supplied, stored)

    def claim(self, password: str, *, actor: str) -> str | None:
        """Set the initial password. Returns an error message, or None on success.

        Refuses once claimed, so this can never be used to reset a password
        without knowing the old one.
        """
        if self.is_claimed():
            return "This dashboard has already been set up."
        error = self.password_problem(password)
        if error:
            return error
        self._db.meta_set(META_PASSWORD_HASH, hash_password(password))
        self._claimed_by = actor
        LOG.warning(
            "Dashboard claimed by %s -- the initial password has been set.",
            actor,
        )
        return None

    def change(self, old: str, new: str) -> str | None:
        """Rotate the password. Returns an error message, or None on success."""
        if not self.verify(old):
            return "Current password is incorrect."
        error = self.password_problem(new)
        if error:
            return error
        self._db.meta_set(META_PASSWORD_HASH, hash_password(new))
        if self.env_password():
            LOG.warning(
                "Dashboard password changed, but WEBUI_PASSWORD is still set in "
                "the environment and will keep working until you remove it."
            )
        return None

    @staticmethod
    def password_problem(password: str) -> str | None:
        if len(password) < MIN_PASSWORD_LENGTH:
            return f"Use at least {MIN_PASSWORD_LENGTH} characters."
        return None

    def warn_if_unclaimed(self) -> None:
        """Log the open-claim state prominently.

        Called on a timer while unclaimed so the window is visible in the logs
        of anyone watching, since the claim itself is unauthenticated.
        """
        if self.is_claimed():
            return
        LOG.warning(
            "DASHBOARD IS UNCLAIMED -- anyone who can reach this port can set "
            "the password and take control of the bot. Open it now and set a "
            "password, and keep the port off the public internet."
        )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class SettingsService:
    """Validated reads and writes over ``app_settings``."""

    def __init__(self, db: Any, box: SecretBox,
                 env: Mapping[str, str] | None = None) -> None:
        self._db = db
        self._box = box
        self._env = os.environ if env is None else env
        #: Keys written since the last apply whose scope is not "hot".
        self.pending: set[str] = set()

    # -- reads -------------------------------------------------------------

    def current(self) -> config_mod.Config:
        return config_mod.load(self._db, self._env,
                               decrypt=self._box.decryptor())

    def describe(self) -> dict[str, Any]:
        """The whole Settings tab payload: groups, fields, values, sources.

        Secrets never leave here in plaintext -- each reports only whether it
        is set, plus a short masked hint for the ones where a hint is harmless.
        """
        cfg = self.current()
        # Read the raw rows ONCE. _field needs them to show "what this would
        # become if you unpinned it", and querying per field would mean ~90
        # round trips to build one page.
        try:
            rows = {r["key"]: r for r in self._db.settings_rows()}
        except Exception:  # noqa: BLE001
            rows = {}
        groups = []
        for group in config_mod.GROUP_ORDER:
            items = [
                self._field(cfg, s, rows)
                for s in config_mod.SPEC.values() if s.group == group
            ]
            if items:
                groups.append({
                    "key": group,
                    "label": config_mod.GROUP_LABELS[group],
                    "items": items,
                })
        return {
            "groups": groups,
            "pending": sorted(self.pending),
            "rev": self._db.settings_rev(),
            "crypto": {
                "available": self._box.available(),
                "key_path": str(self._box.key_path) if self._box.key_path else None,
            },
        }

    def _field(self, cfg: config_mod.Config, s: config_mod.Setting,
               rows: dict[str, Any]) -> dict[str, Any]:
        source = cfg.source(s.key)
        raw = cfg.raw(s.key)
        field: dict[str, Any] = {
            "key": s.key,
            "kind": s.kind,
            "label": s.label or s.key.replace("_", " ").capitalize(),
            "help": s.help,
            "group": s.group,
            "apply": s.apply,
            "secret": s.secret,
            "source": source,
            "pinned": source == "env",
            "editable": s.apply != "env-only",
            "default": s.default,
            "choices": list(s.choices),
            "min": s.min,
            "max": s.max,
            "restart_note": s.restart_note,
            "pending": s.key in self.pending,
        }
        if s.secret:
            field["is_set"] = bool(raw)
            field["masked"] = _mask(s.key, raw)
            field["value"] = ""
        else:
            field["value"] = raw
            # What the value would become if the environment pin were removed,
            # so "Saved but not in effect" can show the operator what is waiting.
            if source == "env":
                field["stored"] = _stored_plain(rows, s.key)
        if s.derive is not None and not raw.strip():
            field["derived"] = _display(cfg[s.key])
        return field

    # -- writes ------------------------------------------------------------

    def set(self, key: str, value: str | None, *, actor: str) -> dict[str, Any]:
        """Validate and store one setting.

        Returns a result dict. ``effective`` is False when the environment is
        pinning this key: the write still happens and is real the moment the
        pin is removed, but it changes nothing right now, and the caller is
        expected to say so rather than showing a bare "Saved".
        """
        s = config_mod.SPEC.get(key)
        if s is None:
            return {"ok": False, "field": key, "error": f"Unknown setting {key!r}."}

        if value is None:
            stored_value, encrypted = None, False
        else:
            error = config_mod.validate(key, value)
            if error:
                return {"ok": False, "field": key, "error": error}
            if s.secret and value != "":
                stored_value, encrypted = self._box.seal(value)
                if not encrypted:
                    LOG.warning(
                        "Storing %s WITHOUT encryption -- 'cryptography' is not "
                        "available.", key,
                    )
            else:
                stored_value, encrypted = value, False

        rev = self._db.settings_set(
            key, stored_value, encrypted=encrypted, actor=actor,
        )

        cfg = self.current()
        pinned = cfg.pinned(key)
        if s.apply != "hot" and not pinned:
            self.pending.add(key)

        LOG.info("Setting %s changed by %s (rev %d).", key, actor, rev)
        return {
            "ok": True,
            "key": key,
            "rev": rev,
            "apply": s.apply,
            "effective": not pinned,
            "pinned_by": "environment" if pinned else None,
            "needs_restart": s.apply != "hot" and not pinned,
        }

    def clear_pending(self) -> None:
        self.pending.clear()

    def mark_good(self, rev: int) -> None:
        """Record ``rev`` as a configuration that produced a bot which started."""
        self._db.meta_set(META_CONFIG_REV_GOOD, str(rev))

    def last_good_rev(self) -> int:
        try:
            return int(self._db.meta_get(META_CONFIG_REV_GOOD) or 0)
        except (TypeError, ValueError):
            return 0

    def revert_to_last_good(self, *, actor: str) -> dict[str, Any]:
        """Replay settings history back to the last known-good revision.

        Honest about its limits: a secret's history rows are redacted, so a bad
        secret can only be CLEARED, not restored to its previous value. The
        caller surfaces that distinction rather than implying a full undo.
        """
        good = self.last_good_rev()
        rows = self._db.settings_since(good)
        if not rows:
            return {"ok": False, "error": "Nothing to revert."}

        reverted, cleared = [], []
        for row in reversed(rows):  # newest first, so the oldest write wins last
            key = row["key"]
            if row["redacted"]:
                self._db.settings_set(key, None, encrypted=False, actor=actor)
                cleared.append(key)
            else:
                self._db.settings_set(
                    key, row["old_value"], encrypted=False, actor=actor,
                )
                reverted.append(key)
        self.pending.clear()
        LOG.warning("Reverted settings to revision %d by %s.", good, actor)
        return {
            "ok": True, "rev": good,
            "reverted": sorted(set(reverted)),
            "cleared": sorted(set(cleared)),
        }

    # -- export ------------------------------------------------------------

    def export_env(self) -> str:
        """Reproduce the current configuration as a ``.env`` file.

        Secrets in plaintext -- this is the downgrade, migration and
        "I don't trust this new database thing" escape hatch, and it is
        useless if it redacts. The caller gates it behind a confirmation.
        """
        cfg = self.current()
        lines = [
            "# Generated by the gym bot dashboard.",
            "# Contains every secret IN PLAINTEXT. Treat it like a password file.",
            "# Anything set here overrides the dashboard when the bot restarts.",
            "",
        ]
        for group in config_mod.GROUP_ORDER:
            settings = [s for s in config_mod.SPEC.values() if s.group == group]
            written = False
            for s in settings:
                raw = cfg.raw(s.key)
                if not raw or cfg.source(s.key) == "default":
                    continue
                if not written:
                    lines.append(f"# ---- {config_mod.GROUP_LABELS[group]} ----")
                    written = True
                lines.append(f"{s.key}={raw}")
            if written:
                lines.append("")
        return "\n".join(lines)


def _stored_plain(rows: dict[str, Any], key: str) -> str:
    """The stored value for ``key``, or "" — never a secret.

    Used only to show what a pinned field would become once the environment pin
    is removed, so it must refuse to surface an encrypted value.
    """
    row = rows.get(key)
    if row is None or row["value"] is None or row["encrypted"]:
        return ""
    return row["value"]


def _mask(key: str, raw: str) -> str:
    """A hint that identifies a value without leaking it.

    Tokens whose prefix identifies the application (the Discord token, the
    Fernet keys) get no hint at all -- a Discord token's first segment is the
    base64 application ID.
    """
    if not raw:
        return ""
    if key == "DISCORD_TOKEN" or key.endswith("_FERNET_KEY"):
        return "•" * 8
    if len(raw) <= 4:
        return "•" * 8
    return "•" * 8 + "·" + raw[-4:]


def _display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (set, frozenset)):
        return ", ".join(str(v) for v in sorted(value))
    return str(value)


# ---------------------------------------------------------------------------
# One-time environment seed
# ---------------------------------------------------------------------------

def seed_from_env_once(db: Any, box: SecretBox,
                       env: Mapping[str, str] | None = None) -> int:
    """Copy the operator's existing environment into ``app_settings``, once.

    Only keys ACTUALLY PRESENT and non-empty in the environment are seeded.
    Code defaults are deliberately not written: an explicit row would freeze
    that default forever and silently block a future change to it.

    Exactly-once by three independent mechanisms, so it can never fight an
    operator's dashboard edit: the ``settings_seeded_v1`` sentinel; INSERT OR
    IGNORE, which no-ops against any existing row even if the sentinel is lost;
    and rows never being deleted, so a cleared setting cannot be resurrected.

    Returns how many rows were inserted.
    """
    env = os.environ if env is None else env

    if db.meta_get(META_SEEDED):
        return 0

    pairs: list[tuple[str, str | None, bool]] = []
    for key, s in config_mod.SPEC.items():
        raw = env.get(key)
        if raw is None or raw == "":
            continue
        if s.apply == "env-only":
            # Pointless to seed: these are only ever read from the environment,
            # and a stored copy would be misleading in the UI.
            continue
        if s.secret:
            stored, encrypted = box.seal(raw)
            pairs.append((key, stored, encrypted))
        else:
            pairs.append((key, raw, False))

    # ENABLE_MEMBER_MIRROR is new, and getting it wrong bricks every upgrade.
    #
    # Before this change, `WEBUI_ENABLED = bool(WEBUI_PASSWORD) and not
    # WEBUI_DISABLED` was what turned on intents.members. The dashboard now
    # always exists, so if the flag simply meant "is there a dashboard" it would
    # be permanently true -- requesting the PRIVILEGED Server Members intent for
    # every deployment. Discord refuses the gateway for anyone who has not
    # enabled that toggle, so every deployment that never ran the dashboard
    # would crash-loop on first boot of the new image.
    #
    # So compute it from the PRE-UPGRADE environment instead. A fresh install
    # gets "false" and opts in from the Settings tab.
    if "ENABLE_MEMBER_MIRROR" not in {p[0] for p in pairs}:
        had_dashboard = bool((env.get("WEBUI_PASSWORD") or "").strip()) and (
            (env.get("WEBUI_DISABLED") or "0").lower()
            not in ("1", "true", "yes", "y", "on")
        )
        pairs.append(("ENABLE_MEMBER_MIRROR",
                      "true" if had_dashboard else "false", False))

    inserted = db.settings_seed(pairs) if pairs else 0
    db.meta_set(META_SEEDED, "1")

    if inserted:
        LOG.info(
            "Imported %d existing settings from the environment. They are "
            "editable in the dashboard's Settings tab, but your environment "
            "still takes priority until you remove it from docker-compose.yml.",
            inserted,
        )
    return inserted


def undecryptable_keys(db: Any, box: SecretBox) -> list[str]:
    """Secret settings whose stored value will not decrypt with the current key.

    Surfaced at boot so a lost or swapped ``.secret_key`` presents as "your
    encryption key does not match this database" rather than as an assortment
    of integrations quietly reporting themselves unconfigured.
    """
    bad: list[str] = []
    try:
        rows = db.settings_rows()
    except Exception:  # noqa: BLE001
        return bad
    for row in rows:
        if not row["encrypted"] or row["value"] is None:
            continue
        try:
            box.open_secret(row["value"])
        except SecretBoxUnavailable:
            bad.append(row["key"])
    return bad
