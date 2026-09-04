"""Shared text-processing utilities used across the JobPilot agent.

Includes tokenisation, stop-word filtering, JD keyword extraction, keyword
coverage scoring, e-mail extraction and filename/dedup helpers. Kept free of
heavy imports so every other module can use it safely.
"""
from __future__ import annotations

import hashlib
import re
from typing import Iterable, List, Optional, Sequence

try:  # scikit-learn ships a solid English stop-word list
    from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS as _SK_STOP_WORDS
except Exception:  # pragma: no cover - sklearn is a hard dependency
    _SK_STOP_WORDS = frozenset()

#: Domain noise words that pollute keyword scores for resumes / job
#: descriptions (generic HR boilerplate, not skills).
EXTRA_STOP_WORDS = frozenset({
    "will", "you", "your", "our", "we", "us", "they", "their", "the", "this",
    "that", "these", "those", "with", "and", "for", "are", "have", "has",
    "had", "was", "were", "but", "not", "all", "any", "can", "may", "who",
    "whom", "which", "what", "when", "where", "why", "how", "from", "into",
    "onto", "per", "via", "etc", "ie", "eg", "across", "within", "able",
    "apply", "application", "applicant", "candidate", "candidates", "role",
    "roles", "job", "jobs", "position", "positions", "company", "companies",
    "team", "teams", "work", "working", "years", "year", "experience",
    "experienced", "required", "require", "requires", "requirement",
    "requirements", "preferred", "plus", "strong", "excellent", "good",
    "great", "new", "including", "include", "includes", "must", "should",
    "would", "well", "also", "using", "use", "used", "join", "looking",
    "seeking", "hiring", "day", "days", "time", "times", "opportunity",
    "employer", "equal", "benefits", "salary", "competitive", "remote",
    "full", "part", "other", "duties", "responsibilities", "responsible",
    "skills", "skill", "ability", "knowledge", "help", "need", "needs",
})

STOP_WORDS = frozenset(_SK_STOP_WORDS) | EXTRA_STOP_WORDS

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#./-]*")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style).*?>.*?</\1>")
_WS_RE = re.compile(r"\s+")

#: Addresses/domains that are never real application inboxes.
_EMAIL_NOISE = (
    "example.com", "sentry.io", "your.email", "yourname", "email.com",
    "domain.com", "remotive.com", "arbeitnow.com", "test.com", "company.com",
    "user@", "name@", "gmail.con", "@2x", "no-reply", "noreply",
)

#: File extensions that masquerade as e-mail addresses inside JD HTML.
_EMAIL_EXT_NOISE = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
                    ".css", ".js", ".ico")


def strip_html(text: str) -> str:
    """Remove HTML tags/entities and collapse whitespace (JDs arrive as HTML)."""
    if not text:
        return ""
    text = _SCRIPT_STYLE_RE.sub(" ", text)
    text = _HTML_TAG_RE.sub(" ", text)
    for entity, char in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&nbsp;", " "), ("&#39;", "'"), ("&quot;", '"')):
        text = text.replace(entity, char)
    return _WS_RE.sub(" ", text).strip()


def tokenize(text: str) -> List[str]:
    """Lower-case alphanumeric tokenisation minus stop words."""
    tokens: List[str] = []
    for raw in _TOKEN_RE.findall(text or ""):
        token = raw.lower().strip(".-")
        if len(token) < 2 or token in STOP_WORDS or token.isdigit():
            continue
        tokens.append(token)
    return tokens


def extract_keywords(text: str, top_n: int = 30) -> List[str]:
    """Rank the most salient keywords/phrases in *text*.

    Unigrams are weighted by frequency; bigrams occurring at least twice get
    a boost because multi-word skills ("data entry", "customer service",
    "financial reporting") carry more ATS weight than their component words.
    """
    tokens = tokenize(text)
    if not tokens:
        return []
    unigram_freq: dict = {}
    for token in tokens:
        unigram_freq[token] = unigram_freq.get(token, 0) + 1
    bigram_freq: dict = {}
    for first, second in zip(tokens, tokens[1:]):
        bigram_freq[f"{first} {second}"] = bigram_freq.get(f"{first} {second}", 0) + 1
    scored = dict(unigram_freq)
    for bigram, freq in bigram_freq.items():
        if freq >= 2:
            scored[bigram] = scored.get(bigram, 0) + freq * 2
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
    return [keyword for keyword, _ in ranked[:top_n]]


def keyword_coverage(jd_text: str, document_text: str,
                     keywords: Optional[Sequence[str]] = None) -> float:
    """Fraction of the JD's top keywords present in *document_text* (0..1).

    This is the metric real ATS keyword gates measure; raw cosine similarity
    alone is not comparable across document types (see matcher.py).
    """
    if keywords is None:
        keywords = extract_keywords(jd_text, top_n=40)
    if not keywords:
        return 0.0
    haystack = " " + _WS_RE.sub(" ", (document_text or "").lower()) + " "
    hits = 0
    for keyword in keywords:
        if " " in keyword:
            if keyword in haystack:
                hits += 1
        elif re.search(rf"\b{re.escape(keyword)}\b", haystack):
            hits += 1
    return hits / len(keywords)


def find_emails(text: str) -> List[str]:
    """Extract plausible application e-mail addresses from *text*."""
    if not text:
        return []
    found: List[str] = []
    seen = set()
    for match in _EMAIL_RE.findall(text):
        candidate = match.strip(".").lower()
        if any(ext in candidate for ext in _EMAIL_EXT_NOISE):
            continue
        if any(noise in candidate for noise in _EMAIL_NOISE):
            continue
        if candidate not in seen:
            seen.add(candidate)
            found.append(candidate)
    return found


def rank_emails_for_company(emails: Iterable[str], company: str) -> Optional[str]:
    """Pick the most plausible application address (careers/hr/company domain)."""
    emails = list(emails)
    if not emails:
        return None
    company_token = re.sub(r"[^a-z0-9]", "", (company or "").lower())

    def score(addr: str) -> int:
        local, _, domain = addr.partition("@")
        rank = 0
        if any(word in local for word in
               ("career", "job", "apply", "recruit", "talent", "hiring")) or local in ("hr", "jobs"):
            rank += 4
        if company_token and company_token in domain.replace(".", ""):
            rank += 3
        if not re.search(r"\d", addr):
            rank += 1
        return rank

    return sorted(emails, key=lambda addr: (-score(addr), addr))[0]


def sanitize_filename(text: str) -> str:
    """Turn arbitrary text (roles, companies) into a safe filename fragment."""
    cleaned = re.sub(r"[^\w\s-]", "", text or "", flags=re.UNICODE)
    cleaned = re.sub(r"[\s_-]+", "_", cleaned).strip("_-")
    return cleaned or "Unknown"


def dedup_key(company: str, role_title: str, email: str) -> str:
    """Stable SHA-256 key identifying a (company, role, recipient) triple."""
    raw = (f"{(company or '').strip().lower()}|"
           f"{(role_title or '').strip().lower()}|"
           f"{(email or '').strip().lower()}")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
