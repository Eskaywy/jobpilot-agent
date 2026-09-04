"""Step 3 - ATS Resume Generation Module (strict formatting directives).

Generates a tailored, ATS-parsable, single-page PDF resume with ReportLab
enforcing the layout rules from the specification:

* **1-page constraint** - content flows inside a fixed frame; if it would
  overflow, the generator progressively shrinks the body font (10 -> 8.5pt),
  tightens leading and finally truncates the lowest-priority bullet until the
  frame fits. The page count of the written file is asserted before return.
* **Impact prioritisation** - JD keywords are extracted and used to re-rank
  experience bullets, preferring bullets that contain quantified metrics
  (regex-detected numbers, %, currency, multipliers) and matched keywords.
* **Section hierarchy (strict order)** - Contact Header, Professional
  Summary (dense/quantifiable), Key Skills & Competencies, Professional
  Experience, Education LAST.

Candidate master data lives in ``profile.yaml`` (contact, summary, skills,
experience bullets with metrics, education). Tailoring = keyword matching
between the JD and profile content, never fabrication.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml
from PyPDF2 import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate,
                                Paragraph, Spacer, Table, TableStyle)

from .config import Settings
from .textutils import extract_keywords, sanitize_filename, tokenize

log = logging.getLogger("jobpilot.resume_generator")

#: Fonts kept minimal on purpose - ATS parsers choke on exotic encodings.
FONT_REGULAR = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
FONT_ITALIC = "Helvetica-Oblique"

#: Body font scale used by the auto-fit loop (first fitting size wins).
FONT_SIZES = (10.0, 9.5, 9.0, 8.5)

_METRIC_RE = re.compile(
    r"(\d+(?:[.,]\d+)*\s?%|\$\s?\d[\d,.]*|\b\d+\s?[kKmMbB]?\b|\b[a-z]+\s?x\b)", re.I)


def has_metrics(text: str) -> bool:
    """True when *text* contains a quantified achievement (%, $, x, count)."""
    return bool(_METRIC_RE.search(text or ""))


class ATSResumeGenerator:
    """Builds a strictly one-page, tailored ATS resume PDF from profile.yaml."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.profile: Dict = {}
        if settings.profile_path.exists():
            try:
                self.profile = yaml.safe_load(
                    settings.profile_path.read_text(encoding="utf-8")) or {}
            except Exception as exc:
                log.error("Failed to parse profile.yaml: %s", exc)

    # ------------------------------------------------------------- tailoring
    def _rank_bullets(self, jd_text: str) -> List[Dict]:
        """Flatten all experience bullets, scored for this JD.

        Score = keyword overlap + metric bonus; sorted descending so the most
        impactful bullets lead and the weakest are truncated first when the
        page overflows. Each job keeps its bullets in ranked order.
        """
        jd_tokens = set(tokenize(jd_text))
        ranked_jobs: List[Dict] = []
        for job in (self.profile.get("experience") or []):
            scored: List[tuple] = []
            for bullet in (job.get("bullets") or []):
                tokens = set(tokenize(bullet))
                overlap = len(tokens & jd_tokens)
                score = overlap * 2 + (3 if has_metrics(bullet) else 0)
                scored.append((-score, bullet))
            scored.sort()
            ranked_jobs.append({
                "company": job.get("company") or "",
                "title": job.get("title") or "",
                "location": job.get("location") or "",
                "period": job.get("period") or "",
                "bullets": [bullet for _, bullet in scored],
            })
        return ranked_jobs

    def _rank_skills(self, jd_text: str, limit: Optional[int] = None) -> List[str]:
        """Skills reordered by JD keyword overlap; optionally capped."""
        jd_tokens = set(tokenize(jd_text))
        skills = [str(skill) for skill in (self.profile.get("skills") or [])]
        scored = sorted(
            skills,
            key=lambda skill: (-len(set(tokenize(skill)) & jd_tokens), skill.lower()),
        )
        return scored[:limit] if limit else scored

    def _summary_text(self, jd_text: str) -> str:
        """Professional summary from profile.yaml (dense, quantifiable)."""
        summary = str(self.profile.get("summary") or "").strip()
        if not summary:
            return ("Results-driven professional delivering quantified "
                    "improvements in data quality, reporting speed and "
                    "customer satisfaction.")
        return summary

    # ------------------------------------------------------------- rendering
    def _styles(self, body_size: float) -> Dict[str, ParagraphStyle]:
        """Paragraph styles scaled from *body_size* (pt)."""
        lead = body_size * 1.22
        section = body_size * 1.15
        name = body_size * 1.9
        return {
            "name": ParagraphStyle("Name", fontName=FONT_BOLD, fontSize=name,
                                   leading=name * 1.15, alignment=TA_CENTER,
                                   spaceAfter=3, textColor=colors.black),
            "contact": ParagraphStyle("Contact", fontName=FONT_REGULAR,
                                      fontSize=body_size * 0.92,
                                      leading=body_size * 1.1,
                                      alignment=TA_CENTER, spaceAfter=6,
                                      textColor=colors.black),
            "section": ParagraphStyle("Section", fontName=FONT_BOLD,
                                      fontSize=section, leading=section * 1.2,
                                      spaceBefore=7, spaceAfter=2,
                                      textColor=colors.HexColor("#1a1a1a")),
            "body": ParagraphStyle("Body", fontName=FONT_REGULAR,
                                   fontSize=body_size, leading=lead,
                                   textColor=colors.black),
            "bullet": ParagraphStyle("Bullet", fontName=FONT_REGULAR,
                                     fontSize=body_size, leading=lead,
                                     leftIndent=12, bulletIndent=3,
                                     spaceAfter=1.5, textColor=colors.black),
            "job": ParagraphStyle("Job", fontName=FONT_BOLD, fontSize=body_size,
                                  leading=lead, spaceBefore=3, spaceAfter=0.5,
                                  textColor=colors.black),
            "meta": ParagraphStyle("Meta", fontName=FONT_ITALIC,
                                   fontSize=body_size * 0.9,
                                   leading=body_size * 1.05, spaceAfter=2,
                                   textColor=colors.HexColor("#333333")),
        }

    def _build_story(self, role: str, company: str, jd_text: str,
                     body_size: float, skill_limit: int,
                     max_bullets_per_job: int) -> List:
        """Assemble the platypus story in the mandated section order."""
        styles = self._styles(body_size)
        contact = self.profile.get("contact") or {}
        story: List = []

        # 1. Contact header -------------------------------------------------
        story.append(Paragraph(str(contact.get("name") or "Candidate"),
                               styles["name"]))
        bits = [str(contact.get(k)) for k in
                ("phone", "email", "location", "linkedin") if contact.get(k)]
        story.append(Paragraph("  |  ".join(bits), styles["contact"]))

        # 2. Professional summary (dense, quantifiable) --------------------
        story.append(Paragraph("PROFESSIONAL SUMMARY", styles["section"]))
        story.append(Paragraph(self._summary_text(jd_text), styles["body"]))

        # 3. Key skills & competencies --------------------------------------
        story.append(Paragraph("KEY SKILLS & COMPETENCIES", styles["section"]))
        story.append(Paragraph(
            "  |  ".join(self._rank_skills(jd_text, skill_limit)),
            styles["body"]))

        # 4. Professional experience (impact-led bullets) -------------------
        story.append(Paragraph("PROFESSIONAL EXPERIENCE", styles["section"]))
        for job in self._rank_bullets(jd_text):
            title_line = (f"{job['title']}  -  {job['company']}"
                          if job["company"] else job["title"])
            meta_bits = [job["location"], job["period"]]
            meta_line = "  |  ".join(bit for bit in meta_bits if bit)
            story.append(Paragraph(title_line, styles["job"]))
            if meta_line:
                story.append(Paragraph(meta_line, styles["meta"]))
            for bullet in job["bullets"][:max_bullets_per_job]:
                story.append(Paragraph(f"- {bullet}", styles["bullet"]))

        # 5. Education - MUST remain the final section ----------------------
        story.append(Paragraph("EDUCATION", styles["section"]))
        for entry in (self.profile.get("education") or []):
            line = str(entry.get("degree") or "")
            if entry.get("school"):
                line += f", {entry['school']}"
            if entry.get("year"):
                line += f" ({entry['year']})"
            if entry.get("details"):
                line += f" - {entry['details']}"
            story.append(Paragraph(line, styles["bullet"]))

        return story

    def _fits_one_page(self, story: List) -> bool:
        """Render into a throwaway in-memory PDF; True iff exactly 1 page."""
        from io import BytesIO
        buffer = BytesIO()
        doc = BaseDocTemplate(buffer, pagesize=LETTER,
                              leftMargin=0.55 * inch, rightMargin=0.55 * inch,
                              topMargin=0.45 * inch, bottomMargin=0.45 * inch)
        frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
                      id="probe")
        doc.addPageTemplates([PageTemplate(id="probe", frames=[frame])])
        doc.build(list(story))
        pages = doc.page
        buffer.close()
        return pages == 1

    # ------------------------------------------------------------- generate
    def generate(self, role: str, company: str, jd_text: str) -> Path:
        """Create ``./resumes/[Name]_[Role]_Resume.pdf`` (1 page, forced).

        Auto-fit ladder: starts at 10pt with full skills/bullets and degrades
        deliberately (smaller font -> fewer skills -> fewer bullets per job)
        until the layout fits one page. Education is never dropped.
        """
        self.settings.resumes_dir.mkdir(parents=True, exist_ok=True)
        contact = self.profile.get("contact") or {}
        name_prefix = sanitize_filename(
            str(contact.get("name") or self.settings.sender_name or "Candidate"))
        out_path = (self.settings.resumes_dir
                    / f"{name_prefix}_{sanitize_filename(role)}_Resume.pdf")

        ladder = [
            (10.0, 10, 4), (9.5, 10, 4), (9.5, 8, 3), (9.0, 8, 3),
            (9.0, 7, 2), (8.5, 6, 2), (8.5, 5, 2),
        ]
        chosen = None
        for body_size, skill_limit, bullet_cap in ladder:
            story = self._build_story(role, company, jd_text, body_size,
                                      skill_limit, bullet_cap)
            if self._fits_one_page(story):
                chosen = (body_size, skill_limit, bullet_cap)
                break
        if chosen is None:  # absolute worst case: last rung, already minimal
            chosen = ladder[-1]

        body_size, skill_limit, bullet_cap = chosen
        story = self._build_story(role, company, jd_text, body_size,
                                  skill_limit, bullet_cap)
        contact = self.profile.get("contact") or {}
        doc = BaseDocTemplate(
            str(out_path), pagesize=LETTER,
            leftMargin=0.55 * inch, rightMargin=0.55 * inch,
            topMargin=0.45 * inch, bottomMargin=0.45 * inch,
            title=f"{contact.get('name', 'Candidate')} - {role}",
            author=str(contact.get("name") or self.settings.sender_name),
        )
        frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
                      id="resume")
        doc.addPageTemplates([PageTemplate(id="resume", frames=[frame])])
        doc.build(story)

        # Hard verification of the 1-page constraint on the written artifact.
        page_count = len(PdfReader(str(out_path)).pages)
        if page_count != 1:
            raise RuntimeError(
                f"Resume {out_path.name} rendered to {page_count} pages; "
                "the 1-page constraint was violated.")
        log.info("ATS resume saved: %s (font %.1fpt, %d skills, <=%d bullets/job)",
                 out_path.name, body_size, skill_limit, bullet_cap)
        return out_path


if __name__ == "__main__":  # standalone smoke test: python -m agent.resume_generator
    from .config import Settings as _S

    demo_jd = (
        "We are looking for a Junior Data Analyst. Responsibilities: build "
        "Excel dashboards and Power BI reports, run SQL queries, reconcile "
        "financial data, and support KPI reporting. Requirements: advanced "
        "Excel (PivotTables, VLOOKUP), SQL basics, data cleaning, attention "
        "to detail, and strong customer service communication skills. "
        "2+ years of experience with data analysis and reporting required. "
        "Send applications to careers@example-analytics.com."
    )
    generator = ATSResumeGenerator(_S.from_env())
    path = generator.generate("Junior Data Analyst", "Demo Company", demo_jd)
    print(f"Demo resume written to {path}")
