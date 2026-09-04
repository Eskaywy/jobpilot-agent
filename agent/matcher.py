"""Step 2 - Resume Matching & 90% Keyword Threshold Engine.

Scans every PDF in ``./resumes/``, extracts its text with PyPDF2 and scores
it against a job description using two complementary measures:

1. **Cosine similarity** over scikit-learn TF-IDF vectors (spec requirement).
   Raw cosine between different document *types* (JD vs resume) tops out
   around 0.15-0.45 even for excellent matches, because TF-IDF cosine is only
   directly comparable between documents of the same kind. To make the
   number useful we calibrate it: ``min(cosine / 0.55, 1.0)`` maps the
   realistic 0-0.55 band onto 0-1.
2. **Keyword coverage** - the share of the JD's top-weighted keywords present
   in the resume, which is what actual ATS keyword gates measure.

The **ATS Match Score** reported to the tracker and used for the 90% gate is
``0.4 * calibrated cosine + 0.6 * keyword coverage``. The threshold remains
0.90 as specified; all three components are recorded for auditability.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from PyPDF2 import PdfReader
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from .config import Settings
from .textutils import extract_keywords, keyword_coverage

log = logging.getLogger("jobpilot.matcher")

#: Upper bound of the realistic JD-vs-resume cosine band (calibration).
COSINE_CEILING = 0.55

#: Blended-score weights (sum to 1.0).
WEIGHT_COSINE = 0.4
WEIGHT_COVERAGE = 0.6


@dataclass
class MatchResult:
    """Outcome of scoring one resume against one JD."""

    resume_path: Optional[Path]
    cosine_raw: float = 0.0
    cosine_calibrated: float = 0.0
    keyword_coverage: float = 0.0
    ats_match_score: float = 0.0
    matched_keywords: List[str] = field(default_factory=list)
    missing_keywords: List[str] = field(default_factory=list)
    meets_threshold: bool = False


class ResumeMatcher:
    """TF-IDF + keyword matcher over the PDF resume library."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.threshold = min(max(settings.match_threshold, 0.0), 1.0)

    # ------------------------------------------------------------------ I/O
    def list_resumes(self) -> List[Path]:
        """All PDFs currently in ./resumes/ (sorted for determinism)."""
        if not self.settings.resumes_dir.exists():
            return []
        return sorted(self.settings.resumes_dir.glob("*.pdf"))

    @staticmethod
    def read_pdf_text(pdf_path: Path) -> str:
        """Best-effort text extraction; a corrupt PDF yields empty text."""
        try:
            reader = PdfReader(str(pdf_path))
            return " ".join(
                (page.extract_text() or "") for page in reader.pages
            ).strip()
        except Exception as exc:
            log.warning("Could not extract text from %s: %s", pdf_path.name, exc)
            return ""

    # -------------------------------------------------------------- scoring
    def score(self, jd_text: str, resume_text: str,
              jd_keywords: Optional[List[str]] = None) -> Tuple[float, float, float]:
        """Return ``(raw_cosine, calibrated_cosine, keyword_coverage)``."""
        cos_raw = 0.0
        if jd_text.strip() and resume_text.strip():
            try:
                vectors = TfidfVectorizer(
                    stop_words="english", ngram_range=(1, 2), sublinear_tf=True,
                ).fit_transform([jd_text, resume_text])
                cos_raw = float(cosine_similarity(vectors[0], vectors[1])[0][0])
            except ValueError:
                cos_raw = 0.0  # degenerate vocabulary (e.g. empty after stopwords)
        cos_cal = min(cos_raw / COSINE_CEILING, 1.0) if cos_raw > 0 else 0.0
        coverage = keyword_coverage(jd_text, resume_text, jd_keywords)
        return cos_raw, cos_cal, coverage

    def blend(self, cos_calibrated: float, coverage: float) -> float:
        return round(WEIGHT_COSINE * cos_calibrated + WEIGHT_COVERAGE * coverage, 4)

    # ------------------------------------------------------------ selection
    def find_best_match(self, jd_text: str) -> MatchResult:
        """Score every resume in ./resumes/ against *jd_text*.

        Returns the highest-scoring :class:`MatchResult`. When no resume
        exists at all the result has ``resume_path=None`` and
        ``meets_threshold=False`` so the pipeline triggers the ATS generator.
        """
        jd_keywords = extract_keywords(jd_text, top_n=40)
        resumes = self.list_resumes()
        if not resumes:
            log.warning("No resumes found in %s", self.settings.resumes_dir)
            return MatchResult(resume_path=None, missing_keywords=jd_keywords[:10])

        best: Optional[MatchResult] = None
        for pdf_path in resumes:
            resume_text = self.read_pdf_text(pdf_path)
            cos_raw, cos_cal, coverage = self.score(jd_text, resume_text, jd_keywords)
            blended = self.blend(cos_cal, coverage)
            result = MatchResult(
                resume_path=pdf_path,
                cosine_raw=round(cos_raw, 4),
                cosine_calibrated=round(cos_cal, 4),
                keyword_coverage=round(coverage, 4),
                ats_match_score=blended,
                meets_threshold=blended >= self.threshold,
            )
            if best is None or result.ats_match_score > best.ats_match_score:
                best = result

        if best and best.resume_path is not None:
            top_text = self.read_pdf_text(best.resume_path).lower()
            best.matched_keywords = [kw for kw in jd_keywords
                                     if (" " in kw and kw in top_text)
                                     or (" " not in kw and kw in top_text.split())][:10]
            best.missing_keywords = [kw for kw in jd_keywords
                                     if kw not in best.matched_keywords][:10]

        if best:
            log.info("Best match: %s | cosine=%.3f (raw %.3f) | coverage=%.1f%% | "
                     "ATS score=%.1f%% | threshold=%.0f%% | meets=%s",
                     best.resume_path.name if best.resume_path else "-",
                     best.cosine_calibrated, best.cosine_raw,
                     best.keyword_coverage * 100, best.ats_match_score * 100,
                     self.threshold * 100, best.meets_threshold)
        return best or MatchResult(resume_path=None)
