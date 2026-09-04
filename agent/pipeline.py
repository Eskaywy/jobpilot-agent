"""Pipeline orchestrator - wires together Steps 1-6 for one hourly cycle.

Per cycle:
  1. DiscoveryEngine  -> new, unseen, actionable listings
  2. ResumeMatcher    -> best existing resume + ATS match score
  3. (< 90%)          -> ATSResumeGenerator builds a tailored 1-page PDF
  4. (if required)    -> CoverLetterGenerator builds the 3-paragraph PDF
  5. EmailDispatcher  -> sends (or dry-run renders) the application
  6. Tracker          -> appends the full audit row to tracker.xlsx

Every listing is isolated in its own try/except so one bad posting never
aborts the hourly cycle.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, Optional

from .config import Settings
from .cover_letter import CoverLetterGenerator
from .discovery import (DiscoveryEngine, JSearchProvider, JobListing,
                        job_listing_from_details)
from .dispatcher import EmailDispatcher
from .matcher import MatchResult, ResumeMatcher
from .resume_generator import ATSResumeGenerator
from .tracker import Tracker
from .textutils import dedup_key

log = logging.getLogger("jobpilot.pipeline")


def build_tracker_row(listing: JobListing, match: MatchResult,
                      resume_path, cover_letter_path,
                      status: str, notes: str = "") -> Dict[str, object]:
    """Assemble one tracker row in canonical COLUMNS order."""
    def filename(path) -> str:
        return path.name if path is not None else ""
    return {
        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Company": listing.company,
        "Job Role": listing.role_title,
        "Recipient Email": listing.application_email,
        "Keyword Match %": round(match.ats_match_score * 100, 1),
        "Cosine Similarity": round(match.cosine_raw, 4),
        "Keyword Coverage": round(match.keyword_coverage * 100, 1),
        "Resume Used": filename(resume_path),
        "Cover Letter Generated": ("Yes" if cover_letter_path is not None else "No"),
        "Application Status": status,
        "Source": listing.source,
        "Job URL": listing.url,
        "Notes": notes,
    }


def process_listing(listing: JobListing, settings: Settings,
                    matcher: ResumeMatcher, generator: ATSResumeGenerator,
                    letters: CoverLetterGenerator, dispatcher: EmailDispatcher,
                    tracker: Tracker) -> bool:
    """Run Steps 2-6 for a single listing; True when an application happened."""
    log.info("Processing: %s @ %s [%s] (cover letter: %s)",
             listing.role_title, listing.company, listing.source,
             "required" if listing.cover_letter_required else "not required")

    # Step 2 - match existing resumes against the JD.
    match = matcher.find_best_match(listing.job_description)

    # Minimum-score gate: never spend a generated resume or an outbound
    # e-mail on a weak match. 0 disables the gate entirely.
    min_score = settings.min_apply_score
    if min_score > 0 and match.ats_match_score < min_score:
        notes = (f"ATS {match.ats_match_score * 100:.1f}% below the "
                 f"{min_score * 100:.0f}% apply minimum; e-mail available: "
                 f"{listing.application_email}")
        if match.missing_keywords:
            notes += (". Gaps: "
                      + ", ".join(match.missing_keywords[:5]))
        try:
            tracker.append(build_tracker_row(listing, match, None, None,
                                             "Skipped (Low Match)", notes))
        except Exception as exc:
            # A locked/broken workbook must not turn a routine low-match
            # skip into a spurious failure - fail soft, log, and move on.
            log.warning("Could not record low-match skip for %s @ %s: %s",
                        listing.role_title, listing.company, exc)
        log.info("Skipped '%s' @ %s - ATS %.1f%% below %.0f%% apply "
                 "minimum", listing.role_title, listing.company,
                 match.ats_match_score * 100, min_score * 100)
        return False

    resume_path = match.resume_path
    if match.resume_path is not None and match.meets_threshold:
        # >= 90%: reuse the highest-scoring existing resume.
        log.info("Match >= %.0f%% (%.1f%%) - reusing %s",
                 matcher.threshold * 100, match.ats_match_score * 100,
                 match.resume_path.name)
    else:
        # < 90%: generate a fresh tailored ATS resume (Step 3).
        resume_path = generator.generate(listing.canonical_role,
                                         listing.company,
                                         listing.job_description)

    # Step 4 - cover letter when the JD demands one.
    cover_letter_path = None
    if listing.cover_letter_required:
        cover_letter_path = letters.save_pdf(listing.canonical_role,
                                             listing.company,
                                             listing.job_description)

    # Step 5 - dispatch the application e-mail.
    status = dispatcher.send_application(listing, resume_path, cover_letter_path)

    # Step 6 - record everything in the Excel ledger.
    notes = ("Gaps: " + ", ".join(match.missing_keywords[:5])
             if match.missing_keywords else "")
    tracker.append(build_tracker_row(listing, match, resume_path,
                                     cover_letter_path, status, notes))
    return True


def run_pipeline(settings: Optional[Settings] = None) -> Dict[str, int]:
    """Execute one complete hourly cycle; returns a summary counter dict."""
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    started = datetime.now()
    log.info("=" * 70)
    log.info("PIPELINE CYCLE START - %s", started.strftime("%Y-%m-%d %H:%M:%S"))
    for warning in settings.validate():
        log.warning("Config: %s", warning)

    tracker = Tracker(settings.tracker_path)
    tracker.ensure()                            # Step 6 scaffolding (dedup)
    matcher = ResumeMatcher(settings)           # Step 2 engine
    generator = ATSResumeGenerator(settings)    # Step 3 engine
    letters = CoverLetterGenerator(settings)    # Step 4 engine
    dispatcher = EmailDispatcher(settings)      # Step 5 engine
    discovery = DiscoveryEngine(settings, tracker)  # Step 1 engine

    summary = {"discovered": 0, "applied": 0, "failed": 0, "skipped": 0}
    try:
        listings = discovery.find_new_listings()            # Step 1
    except Exception as exc:
        log.error("Discovery crashed this cycle: %s", exc)
        listings = []
    summary["discovered"] = len(listings)

    quota = settings.max_applications_per_cycle
    for listing in listings:
        if summary["applied"] >= quota:
            log.info("Per-cycle application cap (%d) reached - deferring %d "
                     "listings to the next cycle",
                     quota, len(listings) - summary["applied"])
            break
        process_listing_safe(listing, settings, matcher, generator, letters,
                             dispatcher, tracker, summary)

    elapsed = (datetime.now() - started).total_seconds()
    log.info("PIPELINE CYCLE END - %s | discovered=%d applied=%d failed=%d "
             "skipped=%d | %.1fs", datetime.now().strftime("%H:%M:%S"),
             summary["discovered"], summary["applied"], summary["failed"],
             summary["skipped"], elapsed)
    return summary


def process_listing_safe(listing: JobListing, settings: Settings,
                         matcher: ResumeMatcher,
                         generator: ATSResumeGenerator,
                         letters: CoverLetterGenerator,
                         dispatcher: EmailDispatcher, tracker: Tracker,
                         summary: Dict[str, int]) -> None:
    """Wrapped :func:`process_listing` that keeps the summary consistent."""
    try:
        ok = process_listing(listing, settings, matcher, generator, letters,
                             dispatcher, tracker)
        summary["applied" if ok else "skipped"] += 1
    except Exception as exc:
        summary["failed"] += 1
        log.exception("Failed processing %s @ %s: %s",
                      listing.role_title, listing.company, exc)
        try:  # record the failure so dedup still skips it next cycle
            tracker.append(build_tracker_row(
                listing, MatchResult(resume_path=None), None, None,
                "Failed", notes=str(exc)[:200]))
        except Exception:
            log.exception("Could not record failure row in tracker")


def run_single_job(settings: Settings, job_id: str) -> Optional[str]:
    """Fetch one specific JSearch ``job_id`` and run Steps 2-6 on it.

    Bridges the ``job-details`` endpoint into the normal pipeline: the
    enriched record is normalised into a :class:`JobListing` (via
    ``job_listing_from_details``) and processed exactly like a discovered
    listing - match, resume generation, dispatch, tracker. This is the
    engine behind ``python main.py --job-id <id>``.

    Returns the outcome string ("processed"/"skipped"/"duplicate"/"no-email"/
    "failed") or None when the job cannot be fetched or is not a target role.
    """
    settings.ensure_dirs()
    if not settings.jsearch_api_key:
        log.error("JSEARCH_API_KEY is not set in .env - cannot fetch job %s",
                  job_id)
        return None

    tracker = Tracker(settings.tracker_path)
    tracker.ensure()
    matcher = ResumeMatcher(settings)
    generator = ATSResumeGenerator(settings)
    letters = CoverLetterGenerator(settings)
    dispatcher = EmailDispatcher(settings)

    provider = JSearchProvider(api_key=settings.jsearch_api_key,
                               country=settings.jsearch_country,
                               timeout=settings.request_timeout)
    details = provider.get_job_details(job_id)
    listing = job_listing_from_details(details) if details else None
    if listing is None:
        log.error("JSearch job-details returned no usable record for job_id "
                  "%s (or the title is not a target role)", job_id)
        return None

    if listing.dedup_key() in tracker.known_keys():
        log.info("Job %s already recorded in the tracker - skipping (dedup).",
                 job_id)
        return "duplicate"

    if not listing.application_email:
        log.info("Job %s has no application e-mail in its details - nothing "
                 "to dispatch (apply manually via: %s)",
                 job_id, listing.url or "the provider site")
        return "no-email"

    log.info("Processing single job: %s @ %s [%s] (cover letter: %s)",
             listing.role_title, listing.company, listing.source,
             "required" if listing.cover_letter_required else "not required")
    try:
        ok = process_listing(listing, settings, matcher, generator, letters,
                             dispatcher, tracker)
    except Exception as exc:
        log.exception("Failed processing single job %s: %s", job_id, exc)
        return "failed"
    return "processed" if ok else "skipped"