"""Encryption at rest for settings the operator would not want in a backup.

The key lives at ``<db dir>/.secret_key`` (mode 0600), created with O_EXCL so
it is never briefly world-readable. ``/data`` is already chowned to the bot's
uid and is the only writable volume, so this needs no new mount.

What this protects, stated plainly, because the honest scope is narrow:

* **Protects: snapshots that leave the box.** ``Database.backup_to`` copies the
  database file and nothing else, so the nightly ``/data/backups/*.sqlite3``
  that operators are told to sync off-box with rclone or restic contain
  ciphertext with no key. That property is the entire reason the key is a
  separate file rather than a row in the database it protects.
* **Does NOT protect against read access to /data.** The key sits beside the
  ciphertext, owned by the same uid the bot runs as. A container escape, a host
  compromise, ``docker cp``, or a ``docker exec`` shell yields both halves. This
  is not defence in depth against an attacker on the box.
* **Versus the old arrangement:** the keys move from a ``.env`` beside the
  compose file into the data volume. Better for anyone who backs up ``/data``,
  worse for nobody -- a ``.env`` was already in the same blast radius as the
  compose file, and ``.env`` files reach git far more often than volumes do.

The alternative (an operator-supplied passphrase typed at every boot) would
defeat the point of unattended ``restart: unless-stopped``.
"""
from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Mapping

try:  # pragma: no cover - exercised by the dependency-missing path
    from cryptography.fernet import Fernet, InvalidToken  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001 - cryptography is a soft dependency
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment,misc]

LOG = logging.getLogger("gymbot.secretbox")

KEY_FILENAME = ".secret_key"

#: The names the sibling clients look for, in the order a generated key must
#: satisfy them. ``revo_client`` reads only REVO_FERNET_KEY and has no fallback
#: chain (app/revo_client.py:81); ``strava_client`` falls back to it
#: (app/strava_client.py:104), ``hevy_client`` falls back to both
#: (app/hevy_client.py:68) and ``ha_client`` falls back to all three
#: (app/ha_client.py). So a single key stored as REVO_FERNET_KEY is the only
#: choice that serves every one of them at once.
FERNET_KEYS = (
    "REVO_FERNET_KEY", "STRAVA_FERNET_KEY", "HEVY_FERNET_KEY", "HA_FERNET_KEY",
)
GENERATED_FERNET_KEY = "REVO_FERNET_KEY"


class SecretBoxUnavailable(RuntimeError):
    """Raised when encryption was requested but ``cryptography`` is missing."""


class SecretBox:
    """Seals and opens secret setting values.

    Use :meth:`open_at` rather than constructing directly -- it handles key
    creation and the missing-dependency path.
    """

    __slots__ = ("_fernet", "_path")

    def __init__(self, key: bytes | None, path: Path | None) -> None:
        self._path = path
        self._fernet = Fernet(key) if (key and Fernet is not None) else None

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open_at(cls, db_path: str) -> "SecretBox":
        """Load (or create) the key file next to ``db_path``.

        Never raises: a box that cannot encrypt still works, it just stores
        values in the clear and reports :meth:`available` as False so the
        dashboard can show a banner.
        """
        if Fernet is None:
            LOG.error(
                "The 'cryptography' package is not installed -- secrets will "
                "be stored UNENCRYPTED. Install it (pip install cryptography) "
                "and restart to fix this."
            )
            return cls(None, None)

        if db_path == ":memory:":
            # An in-memory database is a test or a probe. Generate an ephemeral
            # key rather than writing .secret_key into the working directory,
            # which is where Path(":memory:").parent resolves to.
            return cls(Fernet.generate_key(), None)

        path = Path(db_path).parent / KEY_FILENAME
        try:
            key = cls._read_or_create(path)
        except OSError as exc:
            LOG.error(
                "Could not open or create the encryption key at %s (%s) -- "
                "secrets will be stored UNENCRYPTED.", path, exc,
            )
            return cls(None, None)
        return cls(key, path)

    @staticmethod
    def _read_or_create(path: Path) -> bytes:
        if path.exists():
            key = path.read_bytes().strip()
            if key:
                return key
            # A zero-byte key file (an interrupted first write, or a truncating
            # restore) must be removed, not just logged about: the O_EXCL
            # create below would raise FileExistsError, the handler would read
            # the same empty bytes back, and the box would be permanently
            # unavailable on an otherwise perfectly writable volume -- which in
            # turn makes every secret fall back to being stored in the clear.
            LOG.warning("Encryption key at %s was empty -- regenerating.", path)
            with contextlib.suppress(OSError):
                path.unlink()

        path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        # O_EXCL so we never overwrite a key another process just wrote, and
        # 0600 from the moment of creation rather than a chmod afterwards --
        # otherwise there is a window where the key is world-readable.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            # Another process won the race. Use its key -- but refuse to accept
            # an empty one, which would silently disable encryption.
            existing = path.read_bytes().strip()
            if not existing:
                raise OSError(f"encryption key at {path} is empty")
            return existing
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        LOG.info(
            "Generated a new encryption key at %s. BACK THIS FILE UP with your "
            "database -- without it every stored secret and every linked "
            "Revo/Strava/Hevy account becomes unreadable.", path,
        )
        return key

    # -- use ---------------------------------------------------------------

    def available(self) -> bool:
        return self._fernet is not None

    @property
    def key_path(self) -> Path | None:
        return self._path

    def seal(self, plaintext: str) -> tuple[str, bool]:
        """Encrypt ``plaintext``. Returns ``(stored_value, encrypted)``.

        Falls back to storing in the clear (with ``encrypted=False``) when no
        key is available, so a missing optional dependency degrades rather than
        losing the operator's input.
        """
        if self._fernet is None:
            return plaintext, False
        return self._fernet.encrypt(plaintext.encode()).decode(), True

    def open_secret(self, stored: str) -> str:
        """Decrypt a sealed value.

        Raises :class:`SecretBoxUnavailable` on failure rather than returning
        an empty string. That distinction matters: an undecryptable
        DISCORD_TOKEN must surface as "your key is wrong", not as "no token
        configured", or a key mishap looks like an unconfigured bot.
        """
        if self._fernet is None:
            raise SecretBoxUnavailable(
                "No encryption key available to decrypt this value."
            )
        try:
            return self._fernet.decrypt(stored.encode()).decode()
        except InvalidToken as exc:
            raise SecretBoxUnavailable(
                "Stored secret could not be decrypted with the current key. "
                "If you restored a backup, restore .secret_key alongside it."
            ) from exc

    def decryptor(self):
        """A ``callable(stored) -> str`` for ``Database.settings_all``.

        Returns the ciphertext unchanged on failure and logs once, so one bad
        row cannot stop the whole config from loading.
        """
        def _decrypt(stored: str) -> str:
            try:
                return self.open_secret(stored)
            except SecretBoxUnavailable as exc:
                LOG.error("%s", exc)
                return ""
        return _decrypt


def bootstrap_fernet(db, box: SecretBox,
                     env: Mapping[str, str] | None = None) -> str | None:
    """Make sure exactly one Fernet key exists, without ever destroying one.

    The ordering here is the difference between a smooth upgrade and making
    every linked account unreadable, so it is deliberately boring:

    1. Any ``*_FERNET_KEY`` present in the ENVIRONMENT wins and is left alone.
       A host that set only ``STRAVA_FERNET_KEY`` keeps exactly today's
       behaviour, including ``revo_client``'s deliberate lack of a fallback
       chain -- we do not "helpfully" fill that gap, because doing so changes
       which key decrypts what.
    2. Otherwise a key already in ``app_settings`` is used.
    3. Otherwise -- and only when none of the three exists in either place --
       generate exactly one and store it as ``REVO_FERNET_KEY``, the last entry
       in both the Strava and Hevy fallback chains, so one key serves all three.

    Returns the name of the key that was generated, or None when nothing was.
    """
    env = os.environ if env is None else env

    for name in FERNET_KEYS:
        if (env.get(name) or "").strip():
            LOG.debug("%s is set in the environment -- not generating a key.", name)
            return None

    try:
        stored = db.settings_all(decrypt=None)
    except Exception as exc:  # noqa: BLE001
        LOG.error("Could not read stored settings while bootstrapping the "
                  "encryption key (%s) -- not generating one.", exc)
        return None

    for name in FERNET_KEYS:
        if (stored.get(name) or "").strip():
            LOG.debug("%s is already stored -- not generating a key.", name)
            return None

    if Fernet is None:
        LOG.warning(
            "No Fernet key is configured and 'cryptography' is not installed, "
            "so one cannot be generated. The Revo, Strava and Hevy "
            "integrations will report themselves unavailable until you "
            "install it and restart."
        )
        return None

    key = Fernet.generate_key().decode()
    # Sealed with the FILE key, never stored in the clear. Storing it plainly
    # would put it in every nightly snapshot beside the very Revo/Strava/Hevy
    # ciphertext it protects, which would silently destroy the ciphertext-only
    # property that is the whole reason .secret_key is a separate file.
    stored_value, encrypted = box.seal(key)
    if not encrypted:
        LOG.error(
            "Generated a Fernet key but cannot encrypt it at rest, so it would "
            "be written to the database in the clear -- and your nightly "
            "snapshots would then carry both the ciphertext and its key. "
            "Refusing to store it. Install 'cryptography' and restart."
        )
        return None
    db.settings_set(
        GENERATED_FERNET_KEY, stored_value, encrypted=True, actor="bootstrap",
    )
    LOG.info(
        "Generated %s and stored it encrypted in the database. It protects "
        "every linked member's Revo password and the stored Strava and Hevy "
        "credentials. Back up %s alongside your database -- without it those "
        "credentials cannot be recovered.",
        GENERATED_FERNET_KEY, box.key_path,
    )
    return GENERATED_FERNET_KEY
