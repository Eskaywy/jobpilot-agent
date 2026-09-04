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


def _append_parsed(listings: List[JobListing], *, title: str, company: str,
                   jd: str, source: str, url: str = "", location: str = "",
                   posted_at: str = "") -> None:
    """Parse one raw API hit and append it to *listings* when it is a target role.

    Providers always normalise through this one path instead of copy-pasting
    the ``_parse_listing`` plus append boilerplate; hits that are not a
    target role are simply skipped (fail soft).
    """
    parsed = _parse_listing(
        raw_title=title, raw_company=company, raw_jd=jd,
        source=source, url=url, location=location, posted_at=posted_at,
    )
    if parsed:
        listings.append(parsed)


def _first_apply_link(item: Dict[str, object]) -> str:
    """Return the first usable ``link`` from a SerpApi ``apply_options`` list.

    A posting can expose several apply channels and the first entry is not
    guaranteed to carry a URL (it may be an "on-site apply" stub), so the
    whole list is scanned instead of assuming index 0. Tolerates
    ``apply_options`` being missing, ``null`` or a non-list, mirroring the
    module's fail-soft philosophy.
    """
    apply_options = item.get("apply_options")
    if not isinstance(apply_options, list):
        return ""
    for option in apply_options:
        if not isinstance(option, dict):
            continue
        link = option.get("link")
        if link:
            return str(link)
    return ""


def _jsearch_apply_link(item: Dict[str, object]) -> str:
    """First usable apply URL from a jsearch record (fail soft).

    jsearch advertises applying through ``job_apply_link`` and a list of
    ``apply_options[].apply_link`` channels (note the key is ``apply_link``,
    unlike SerpApi's ``link``, hence the separate scanner). Tolerates missing
    or malformed values.
    """
    direct = item.get("job_apply_link")
    if direct:
        return str(direct)
    for option in item.get("apply_options") or []:
        if isinstance(option, dict) and option.get("apply_link"):
            return str(option["apply_link"])
    return ""


def job_listing_from_details(item: Dict[str, object],
                             source: str = "jsearch") -> Optional[JobListing]:
    """Map a JSearch ``search`` / ``job-details`` record onto a JobListing.

    Shared by :meth:`JSearchProvider.fetch` and
    :meth:`JSearchProvider.get_job_details` so the search feed and the
    ``--job-id`` path normalise identically (same role gate, e-mail ranking,
    cover-letter detection). Returns None (skipped) when the title is not a
    target role.
    """
    return _parse_listing(
        raw_title=item.get("job_title", ""),
        raw_company=item.get("employer_name", ""),
        raw_jd=item.get("job_description", ""),
        source=source,
        url=_jsearch_apply_link(item),
        location=item.get("job_location", ""),
        posted_at=str(item.get("job_posted_at_datetime_utc")
                      or item.get("job_posted_at_timestamp") or ""),
    )


class JobProvider(ABC):
    """Contract for one job data source."""

    name: str = "abstract"

    @abstractmethod
    def fetch(self) -> List[JobListing]:
        """Return all target-role listings visible at this source."""

    def _get_json(self, url: str,
                  headers: Optional[Dict[str, str]] = None) -> Optional[object]:
        """GET *url* and parse JSON; failures return None (fail soft).

        Extra *headers* (API keys, hosts) are merged over the default
        User-Agent/Accept pair. Subclasses that must abort the cycle on a
        specific HTTP status can override :meth:`_check_response` to raise a
        custom exception (see :class:`AdzunaProvider`, which stops on 429/403).
        """
        try:
            request_headers = {
                "User-Agent": "JobPilotAgent/1.0 (+personal job search)",
                "Accept": "application/json",
            }
            if headers:
                request_headers.update(headers)
            response = requests.get(url, timeout=self.timeout,
                                    headers=request_headers)
            self._check_response(response)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
            log.warning("[%s] fetch failed for %s: %s", self.name, url, exc)
            return None

    def _check_response(self, response: requests.Response) -> None:
        """Hook for providers treating specific HTTP statuses as fatal.

        The default accepts every response; a subclass raises its own
        exception here. It runs before ``raise_for_status`` so fatal non-2xx
        statuses (e.g. Adzuna 429/403 quota) take precedence.
        """


class ArbeitnowProvider(JobProvider):
    """https://www.arbeitnow.com/api/job-board-api - free, keyless, permissive.

    Mirrors ``curl --location 'https://www.arbeitnow.com/api/job-board-api'``
    (requests follows redirects by default, the same behaviour as curl -L).

    API facts (verified against the live endpoint):
    * serves ~175 freshly-posted jobs per page; ``?search=`` is IGNORED
      (always returns the newest global feed, mostly German tech roles);
    * paginates via ``links.next`` (there is no ``meta.url``);
    * descriptions are long HTML but almost never contain an application
      e-mail (applications go through their site), so most hits land in the
      watchlist unless the JD text really includes an e-mail.

    Because the global feed is dominated by roles outside the 5 targets, the
    provider walks up to ``pages`` pages per cycle so target-role titles get
    a chance to surface, capped to keep API usage polite.
    """

    name = "arbeitnow"
    endpoint = "https://www.arbeitnow.com/api/job-board-api"

    def __init__(self, timeout: int = 20, pages: int = 4):
        self.timeout = timeout
        self.pages = max(1, min(pages, 8))  # ~175 jobs/page

    def fetch(self) -> List[JobListing]:
        listings: List[JobListing] = []
        url: Optional[str] = self.endpoint
        for _ in range(self.pages):
            if not url:
                break
            payload = self._get_json(url)
            if not payload:
                break
            # "or []" also copes with a null "data" key from a partial source.
            for item in payload.get("data") or []:
                _append_parsed(
                    listings,
                    title=item.get("title", ""),
                    company=item.get("company_name", ""),
                    jd=item.get("description", ""),
                    source=self.name,
                    url=item.get("url", ""),
                    location=item.get("location", ""),
                    posted_at=str(item.get("created_at", "")),
                )
            url = (payload.get("links") or {}).get("next") or None
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
            # "or []" also copes with a null "jobs" key from a partial source.
            for item in payload.get("jobs") or []:
                _append_parsed(
                    listings,
                    title=item.get("title", ""),
                    company=item.get("company_name", ""),
                    jd=item.get("description", ""),
                    source=self.name,
                    url=item.get("url", ""),
                    location=item.get("candidate_required_location", ""),
                    posted_at=str(item.get("publication_date", "")),
                )
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

    def _check_response(self, response: requests.Response) -> None:
        """Quota hook - 429/403 raises :class:`AdzunaQuotaExceeded`.

        The base :meth:`JobProvider._get_json` calls this before
        ``raise_for_status``; the exception unwinds to :meth:`fetch`, which
        stops Adzuna for the rest of the cycle to save quota.
        """
        if response.status_code in (429, 403):
            raise AdzunaQuotaExceeded(
                f"HTTP {response.status_code} from Adzuna - quota or "
                "rate limit hit; stopping Adzuna for this cycle")

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
                # "or []" also copes with a null "results" key from Adzuna.
                for item in payload.get("results") or []:
                    _append_parsed(
                        listings,
                        title=item.get("title", ""),
                        company=(item.get("company") or {}).get(
                            "display_name", ""),
                        jd=item.get("description", ""),
                        source=self.name,
                        url=item.get("redirect_url", ""),
                        location=(item.get("location") or {}).get(
                            "display_name", ""),
                        posted_at=str(item.get("created", "")),
                    )
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
            if not isinstance(payload, dict):
                continue
            # "or []" also covers a null "jobs_results" key; without it a null
            # response would raise TypeError and abort this entire search term.
            for item in payload.get("jobs_results") or []:
                if not isinstance(item, dict):
                    continue
                _append_parsed(
                    listings,
                    title=item.get("title", ""),
                    company=item.get("company_name") or item.get("via") or "",
                    jd=item.get("description", ""),
                    source=self.name,
                    url=_first_apply_link(item) or str(item.get("share_link", "")),
                    location=item.get("location", ""),
                    posted_at=str((item.get("detected_extensions") or {})
                                  .get("posted_at", "")),
                )
        return listings


class JSearchQuotaExceeded(Exception):
    """Raised when RapidAPI answers 403/429 - quota or rate limit exhausted."""


class JSearchProvider(JobProvider):
    """https://rapidapi.com/jsearchapi-jsearchapi/api/jsearch - jobs API.

    RapidAPI job-search keyed by ``JSEARCH_API_KEY``. Exposes the ``search``
    endpoint (full, untruncated descriptions -> good application-e-mail
    discovery) to the normal discovery cycle, and ``job-details`` so a single
    known ``job_id`` (e.g. pasted from a LinkedIn posting) can be fed through
    the pipeline via ``python main.py --job-id <id>``.

    Response shape (``payload["data"][]``): ``job_title``, ``employer_name``,
    ``job_description``, ``job_apply_link``, ``apply_options[].apply_link``,
    ``job_location`` and ``job_posted_at_datetime_utc``.

    Quota protection: the free RapidAPI tier is limited (commonly ~50
    requests/day), so each search term costs exactly one request per cycle
    and a 403/429 answer stops the provider for the rest of the cycle (same
    pattern as :class:`AdzunaProvider`).
    """

    name = "jsearch"
    host = "jsearch.p.rapidapi.com"
    endpoint = "https://jsearch.p.rapidapi.com/search"
    details_endpoint = "https://jsearch.p.rapidapi.com/job-details"
    search_terms = RemotiveProvider.search_terms

    def __init__(self, api_key: str, country: str = "us", timeout: int = 20,
                 pages: int = 1):
        self.api_key = api_key
        self.country = country
        self.timeout = timeout
        self.pages = max(1, min(pages, 2))

    # ------------------------------------------------------------------ api
    def _rapidapi_headers(self) -> Dict[str, str]:
        """Headers required by the RapidAPI gateway for every jsearch call."""
        return {
            "x-rapidapi-key": self.api_key,
            "x-rapidapi-host": self.host,
        }

    def _check_response(self, response: requests.Response) -> None:
        """Quota hook - 403/429 raises :class:`JSearchQuotaExceeded`."""
        if response.status_code in (403, 429):
            raise JSearchQuotaExceeded(
                f"HTTP {response.status_code} from jsearch (RapidAPI) - "
                "quota or rate limit hit; stopping jsearch for this cycle")

    def get_job_details(self, job_id: str) -> Optional[Dict[str, object]]:
        """Fetch the enriched record for a single ``job_id`` (job-details).

        Mirrors the ``job-details`` endpoint; returns the first record from
        ``payload["data"]`` or None when unavailable (fail soft).
        """
        query = urlencode({"job_id": job_id, "country": self.country})
        payload = self._get_json(f"{self.details_endpoint}?{query}",
                                 headers=self._rapidapi_headers())
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return None

    def fetch(self) -> List[JobListing]:
        listings: List[JobListing] = []
        for term in self.search_terms:
            for page in range(1, self.pages + 1):
                query = urlencode({
                    "query": term,
                    "page": page,
                    "num_pages": 1,
                    "country": self.country,
                })
                try:
                    payload = self._get_json(f"{self.endpoint}?{query}",
                                             headers=self._rapidapi_headers())
                except JSearchQuotaExceeded as exc:
                    log.warning("[jsearch] %s", exc)
                    return listings  # stop spending calls this cycle
                if not isinstance(payload, dict):
                    break
                data = payload.get("data")
                if not isinstance(data, list):
                    break
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    listing = job_listing_from_details(item, source=self.name)
                    if listing:
                        listings.append(listing)
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
            _append_parsed(
                listings,
                title=record.get("title", ""),
                company=record.get("company", ""),
                jd=record.get("description", ""),
                source=self.name,
                url=record.get("url", ""),
                location=record.get("location", ""),
                posted_at=record.get("posted_at", ""),
            )
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
        if settings.jsearch_api_key and settings.jsearch_discovery:
            self.providers.append(JSearchProvider(
                api_key=settings.jsearch_api_key,
                country=settings.jsearch_country,
                timeout=settings.request_timeout,
            ))
        elif settings.jsearch_api_key:
            log.info("JSearch discovery disabled - the /search endpoint is "
                     "not part of the current RapidAPI subscription (set "
                     "JSEARCH_DISCOVERY_ENABLED=true when it is); the "
                     "--job-id mode still works.")
        else:
            log.info("JSearch provider disabled - set JSEARCH_API_KEY in "
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
                        try:
                            self.tracker.append(_watchlist_row(listing))
                        except Exception as exc:
                            # A locked/broken workbook must not abort the
                            # cycle - fail soft, log, and move on.
                            log.warning("Could not watchlist '%s' @ %s "
                                        "(%s) - row skipped",
                                        listing.role_title, listing.company, exc)
                        else:
                            log.info("Watchlist: '%s' @ %s [%s] - no "
                                     "application email; saved with apply "
                                     "URL (%d this cycle)",
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
