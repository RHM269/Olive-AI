"""Smoke tests for the Olive AI entry point (app.main orchestration)."""

from __future__ import annotations

from app.logging_config import OLIVE_LOGGER_NAME
import logging

from main import main

_OLIVE_ENV_VARS = ("APP_NAME", "ENVIRONMENT", "LOG_LEVEL", "OLIVE_DATA_MODE")


def test_main_starts_successfully_and_reports_truthful_status(monkeypatch, capsys):
    for var in _OLIVE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    exit_code = main()
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "OLIVE AI" in captured.out
    assert "SYSTEM STATUS: ONLINE" in captured.out
    assert "Prediction engine: NOT IMPLEMENTED" in captured.out


def test_main_fails_closed_on_invalid_configuration(monkeypatch, capsys):
    for var in _OLIVE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ENVIRONMENT", "not-a-real-environment")

    exit_code = main()
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "OLIVE AI failed to start" in captured.err


def test_repeated_main_calls_do_not_duplicate_log_handlers(monkeypatch):
    for var in _OLIVE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    main()
    main()

    logger = logging.getLogger(OLIVE_LOGGER_NAME)
    assert len(logger.handlers) == 1
