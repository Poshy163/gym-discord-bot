"""Storage and seeding behaviour for operator settings.

The seeding rules are the load-bearing part: they are what makes an existing
deployment upgrade without changing behaviour, and what stops a later re-seed
from clobbering an edit the operator made in the dashboard.
"""
from __future__ import annotations

import pytest

from app import config as config_mod
from app import settings_service
from app.db import Database
from app.secretbox import Fernet, SecretBox, bootstrap_fernet


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "s.sqlite3")
    yield d
    d.close()


@pytest.fixture()
def box(tmp_path):
    return SecretBox.open_at(str(tmp_path / "s.sqlite3"))


# ---------------------------------------------------------------------------
# Table behaviour
# ---------------------------------------------------------------------------

def test_set_and_read_back(db):
    db.settings_set("REMINDER_HOUR", "9", actor="web:1.2.3.4")
    assert db.settings_all()["REMINDER_HOUR"] == "9"
    row = {r["key"]: r for r in db.settings_rows()}["REMINDER_HOUR"]
    assert row["updated_by"] == "web:1.2.3.4"


def test_revision_increments_on_every_write(db):
    assert db.settings_rev() == 0
    assert db.settings_set("A", "1") == 1
    assert db.settings_set("B", "2") == 2
    assert db.settings_rev() == 2


def test_history_records_the_transition(db):
    db.settings_set("REMINDER_HOUR", "9", actor="alice")
    db.settings_set("REMINDER_HOUR", "10", actor="bob")
    history = db.settings_history()
    assert history[0]["key"] == "REMINDER_HOUR"
    assert history[0]["old_value"] == "9"
    assert history[0]["new_value"] == "10"
    assert history[0]["actor"] == "bob"


def test_clearing_writes_null_but_keeps_the_row(db):
    db.settings_set("REMINDER_HOUR", "9")
    db.settings_set("REMINDER_HOUR", None)
    assert db.settings_all()["REMINDER_HOUR"] is None
    assert "REMINDER_HOUR" in {r["key"] for r in db.settings_rows()}


def test_secret_history_is_redacted_both_ways(db):
    """A secret's value must never be readable out of the history table."""
    db.settings_set("DISCORD_TOKEN", "ciphertext-1", encrypted=True)
    db.settings_set("DISCORD_TOKEN", "ciphertext-2", encrypted=True)
    for row in db.settings_history():
        if row["key"] == "DISCORD_TOKEN":
            assert row["redacted"] == 1
            assert row["old_value"] is None
            assert row["new_value"] is None


def test_clearing_a_secret_stays_redacted(db):
    """Clearing writes encrypted=0, but the OLD value was secret.

    Without carrying redaction forward from the previous row, clearing a token
    would write its plaintext-or-ciphertext into a table the dashboard renders.
    """
    db.settings_set("DISCORD_TOKEN", "ciphertext", encrypted=True)
    db.settings_set("DISCORD_TOKEN", None, encrypted=False)
    latest = db.settings_history()[0]
    assert latest["key"] == "DISCORD_TOKEN"
    assert latest["redacted"] == 1
    assert latest["old_value"] is None


def test_secret_history_is_redacted_even_when_stored_unencrypted(db):
    """The leak this guards is the reason redaction follows the SPEC, not the
    storage flag.

    If the SecretBox is unavailable -- no cryptography, or a zero-byte
    /data/.secret_key -- SettingsService.set falls back to storing the value in
    the clear. Deriving redaction from `encrypted` alone then put the raw bot
    token into the history table, which the dashboard renders in "Recent
    changes" on the very page that masks the field itself.
    """
    db.settings_set("DISCORD_TOKEN", "raw-token-in-the-clear", encrypted=False)
    rows = db.settings_history()
    assert rows[0]["key"] == "DISCORD_TOKEN"
    assert rows[0]["redacted"] == 1
    assert rows[0]["new_value"] is None
    assert "raw-token-in-the-clear" not in str([dict(r) for r in rows])


def test_non_secret_history_is_not_redacted(db):
    """The counterpart: ordinary settings must stay visible in the history."""
    db.settings_set("REMINDER_HOUR", "9", encrypted=False)
    assert db.settings_history()[0]["new_value"] == "9"


def test_empty_key_file_is_regenerated_rather_than_disabling_encryption(tmp_path):
    """A zero-byte .secret_key used to yield a permanently unavailable box on a
    perfectly writable volume, silently downgrading every secret to plaintext."""
    key_path = tmp_path / ".secret_key"
    key_path.write_bytes(b"")
    box = SecretBox.open_at(str(tmp_path / "gym.sqlite3"))
    if Fernet is None:
        pytest.skip("cryptography not installed")
    assert box.available()
    assert key_path.read_bytes().strip()


def test_seed_never_overwrites_an_existing_row(db):
    db.settings_set("REMINDER_HOUR", "9")
    db.settings_seed([("REMINDER_HOUR", "23", False)])
    assert db.settings_all()["REMINDER_HOUR"] == "9"


def test_seed_cannot_resurrect_a_cleared_setting(db):
    """Rows are never deleted precisely so this can't happen."""
    db.settings_set("REMINDER_HOUR", "9")
    db.settings_set("REMINDER_HOUR", None)
    db.settings_seed([("REMINDER_HOUR", "23", False)])
    assert db.settings_all()["REMINDER_HOUR"] is None


def test_settings_since_returns_rows_after_a_revision(db):
    db.settings_set("A", "1")
    good = db.settings_rev()
    db.settings_set("B", "2")
    db.settings_set("C", "3")
    assert [r["key"] for r in db.settings_since(good)] == ["B", "C"]


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------

def test_secret_round_trips_through_the_box(db, box):
    if not box.available():
        pytest.skip("cryptography not installed")
    stored, encrypted = box.seal("hunter2")
    assert encrypted and stored != "hunter2"
    db.settings_set("DISCORD_TOKEN", stored, encrypted=True)
    # Raw storage is ciphertext...
    assert db.settings_all()["DISCORD_TOKEN"] != "hunter2"
    # ...and only the decryptor gets the plaintext back.
    assert db.settings_all(decrypt=box.decryptor())["DISCORD_TOKEN"] == "hunter2"


def test_key_file_is_created_private(tmp_path):
    box = SecretBox.open_at(str(tmp_path / "gym.sqlite3"))
    if not box.available():
        pytest.skip("cryptography not installed")
    assert box.key_path is not None and box.key_path.exists()


def test_reopening_reuses_the_same_key(tmp_path):
    path = str(tmp_path / "gym.sqlite3")
    first = SecretBox.open_at(path)
    if not first.available():
        pytest.skip("cryptography not installed")
    sealed, _ = first.seal("value")
    assert SecretBox.open_at(path).open_secret(sealed) == "value"


def test_in_memory_database_does_not_write_a_key_file(tmp_path, monkeypatch):
    """Path(':memory:').parent is the working directory -- never write there."""
    monkeypatch.chdir(tmp_path)
    box = SecretBox.open_at(":memory:")
    assert box.key_path is None
    assert not (tmp_path / ".secret_key").exists()


# ---------------------------------------------------------------------------
# Fernet bootstrap -- the ordering here protects every existing deployment
# ---------------------------------------------------------------------------

def test_bootstrap_does_not_generate_when_env_has_a_key(db, box):
    for name in ("REVO_FERNET_KEY", "STRAVA_FERNET_KEY", "HEVY_FERNET_KEY"):
        fresh = Database(db.path + f".{name}")
        try:
            assert bootstrap_fernet(
                fresh, box, {name: "existing-key"},
            ) is None
            assert fresh.settings_all() == {}
        finally:
            fresh.close()


def test_bootstrap_does_not_generate_when_one_is_already_stored(db, box):
    db.settings_set("STRAVA_FERNET_KEY", "already-here")
    assert bootstrap_fernet(db, box, {}) is None


def test_bootstrap_generates_revo_key_so_all_three_clients_resolve(db, box):
    """revo_client has NO fallback chain, strava falls back to revo, and hevy
    falls back to both -- so REVO_FERNET_KEY is the only name one generated key
    can use and still serve all three."""
    if not box.available():
        pytest.skip("cryptography not installed")
    assert bootstrap_fernet(db, box, {}) == "REVO_FERNET_KEY"
    row = {r["key"]: r for r in db.settings_rows()}["REVO_FERNET_KEY"]
    # Sealed, not plaintext: a nightly snapshot must not carry both the
    # ciphertext and the key that opens it.
    assert row["encrypted"] == 1
    assert box.open_secret(row["value"])


# ---------------------------------------------------------------------------
# One-time environment seed
# ---------------------------------------------------------------------------

def test_seed_imports_only_keys_actually_set(db, box):
    settings_service.seed_from_env_once(db, box, {"REMINDER_HOUR": "21"})
    stored = db.settings_all()
    assert stored["REMINDER_HOUR"] == "21"
    # Defaults are deliberately NOT written: an explicit row would freeze them
    # and silently block a future change to the default.
    assert "REMINDER_MINUTE" not in stored


def test_seed_runs_only_once(db, box):
    settings_service.seed_from_env_once(db, box, {"REMINDER_HOUR": "21"})
    db.settings_set("REMINDER_HOUR", "9")
    settings_service.seed_from_env_once(db, box, {"REMINDER_HOUR": "21"})
    assert db.settings_all()["REMINDER_HOUR"] == "9"


def test_seed_skips_env_only_keys(db, box):
    settings_service.seed_from_env_once(db, box, {"WEBUI_PORT": "9999"})
    assert "WEBUI_PORT" not in db.settings_all()


def test_seed_encrypts_secrets(db, box):
    if not box.available():
        pytest.skip("cryptography not installed")
    settings_service.seed_from_env_once(db, box, {"DISCORD_TOKEN": "tok"})
    row = {r["key"]: r for r in db.settings_rows()}["DISCORD_TOKEN"]
    assert row["encrypted"] == 1
    assert row["value"] != "tok"


def test_upgrade_preserves_member_mirroring_for_a_dashboard_user(db, box):
    """The single most dangerous case in the whole migration.

    A deployment that ran the dashboard had intents.members ON. If the new flag
    did not inherit that, mirroring would silently stop.
    """
    settings_service.seed_from_env_once(db, box, {"WEBUI_PASSWORD": "hunter2"})
    assert db.settings_all()["ENABLE_MEMBER_MIRROR"] == "true"


def test_upgrade_leaves_member_mirroring_off_for_everyone_else(db, box):
    """The other half: a deployment that never ran the dashboard had the
    Server Members intent OFF. Turning it on would make Discord refuse the
    gateway and crash-loop the bot on first boot of the new image."""
    settings_service.seed_from_env_once(db, box, {"DISCORD_TOKEN": "tok"})
    assert db.settings_all()["ENABLE_MEMBER_MIRROR"] == "false"


def test_upgrade_respects_webui_disabled(db, box):
    settings_service.seed_from_env_once(
        db, box, {"WEBUI_PASSWORD": "hunter2", "WEBUI_DISABLED": "1"},
    )
    assert db.settings_all()["ENABLE_MEMBER_MIRROR"] == "false"


def test_fresh_install_gets_member_mirroring_off(db, box):
    settings_service.seed_from_env_once(db, box, {})
    assert db.settings_all()["ENABLE_MEMBER_MIRROR"] == "false"


def test_revert_refuses_when_no_known_good_revision_exists(db, box):
    """settings_since(0) selects every retained row, including the bootstrap
    Fernet key written at rev 1. Replaying that would clear the key and make
    every linked Revo/Strava/Hevy credential permanently unreadable."""
    from app.settings_service import SettingsService

    bootstrap_fernet(db, box, {})
    svc = SettingsService(db, box, {})
    svc.set("REMINDER_HOUR", "9", actor="test")

    result = svc.revert_to_last_good(actor="test")
    assert result["ok"] is False
    # And the key is untouched.
    assert (db.settings_all().get("REVO_FERNET_KEY") or "") != ""


def test_revert_never_clears_an_encryption_key(db, box):
    from app.settings_service import SettingsService

    bootstrap_fernet(db, box, {})
    svc = SettingsService(db, box, {})
    svc.mark_good(db.settings_rev())          # baseline AFTER the key exists
    svc.set("REVO_FERNET_KEY", "rotated-by-hand", actor="test")
    svc.set("REMINDER_HOUR", "9", actor="test")

    result = svc.revert_to_last_good(actor="test")
    assert result["ok"] is True
    assert "REVO_FERNET_KEY" not in result["cleared"]
    assert "REVO_FERNET_KEY" not in result["reverted"]
    assert (db.settings_all().get("REVO_FERNET_KEY") or "") != ""


def test_a_full_legacy_environment_resolves_identically(db, box):
    """End-to-end: seed a realistic pre-upgrade environment, then drop the
    environment entirely and confirm the resolved config is unchanged."""
    legacy = {
        "DISCORD_TOKEN": "tok", "GUILD_ID": "974948447335620668",
        "GYM_CHANNEL_IDS": "1494521271000764506",
        "DAILY_UPDATE_CHANNEL_ID": "1494521271000764506",
        "DAILY_UPDATE_HOUR": "8", "DAILY_UPDATE_POST_EMPTY": "true",
        "DISPLAY_TIMEZONE": "Australia/Adelaide",
        "PARSE_REPLY_MAX_ITEMS": "15", "REVO_POLL_MINUTES": "15",
        "ADMIN_USER_IDS": "1072114272064262100",
        "ENABLE_PRESENCE_TRACKING": "true", "COMMAND_SCOPE": "global",
    }
    with_env = config_mod.load(db, legacy)
    settings_service.seed_from_env_once(db, box, legacy)
    without_env = config_mod.load(db, {}, decrypt=box.decryptor())

    for key in legacy:
        assert with_env[key] == without_env[key], (
            f"{key} changed when the environment pin was removed"
        )
    # And the derived values follow along.
    assert without_env["WEEKLY_REPORT_CHANNEL_ID"] == 1494521271000764506
