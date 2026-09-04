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


# --------------------------------------------------------------------------
# Per-job highlight extraction
# --------------------------------------------------------------------------

#: Named tools/software are the strongest signal of a concrete, mentionable
#: JD detail (what the day-to-day work actually touches).
_HIGHLIGHT_TOOLS = (
    "excel", "sql", "power bi", "powerbi", "tableau", "salesforce",
    "zendesk", "sap", "quickbooks", "python", "google sheets",
    "google workspace", "microsoft office", "ms office", "oracle",
    "crm", "erp", "netsuite", "hubspot", "servicenow", "jira",
    "pivot table", "pivottable", "macros", "vlookup",
    "dashboard", "spreadsheet", "reporting tool",
)

#: Duty verbs mark sentences that describe actual work, not requirements.
_HIGHLIGHT_DUTY_VERBS = (
    "prepare", "reconcile", "analy", "report", "maintain", "manage",
    "process", "monitor", "support", "create", "build", "develop",
    "respond", "resolve", "handle", "assist", "track", "audit",
    "compile", "update", "coordinate", "enter", "review", "clean",
    "communicate", "collaborate", "ensure", "perform", "produce",
)

#: HR/lorem boilerplate that must never become the highlight.
_HIGHLIGHT_BOILERPLATE = (
    "equal opportunity", "affirmative action", "benefit", "insurance",
    "401(k)", "paid time off", "pto", "vacation", "about us",
    "who we are", "our mission", "apply now", "click", "drug",
    "background check", "e-verify", "accommodation", "salary",
    "compensation", "we offer", "perks", "referral", "how to apply",
)

_HIGHLIGHT_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")

#: Leading filler stripped so the phrase reads naturally mid-sentence.
_HIGHLIGHT_LEAD_RE = re.compile(
    r"^(?:[-\u2022*o\d]+[.)\]:]*\s+)*"          # bullets / list markers
    r"(?:you(?:r)?\s+(?:will|would|are\s+to)\s+|"
    r"the\s+(?:ideal|successful|right)\s+candidate\s+(?:will|should|must)\s+|"
    r"the\s+[a-z][a-z\s]{0,40}?\s+(?:will|would)\s+|"   # "the clerk will ..."
    r"we\s+are\s+(?:looking|seeking)(?:\s+for\s+someone)?\s+(?:to|who\s+will)\s+|"
    r"(?:key\s+)?(?:responsibilities|duties|essential\s+functions)\s*"
    r"(?:include|are|:)?\s*)",
    flags=re.IGNORECASE,
)


def _highlight_sentence_score(sentence: str) -> int:
    """Specificity score for one JD sentence (negative == boilerplate)."""
    low = sentence.lower()
    if any(marker in low for marker in _HIGHLIGHT_BOILERPLATE):
        return -1
    words = len(sentence.split())
    if words < 5 or words > 60:     # too thin or too rambling to quote
        return 0
    score = 0
    if any(tool in low for tool in _HIGHLIGHT_TOOLS):
        score += 4
    if re.search(r"\d", sentence):  # any numeric detail (%, $, counts)
        score += 3
    if re.search(r"\d+\s*%|\$|kpi|sla|metric|target", low):
        score += 1
    score += sum(2 for verb in _HIGHLIGHT_DUTY_VERBS if verb in low)
    if 8 <= words <= 35:            # information-dense sweet spot
        score += 1
    return score


def _highlight_phrase(sentence: str, max_words: int = 18) -> str:
    """Trim one scored sentence into a phrase that reads mid-sentence."""
    phrase = sentence.strip().rstrip(".!?;:")
    phrase = _HIGHLIGHT_LEAD_RE.sub("", phrase)
    words = phrase.split()
    if len(words) > max_words:
        phrase = " ".join(words[:max_words]).rstrip(",;:-")
    if phrase and phrase[:1].isupper() and not phrase.split()[0].isupper():
        phrase = phrase[0].lower() + phrase[1:]
    return phrase


def extract_job_highlight(jd_text: str) -> str:
    """Pull the single most concrete, mention-worthy detail from a job ad.

    Scores every sentence for specificity (named tools, metrics, duty
    verbs), suppresses HR boilerplate, and returns a short phrase that can
    be dropped into an e-mail or cover-letter sentence, e.g.
    ``"preparing monthly financial reports using Excel and Power BI"``.
    Returns ``""`` when the JD is too thin to say anything specific - the
    caller then falls back to its neutral wording.
    """
    text = strip_html(jd_text or "")
    if not text:
        return ""
    best, best_score = "", 0
    for sentence in _HIGHLIGHT_SENTENCE_SPLIT_RE.split(text):
        score = _highlight_sentence_score(sentence)
        if score > best_score:      # earliest highest-scoring sentence wins
            best, best_score = sentence, score
    if best_score < 2:
        return ""
    return _highlight_phrase(best)
    """Stable SHA-256 key identifying a (company, role, recipient) triple."""
    raw = (f"{(company or '').strip().lower()}|"
           f"{(role_title or '').strip().lower()}|"
           f"{(email or '').strip().lower()}")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
