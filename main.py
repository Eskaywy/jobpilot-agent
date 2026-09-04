#!/usr/bin/env python3
"""JobPilot Agent - main entry point.

Runs the full application pipeline (Steps 1-6) on an hourly schedule inside
the VS Code terminal (or as a background task via .vscode/tasks.json).

Usage:
    python main.py          # run one cycle immediately, then loop hourly
    python main.py --once   # single cycle, then exit (useful for testing)
    python main.py --interval 30   # override the cycle interval (minutes)
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from typing import Optional

import schedule

from agent.config import Settings
from agent.logger import get_logger
from agent.pipeline import run_pipeline, run_single_job

log = get_logger("jobpilot.main")

_shutdown_requested = False


def _handle_sigint(signum, frame):  # noqa: ANN001 - signal handler signature
    """Graceful shutdown flag (finishes the current cycle, then exits)."""
    global _shutdown_requested
    _shutdown_requested = True
    log.info("Shutdown signal received - stopping after the current cycle...")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="JobPilot application agent")
    parser.add_argument("--once", action="store_true",
                        help="run a single pipeline cycle and exit")
    parser.add_argument("--job-id", type=str, default=None,
                        help="fetch one specific jsearch job_id (RapidAPI) "
                             "and run Steps 2-6 on it, then exit")
    parser.add_argument("--interval", type=int, default=None,
                        help="cycle interval override in minutes (default: "
                             "CYCLE_INTERVAL_MINUTES from .env, spec = 60)")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    settings.ensure_dirs()
    interval = args.interval or settings.cycle_interval_minutes

    if args.job_id:
        run_single_job(settings, args.job_id)
        return 0

    if args.once:
        run_pipeline(settings)
        return 0

    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        signal.signal(signal.SIGTERM, _handle_sigint)
    except (AttributeError, ValueError):
        pass  # not available on every platform

    schedule.every(interval).minutes.do(run_pipeline, settings)

    log.info("JobPilot agent starting - immediate cycle, then every %d "
             "minutes. Press Ctrl+C to stop.", interval)
    run_pipeline(settings)  # immediate first cycle

    while not _shutdown_requested:
        try:
            schedule.run_pending()
        except Exception:
            log.exception("Scheduler tick failed - continuing")
        time.sleep(1)

    log.info("JobPilot agent stopped cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
