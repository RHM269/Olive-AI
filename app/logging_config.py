"""Central logging setup for Olive AI.

Provides a single, reusable entry point for configuring Olive's
logging so every future module (and ``main.py``) logs consistently,
without ever attaching duplicate handlers if setup runs more than
once.
"""

from __future__ import annotations

import logging

OLIVE_LOGGER_NAME = "olive"
_CONFIGURED_ATTR = "_olive_logging_configured"

_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(log_level: str = "INFO") -> logging.Logger:
    """Configure and return Olive's root application logger.

    Safe to call multiple times (e.g. from tests or repeated
    ``main()`` invocations within one process): subsequent calls only
    update the log level rather than attaching another handler.
    """
    logger = logging.getLogger(OLIVE_LOGGER_NAME)

    if getattr(logger, _CONFIGURED_ATTR, False):
        logger.setLevel(log_level)
        return logger

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT))

    logger.addHandler(handler)
    logger.setLevel(log_level)
    logger.propagate = False
    setattr(logger, _CONFIGURED_ATTR, True)

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger under Olive's logging namespace.

    Future modules should call this (after ``configure_logging`` has
    run at least once) rather than calling ``logging.getLogger``
    directly, keeping all Olive log output under one namespace.
    """
    if not name:
        return logging.getLogger(OLIVE_LOGGER_NAME)
    return logging.getLogger(f"{OLIVE_LOGGER_NAME}.{name}")
