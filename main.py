"""Olive AI application entry point.

Running ``python main.py`` loads configuration, configures logging,
builds a truthful system-health snapshot, and prints a status report,
then exits. This Phase 1 entry point performs no market-data calls, no
network I/O, no background workers, and no infinite loop.

Orchestration only lives here -- trading/business logic belongs in the
``app`` package's domain modules as later phases add them.
"""

from __future__ import annotations

import sys

from app.config import ConfigurationError, load_settings
from app.health import format_health_report, get_system_health
from app.logging_config import configure_logging


def main() -> int:
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        print(f"OLIVE AI failed to start: {exc}", file=sys.stderr)
        return 1

    logger = configure_logging(settings.log_level)
    logger.info(
        "Olive AI starting (environment=%s, data_mode=%s)",
        settings.environment.value,
        settings.data_mode.value,
    )

    health = get_system_health(settings)
    print(format_health_report(health))

    logger.info("Olive AI startup complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
