"""Shared pytest fixtures.

The repository had no conftest until settings moved into the database. It needs
one now for a specific reason: ``app/config.py`` resolves every setting from the
process environment first, so a developer (or a CI runner) with ``GYM_*``,
``STRAVA_*`` or ``DISCORD_TOKEN`` exported would silently change what the config
tests assert. Local and CI would then disagree, and the equivalence guard in
tests/test_config.py -- the thing standing between a typo and a changed
production deployment -- would be testing the machine rather than the code.

So every test runs against a scrubbed environment by default.
"""
from __future__ import annotations

import os

import pytest

from app import config as config_mod

#: Tests that legitimately need app.bot importable set these before importing
#: it (see tests/test_bot_helpers.py). Keep them, scrub everything else.
_KEEP = {"DB_PATH", "DISCORD_TOKEN"}


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch):
    """Remove every recognised setting from os.environ for the duration.

    Autouse so no test has to remember. Tests that want a value present set it
    explicitly with monkeypatch.setenv, and the ones that pass an ``env`` dict
    straight to config.load are unaffected either way.
    """
    for key in config_mod.SPEC:
        if key not in _KEEP and key in os.environ:
            monkeypatch.delenv(key, raising=False)
    yield
