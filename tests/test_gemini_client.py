"""Tests for app.gemini_client config helpers (no network)."""

from __future__ import annotations

import pytest

from app import gemini_client


def test_model_name_defaults(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert gemini_client.model_name() == gemini_client.DEFAULT_MODEL


def test_model_name_from_env(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-pro")
    assert gemini_client.model_name() == "gemini-2.5-pro"


def test_model_name_blank_falls_back(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "   ")
    assert gemini_client.model_name() == gemini_client.DEFAULT_MODEL


def test_api_key_unset(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert gemini_client.api_key() is None


def test_api_key_strips(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "  AIzTEST  ")
    assert gemini_client.api_key() == "AIzTEST"


def test_available_requires_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert gemini_client.available() is False


def test_generate_without_key_raises(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # Only meaningful when requests is installed; otherwise the dep guard fires
    # first, which is still a GeminiError.
    with pytest.raises(gemini_client.GeminiError):
        gemini_client.generate("hi")
