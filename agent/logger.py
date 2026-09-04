"""Logging setup: rotating file log (logs/agent.log) + console echo.

All agent modules obtain their logger via ``get_logger("<child>")``; child
loggers propagate to the configured "jobpilot" root handler set.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False
_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def get_logger(name: str = "jobpilot") -> logging.Logger:
    """Return a configured logger (configuration happens exactly once).

    Handlers are attached to the ``jobpilot`` base logger so every child
    logger (``jobpilot.pipeline``, ``jobpilot.discovery``, ...) inherits
    them through normal propagation.
    """
    global _CONFIGURED
    base = logging.getLogger("jobpilot")
    if not _CONFIGURED:
        logs_dir = Path(__file__).resolve().parent.parent / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                logs_dir / "agent.log",
                maxBytes=2_000_000, backupCount=5, encoding="utf-8",
            )
            file_handler.setFormatter(logging.Formatter(_FORMAT))
            file_handler.setLevel(logging.DEBUG)
            base.addHandler(file_handler)
        except OSError:  # pragma: no cover - read-only filesystem etc.
            pass
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(_FORMAT, datefmt="%H:%M:%S"))
        console.setLevel(logging.INFO)
        base.addHandler(console)
        base.setLevel(logging.DEBUG)
        base.propagate = False
        _CONFIGURED = True
    return logging.getLogger(name)
