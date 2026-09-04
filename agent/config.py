"""Central configuration for the JobPilot agent.

All tunables are sourced from environment variables (a ``.env`` file at the
project root is loaded automatically) so credentials and behaviour can be
changed without touching code. See ``.env.example`` for the full list.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from dotenv import load_dotenv

#: Project root = parent of the ``agent`` package directory.
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

#: The five roles this agent hunts for (spec requirement).
TARGET_ROLES: Tuple[str, ...] = (
    "Financial Data Analyst",
    "Junior Data Analyst",
    "Email Customer Service",
    "Data Entry",
    "Customer Service Representative",
)


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, default))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, default))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _env_bool_flag(key: str, default: bool = False) -> bool:
    return _env_bool(key, default)


@dataclass(frozen=True)
class Settings:
    """Immutable runtime configuration resolved from the environment."""

    # --- identity -------------------------------------------------------
    sender_name: str = "Your Name"
    sender_email: str = ""
    sender_phone: str = ""
    sender_location: str = ""
    # --- SMTP -----------------------------------------------------------
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_use_ssl: bool = True
    smtp_user: str = ""
    smtp_password: str = ""
    # --- behaviour ------------------------------------------------------
    dry_run: bool = True
    match_threshold: float = 0.90
    max_applications_per_cycle: int = 5
    cycle_interval_minutes: int = 60
    request_timeout: int = 20
    enable_fixtures: bool = False
    # --- Adzuna API -------------------------------------------------------
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    adzuna_country: str = "us"
    # --- SerpApi (Google Jobs) --------------------------------------------
    serpapi_api_key: str = ""
    serpapi_engine: str = "google_jobs"
    # --- paths ----------------------------------------------------------
    project_root: Path = PROJECT_ROOT
    resumes_dir: Path = PROJECT_ROOT / "resumes"
    cover_letters_dir: Path = PROJECT_ROOT / "cover_letters"
    outbox_dir: Path = PROJECT_ROOT / "outbox"
    logs_dir: Path = PROJECT_ROOT / "logs"
    tracker_path: Path = PROJECT_ROOT / "tracker.xlsx"
    profile_path: Path = PROJECT_ROOT / "profile.yaml"

    @classmethod
    def from_env(cls) -> "Settings":
        """Build a Settings instance from process env / .env file."""
        return cls(
            sender_name=os.getenv("SENDER_NAME", "Your Name"),
            sender_email=os.getenv("SENDER_EMAIL", os.getenv("SMTP_USER", "")),
            sender_phone=os.getenv("SENDER_PHONE", ""),
            sender_location=os.getenv("SENDER_LOCATION", ""),
            smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com"),
            smtp_port=_env_int("SMTP_PORT", 465),
            smtp_use_ssl=_env_bool("SMTP_USE_SSL", True),
            smtp_user=os.getenv("SMTP_USER", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            dry_run=_env_bool("DRY_RUN", True),
            match_threshold=_env_float("MATCH_THRESHOLD", 0.90),
            max_applications_per_cycle=_env_int("MAX_APPLICATIONS_PER_CYCLE", 5),
            cycle_interval_minutes=_env_int("CYCLE_INTERVAL_MINUTES", 60),
            request_timeout=_env_int("REQUEST_TIMEOUT", 20),
            enable_fixtures=_env_bool_flag("ENABLE_FIXTURES", False),
            adzuna_app_id=os.getenv("ADZUNA_APP_ID", "").strip(),
            adzuna_app_key=os.getenv("ADZUNA_APP_KEY", "").strip(),
            adzuna_country=(os.getenv("ADZUNA_COUNTRY", "us").strip().lower()
                            or "us"),
            serpapi_api_key=os.getenv("SERPAPI_API_KEY", "").strip(),
            serpapi_engine=(os.getenv("SERPAPI_ENGINE", "google_jobs").strip()
                            or "google_jobs"),
        )

    def validate(self) -> List[str]:
        """Return human-readable warnings (never raises)."""
        warnings: List[str] = []
        if not self.dry_run and not (self.smtp_user and self.smtp_password):
            warnings.append(
                "DRY_RUN is false but SMTP credentials are missing; the "
                "dispatcher will fall back to dry-run mode for this session."
            )
        if not (0.0 < self.match_threshold <= 1.0):
            warnings.append(
                f"MATCH_THRESHOLD={self.match_threshold} outside (0, 1]; "
                "clamping to 0.90."
            )
        if not self.profile_path.exists():
            warnings.append(
                f"profile.yaml not found at {self.profile_path}; resume/cover "
                "letter generation will fail until you create it."
            )
        if not any(self.resumes_dir.glob("*.pdf")):
            warnings.append(
                "No PDF resumes found in ./resumes/; every application will "
                "trigger the ATS generator until you add base resumes."
            )
        if self.dry_run:
            warnings.append("DRY_RUN=true: emails are rendered to ./outbox/ and NOT sent.")
        return warnings

    def ensure_dirs(self) -> None:
        """Create all runtime output directories if missing."""
        for path in (self.resumes_dir, self.cover_letters_dir,
                     self.outbox_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)
