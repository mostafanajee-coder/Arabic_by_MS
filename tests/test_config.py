"""Tests for config loading helpers."""

from __future__ import annotations

import os

from backend import config


def test_load_environment_reads_dotenv_file(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GEMINI_API_KEY=dotenv-test-key\nGEMINI_MODEL=gemini-dotenv\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)

    loaded = config.load_environment(env_file)

    assert loaded is True
    assert os.getenv("GEMINI_API_KEY") == "dotenv-test-key"
    assert os.getenv("GEMINI_MODEL") == "gemini-dotenv"


def test_load_environment_does_not_override_existing_env(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=dotenv-value\n", encoding="utf-8")

    monkeypatch.setenv("GEMINI_API_KEY", "existing-value")

    config.load_environment(env_file)

    assert os.getenv("GEMINI_API_KEY") == "existing-value"
