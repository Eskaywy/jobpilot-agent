"""Step 1 - Job Discovery & Data Extraction Engine.

Queries sanctioned public job-board APIs (no scraping of hostile sites) and
normalises every hit into a :class:`JobListing` with the metadata required by
the pipeline: company, role title, JD text, application e-mail and whether a
cover letter is required. Providers fail soft - a dead source never aborts
the hourly cycle.

Add new sources by subclassing :class:`JobProvider` and registering the
instance in :class:`DiscoveryEngine`. The MCP Puppeteer server (configured in
``.vscode/mcp.json``) can be used interactively inside VS Code for sources
that require a real browser; this module stays API-based by design.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlencode

import requests

from .config import PROJECT_ROOT, TARGET_ROLES, Settings
from .textutils import (find_emails, rank_emails_for_company, strip_html)

log = logging.getLogger("jobpilot.discovery")

#: Title keyword variants mapped to the canonical target roles. Most specific
#: variants are listed first so "junior data analyst" wins over "data analyst".
ROLE_TITLE_KEYWORDS = {
    "Financial Data Analyst": [
        "financial data analyst", "finance data analyst", "fp&a analyst",
        "fpna analyst", "financial analyst",
    ],
    "Junior Data Analyst": [
        "junior data analyst", "entry level data analyst",
        "entry-level data analyst", "jr. data analyst", "jr data analyst",
        "associate data analyst", "graduate data analyst", "data analyst i",
        "data analyst",
    ],
    "Email Customer Service": [
        "email customer service", "customer service email", "email support",
        "customer care email", "digital customer service",
    ],
    "Data Entry": [
        "data entry clerk", "data entry specialist", "data entry operator",
        "data entry",
    ],
    "Customer Service Representative": [
        "customer service representative", "customer support representative",
        "customer service rep", "customer service associate",
        "customer care representative", "call center representative",
        "call centre representative", "customer service",
    ],
}

#: Junior-targeted roles must not match senior/management titles.
SENIOR_MARKERS = ("senior", "sr.", "sr ", "lead", "principal", "manager",
                  "director", "head of", "chief", "vp ", " ii", " iii")


@dataclass
class JobListing:
    """Normalised job posting consumed by the rest of the pipeline."""

    company: str
    role_title: str            # title exactly as posted
    canonical_role: str        # one of TARGET_ROLES
    job_description: str       # plain text (HTML stripped)
    application_email: str     # may be "" when not discoverable
    cover_letter_required: bool
    source: str
    url: str = ""
    location: str = ""
    posted_at: str = ""

    def dedup_key(self) -> str:
        from .textutils import dedup_key as _key
        return _key(self.company, self.role_title, self.application_email)


def classify_role(title: str) -> Optional[str]:
    """Map a posted job title to one of TARGET_ROLES, or None if unrelated."""
    text = f" {strip_html(title or '').lower()} "
    for role in TARGET_ROLES:  # tuple order == priority order
        for variant in ROLE_TITLE_KEYWORDS[role]:
            if variant in text:
                if role == "Junior Data Analyst" and any(m in text for m in SENIOR_MARKERS):
                    continue
                return role
    return None


_COVER_LETTER_TRIGGERS = (
    "cover letter", "coverletter", "covering letter",
    "motivation letter", "letter of motivation",
)


def _cover_letter_required(jd_text: str) -> bool:
    return any(trigger in (jd_text or "").lower() for trigger in _COVER_LETTER_TRIGGERS)


#: Tracker status for target-role listings that cannot be emailed automatically.
WATCHLIST_STATUS = "Watchlist"


def _watchlist_row(listing: "JobListing") -> Dict[str, object]:
    """Build a tracker row that saves a no-email listing for manual applying.

    Uses the same canonical column order as ``tracker.COLUMNS`` so the row
    lands correctly in tracker.xlsx. Dedup works because the row carries the
    same (company, role, empty email) triple the discovery engine uses.
    """
    return {
        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Company": listing.company,
        "Job Role": listing.role_title,
        "Recipient Email": "",
        "Keyword Match %": "",
        "Cosine Similarity": "",
        "Keyword Coverage": "",
        "Resume Used": "",
        "Cover Letter Generated": "No",
        "Application Status": WATCHLIST_STATUS,
        "Source": listing.source,
        "Job URL": listing.url,
        "Notes": "No application email in JD - apply manually via Job URL",
    }


def _parse_listing(raw_title: str, raw_company: str, raw_jd: str,
                   source: str, url: str = "", location: str = "",
                   posted_at: str = "") -> Optional[JobListing]:
    """Normalise a raw API hit; returns None when the role is not a target."""
    canonical = classify_role(raw_title)
    if canonical is None:
        return None
    jd_text = strip_html(raw_jd or "")
    emails = find_emails(jd_text)
    company = (raw_company or "Unknown Company").strip()
    return JobListing(
        company=company,
        role_title=strip_html(raw_title or canonical).strip(),
        canonical_role=canonical,
        job_description=jd_text,
        application_email=rank_emails_for_company(emails, company) or "",
        cover_letter_required=_cover_letter_required(jd_text),
        source=source,
        url=url,
        location=location,
        posted_at=posted_at,
    )


class JobProvider(ABC):
    """Contract for one job data source."""

    name: str = "abstract"

    @abstractmethod
    def fetch(self) -> List[JobListing]:
        """Return all target-role listings visible at this source."""

    def _get_json(self, url: str) -> Optional[object]:
        """GET *url* and parse JSON; failures return None (fail soft)."""
        try:
            response = requests.get(url, timeout=self.timeout, headers={
                "User-Agent": "JobPilotAgent/1.0 (+personal job search)",
                "Accept": "application/json",
            })
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
            log.warning("[%s] fetch failed for %s: %s", self.name, url, exc)
            return None


class ArbeitnowProvider(JobProvider):
    """https://arbeitnow.com/api - free, keyless, permissive licence."""

    name = "arbeitnow"
    endpoint = "https://www.arbeitnow.com/api/job-board-api"

    def __init__(self, timeout: int = 20, pages: int = 2):
        self.timeout = timeout
        self.pages = pages  # paginated cursor API

    def fetch(self) -> List[JobListing]:
        listings: List[JobListing] = []
        url: Optional[str] = self.endpoint
        for _ in range(self.pages):
            if not url:
                break
            payload = self._get_json(url)
            if not payload:
                break
            for item in payload.get("data", []):
                parsed = _parse_listing(
                    raw_title=item.get("title", ""),
                    raw_company=item.get("company_name", ""),
                    raw_jd=item.get("description", ""),
                    source=self.name,
                    url=item.get("url", ""),
                    location=item.get("location", ""),
                    posted_at=str(item.get("created_at", "")),
                )
                if parsed:
                    listings.append(parsed)
            url = payload.get("meta", {}).get("url") or None
        return listings


class RemotiveProvider(JobProvider):
    """https://remotive.com/api/remote-jobs - free, keyless public API."""

    name = "remotive"
    endpoint = "https://remotive.com/api/remote-jobs"
    search_terms = [
        "data analyst", "financial analyst", "customer service",
        "customer support", "data entry", "email support",
    ]

    def __init__(self, timeout: int = 20):
        self.timeout = timeout

    def fetch(self) -> List[JobListing]:
        listings: List[JobListing] = []
        for term in self.search_terms:
            payload = self._get_json(
                f"{self.endpoint}?search={requests.utils.quote(term)}")
            if not payload:
                continue
            for item in payload.get("jobs", []):
                parsed = _parse_listing(
                    raw_title=item.get("title", ""),
                    raw_company=item.get("company_name", ""),
                    raw_jd=item.get("description", ""),
                    source=self.name,
                    url=item.get("url", ""),
                    location=item.get("candidate_required_location", ""),
                    posted_at=str(item.get("publication_date", "")),
                )
                if parsed:
                    listings.append(parsed)
        return listings


class AdzunaQuotaExceeded(Exception):
    """Raised when Adzuna answers 429/403 - quota or rate limit exhausted."""


class AdzunaProvider(JobProvider):
    """https://developer.adzuna.com - free API, requires app_id + app_key.

    Endpoint shape: ``https://api.adzuna.com/v1/api/jobs/{country}/search/{page}``
    with ``app_id``, ``app_key`` and ``results_per_page`` (hard cap 50) as
    query parameters. Results arrive in ``payload["results"]`` with
    ``title``, ``description``, ``redirect_url``, ``company.display_name``,
    ``location.display_name`` and ``created`` fields.

    Rate-limit strategy (free tier ~250 calls/day): one page per search term
    per cycle (6 calls/cycle = ~144/day on hourly cycles), and a hard stop
    for the rest of the cycle when the API answers 429/403.

    Limitations (by design of the Adzuna service):
    * descriptions are truncated (~500 chars), which limits e-mail discovery
      - listings without an application e-mail are saved to the watchlist
      tracker instead of being applied to automatically;
    * there is no "remote" flag and no direct application e-mail field;
    * Adzuna does not cover every country - set ADZUNA_COUNTRY to one of the
      supported codes (e.g. us, gb, za, in, ca, de, fr, nl, it, es, ...).
    """

    name = "adzuna"
    endpoint = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
    search_terms = RemotiveProvider.search_terms

    def __init__(self, app_id: str, app_key: str, country: str = "us",
                 timeout: int = 20, pages: int = 1, results_per_page: int = 50):
        self.app_id = app_id
        self.app_key = app_key
        self.country = country
        self.timeout = timeout
        self.pages = max(1, min(pages, 2))  # 2 pages/cycle max, quota-friendly
        self.results_per_page = min(results_per_page, 50)  # Adzuna hard cap

    def _get_json(self, url: str) -> Optional[object]:
        """GET *url*; 429/403 raises :class:`AdzunaQuotaExceeded`."""
        try:
            response = requests.get(url, timeout=self.timeout, headers={
                "User-Agent": "JobPilotAgent/1.0 (+personal job search)",
                "Accept": "application/json",
            })
            if response.status_code in (429, 403):
                raise AdzunaQuotaExceeded(
                    f"HTTP {response.status_code} from Adzuna - quota or "
                    "rate limit hit; stopping Adzuna for this cycle")
            response.raise_for_status()
            return response.json()
        except AdzunaQuotaExceeded:
            raise
        except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
            log.warning("[%s] fetch failed for %s: %s", self.name, url, exc)
            return None

    def fetch(self) -> List[JobListing]:
        listings: List[JobListing] = []
        for term in self.search_terms:
            for page in range(1, self.pages + 1):
                query = urlencode({
                    "app_id": self.app_id,
                    "app_key": self.app_key,
                    "what": term,
                    "results_per_page": self.results_per_page,
                    "content-type": "application/json",
                })
                try:
                    payload = self._get_json(
                        f"{self.endpoint.format(country=self.country, page=page)}"
                        f"?{query}")
                except AdzunaQuotaExceeded as exc:
                    log.warning("[adzuna] %s", exc)
                    return listings  # stop spending calls this cycle
                if not payload:
                    break  # next page will not exist either
                for item in payload.get("results", []):
                    parsed = _parse_listing(
                        raw_title=item.get("title", ""),
                        raw_company=(item.get("company") or {}).get(
                            "display_name", ""),
                        raw_jd=item.get("description", ""),
                        source=self.name,
                        url=item.get("redirect_url", ""),
                        location=(item.get("location") or {}).get(
                            "display_name", ""),
                        posted_at=str(item.get("created", "")),
                    )
                    if parsed:
                        listings.append(parsed)
        return listings


class SerpApiProvider(JobProvider):
    """https://serpapi.com - Google Jobs results via the SerpApi hosted API.

    Uses ``engine=google_jobs`` (the jobs variant of SerpApi's
    ``https://serpapi.com/search`` endpoint - the plain ``engine=google``
    engine returns organic web results, not job postings). Requires a
    SERPAPI_API_KEY. Each request costs one search credit; the free plan
    allows 100 searches/month, so this provider spends only one request per
    search term and runs at most once per SERPAPI_MIN_INTERVAL_HOURS
    (default 12, i.e. ~60 searches/month) via a state file.

    Response shape: ``jobs_results[]`` with ``title``, ``company_name``,
    ``location``, ``via``, a FULL (untruncated) ``description`` - which
    makes application-e-mail discovery far more productive than Adzuna -
    ``apply_options[].link`` and ``detected_extensions`` (e.g. posted_at).
    """

    name = "serpapi"
    endpoint = "https://serpapi.com/search.json"
    search_terms = RemotiveProvider.search_terms

    def __init__(self, api_key: str, engine: str = "google_jobs",
                 timeout: int = 20, min_interval_hours: int = 12):
        self.api_key = api_key
        self.engine = engine
        self.timeout = timeout
        self.min_interval_hours = max(1, min_interval_hours)
        # State file lives next to the agent log; survives between cycles.
        self._state_path = (Path(__file__).resolve().parent.parent
                            / "logs" / "serpapi_last_run.txt")

    def _interval_elapsed(self) -> bool:
        """True when enough time has passed since the last SerpApi run.

        The free plan grants 100 searches/month, so hourly cycles would burn
        the quota in a single day. The provider therefore runs at most once
        per ``min_interval_hours`` (default 12 -> ~60 searches/month).
        """
        try:
            last = float(self._state_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return True
        elapsed_hours = (datetime.now().timestamp() - last) / 3600.0
        return elapsed_hours >= self.min_interval_hours

    def _mark_run(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(str(datetime.now().timestamp()),
                                        encoding="utf-8")
        except OSError as exc:
            log.warning("[serpapi] could not write state file: %s", exc)

    def fetch(self) -> List[JobListing]:
        if not self._interval_elapsed():
            log.info("[serpapi] skipped - last run less than %d hour(s) ago "
                     "(quota protection)", self.min_interval_hours)
            return []
        self._mark_run()
        listings: List[JobListing] = []
        for term in self.search_terms:
            query = urlencode({
                "engine": self.engine,
                "q": term,
                "api_key": self.api_key,
            })
            payload = self._get_json(f"{self.endpoint}?{query}")
            if not payload:
                continue
            for item in payload.get("jobs_results", []):
                apply_options = item.get("apply_options") or []
                url = ""
                if apply_options and isinstance(apply_options[0], dict):
                    url = str(apply_options[0].get("link", ""))
                parsed = _parse_listing(
                    raw_title=item.get("title", ""),
                    raw_company=(item.get("company_name")
                                 or item.get("via") or ""),
                    raw_jd=item.get("description", ""),
                    source=self.name,
                    url=url or str(item.get("share_link", "")),
                    location=item.get("location", ""),
                    posted_at=str((item.get("detected_extensions") or {})
                                  .get("posted_at", "")),
                )
                if parsed:
                    listings.append(parsed)
        return listings


class FixtureProvider(JobProvider):
    """Offline listings from fixtures/jobs.json (ENABLE_FIXTURES=true).

    Lets you rehearse the entire pipeline - matching, generation, dispatch,
    tracking - without touching the network or sending anything.
    """

    name = "fixtures"
    fixture_path = PROJECT_ROOT / "fixtures" / "jobs.json"

    def __init__(self, timeout: int = 20):
        self.timeout = timeout

    def fetch(self) -> List[JobListing]:
        try:
            records = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("[%s] cannot read %s: %s", self.name, self.fixture_path, exc)
            return []
        listings: List[JobListing] = []
        for record in records:
            parsed = _parse_listing(
                raw_title=record.get("title", ""),
                raw_company=record.get("company", ""),
                raw_jd=record.get("description", ""),
                source=self.name,
                url=record.get("url", ""),
                location=record.get("location", ""),
                posted_at=record.get("posted_at", ""),
            )
            if parsed:
                listings.append(parsed)
        return listings


class DiscoveryEngine:
    """Step 1 orchestrator - aggregates providers, deduplicates by SHA-256."""

    def __init__(self, settings: Settings, tracker=None):
        self.settings = settings
        self.tracker = tracker
        self._watch_count = 0          # watchlist rows added this cycle
        self.last_watch_count = 0      # survives the cycle for the summary
        self.providers: List[JobProvider] = [
            ArbeitnowProvider(timeout=settings.request_timeout),
            RemotiveProvider(timeout=settings.request_timeout),
        ]
        if settings.adzuna_app_id and settings.adzuna_app_key:
            self.providers.append(AdzunaProvider(
                app_id=settings.adzuna_app_id,
                app_key=settings.adzuna_app_key,
                country=settings.adzuna_country,
                timeout=settings.request_timeout,
            ))
        else:
            log.info("Adzuna provider disabled - set ADZUNA_APP_ID and "
                     "ADZUNA_APP_KEY in .env to enable it.")
        if settings.serpapi_api_key:
            self.providers.append(SerpApiProvider(
                api_key=settings.serpapi_api_key,
                engine=settings.serpapi_engine,
                timeout=settings.request_timeout,
                min_interval_hours=settings.serpapi_min_interval_hours,
            ))
        else:
            log.info("SerpApi provider disabled - set SERPAPI_API_KEY in "
                     ".env to enable it.")
        if settings.enable_fixtures:
            self.providers.append(FixtureProvider(timeout=settings.request_timeout))

    def find_new_listings(self) -> List[JobListing]:
        """Return unseen target-role listings (dedup across every cycle)."""
        seen_keys = self.tracker.known_keys() if self.tracker else set()
        aggregated: List[JobListing] = []
        seen_in_run = set()
        for provider in self.providers:
            try:
                fetched = provider.fetch()
            except Exception as exc:  # a broken provider must not kill the cycle
                log.warning("Provider %s crashed: %s", provider.name, exc)
                continue
            log.info("Provider %s returned %d target listings",
                     provider.name, len(fetched))
            for listing in fetched:
                key = listing.dedup_key()
                if key in seen_keys or key in seen_in_run:
                    continue
                if not listing.application_email:
                    # No e-mail -> cannot dispatch automatically. Save it to
                    # the tracker as a watchlist entry (deduped forever) so
                    # nothing valuable is silently dropped.
                    if self.tracker is not None:
                        self.tracker.append(_watchlist_row(listing))
                        log.info("Watchlist: '%s' @ %s [%s] - no application "
                                 "email; saved with apply URL (%d this cycle)",
                                 listing.role_title, listing.company,
                                 listing.source, self._watch_count + 1)
                        self._watch_count += 1
                    else:
                        log.info("Skipping '%s' @ %s - no application email "
                                 "found (no tracker to watchlist it)",
                                 listing.role_title, listing.company)
                    seen_in_run.add(key)
                    continue
                seen_in_run.add(key)
                aggregated.append(listing)
        self.last_watch_count = self._watch_count
        log.info("Discovery complete: %d new actionable listings, "
                 "%d added to watchlist", len(aggregated), self._watch_count)
        return aggregated
