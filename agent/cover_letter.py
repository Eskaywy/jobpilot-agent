"""Step 4 - Cover Letter Engine.

Drafts a concise, three-paragraph cover letter tailored to the job
description and renders it to PDF:

  Paragraph 1 (hook)      - names the exact role, the company and the single
                            strongest relevant qualification.
  Paragraph 2 (evidence)  - 2-3 quantified achievements selected from
                            profile.yaml because they share keywords with
                            the JD.
  Paragraph 3 (close)     - call to action, availability and thanks.

Storage: ``./cover_letters/Cover_Letter_[Role]_[Company].pdf``
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer

from .config import Settings
from .textutils import sanitize_filename, tokenize

log = logging.getLogger("jobpilot.cover_letter")

BODY_SIZE = 10.5
LEADING = 14.5


class CoverLetterGenerator:
    """Builds 3-paragraph, JD-tailored cover letter PDFs."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.profile: Dict = {}
        if settings.profile_path.exists():
            try:
                self.profile = yaml.safe_load(
                    settings.profile_path.read_text(encoding="utf-8")) or {}
            except Exception as exc:
                log.error("Failed to parse profile.yaml: %s", exc)

    # ------------------------------------------------------------- drafting
    def _pick_evidence(self, jd_text: str, max_items: int = 3) -> List[str]:
        """Experience bullets whose tokens overlap the JD (metrics first)."""
        jd_tokens = set(tokenize(jd_text))
        scored: List[tuple] = []
        for job in (self.profile.get("experience") or []):
            for bullet in (job.get("bullets") or []):
                tokens = set(tokenize(bullet))
                overlap = len(tokens & jd_tokens)
                metric_bonus = 2 if any(ch.isdigit() for ch in bullet) else 0
                scored.append((overlap + metric_bonus, bullet))
        scored.sort(key=lambda item: -item[0])
        return [bullet for score, bullet in scored[:max_items] if score > 0]

    # ------------------------------------------------------------- drafting
    def build_paragraphs(self, role: str, company: str, jd_text: str) -> List[str]:
        """Return exactly three paragraphs of letter text."""
        contact = self.profile.get("contact") or {}
        candidate_name = contact.get("name") or self.settings.sender_name
        summary = str(self.profile.get("summary") or "").strip()
        top_skill = (summary.split(".")[0].strip(" .") if summary
                     else "analytical and customer-focused professional")
        evidence = self._pick_evidence(jd_text)
        if not evidence:
            experience = self.profile.get("experience") or []
            for job in experience:
                evidence.extend((job.get("bullets") or [])[:1])
        evidence_body = " ".join(evidence)
        greeting = f"Dear {company} Hiring Team,"
        p1 = (f"I am writing to apply for the {role} position at {company}. "
              f"As a {top_skill}, I bring a proven record of delivering "
              f"measurable results in fast-paced, detail-driven environments. "
              f"The responsibilities described in your posting align directly "
              f"with my day-to-day strengths.")
        p2 = (f"{evidence_body} These experiences taught me to pair accuracy "
              f"with speed, to communicate clearly with customers and "
              f"stakeholders, and to keep quality high even under tight "
              f"deadlines - the exact combination your {role} role demands.")
        p3 = (f"I would welcome the opportunity to discuss how my background "
              f"can contribute to {company}'s goals. I am available to start "
              f"at your convenience and can be reached at "
              f"{contact.get('email') or self.settings.sender_email or 'the address below'} "
              f"or {contact.get('phone') or self.settings.sender_phone or 'by phone'}. "
              f"Thank you for your time and consideration.")
        closing = f"Sincerely,\n{candidate_name}"
        return [greeting, p1, p2, p3, closing]

    # -------------------------------------------------------------- writing
    def save_pdf(self, role: str, company: str, jd_text: str) -> Path:
        """Render the cover letter PDF; returns the written path."""
        self.settings.cover_letters_dir.mkdir(parents=True, exist_ok=True)
        contact = self.profile.get("contact") or {}
        name_prefix = sanitize_filename(
            str(contact.get("name") or self.settings.sender_name or "Candidate"))
        out_path = (self.settings.cover_letters_dir
                    / f"{name_prefix}_{sanitize_filename(role)}_Cover_Letter.pdf")

        contact = self.profile.get("contact") or {}
        name = contact.get("name") or self.settings.sender_name
        lines = [name, contact.get("phone") or self.settings.sender_phone,
                 contact.get("email") or self.settings.sender_email]
        header_line = "  |  ".join(str(part) for part in lines if part)

        doc = BaseDocTemplate(
            str(out_path), pagesize=LETTER,
            leftMargin=1.0 * inch, rightMargin=1.0 * inch,
            topMargin=0.8 * inch, bottomMargin=0.8 * inch,
            title=f"Cover Letter - {role} - {company}", author=name,
        )
        frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
                      id="letter")
        doc.addPageTemplates([PageTemplate(id="cover", frames=[frame])])

        style_header = ParagraphStyle(
            "CLHeader", fontName="Helvetica-Bold", fontSize=13, leading=16,
            spaceAfter=2, alignment=TA_LEFT,
        )
        style_sub = ParagraphStyle(
            "CLSub", fontName="Helvetica", fontSize=9.5, leading=12,
            textColor=colors.HexColor("#444444"), spaceAfter=14,
        )
        style_body = ParagraphStyle(
            "CLBody", fontName="Helvetica", fontSize=BODY_SIZE,
            leading=LEADING, alignment=TA_LEFT, spaceAfter=12,
        )

        story = [Paragraph(header_line, style_header), Spacer(1, 14)]
        paragraphs = self.build_paragraphs(role, company, jd_text)
        for text in paragraphs:
            story.append(Paragraph(text.replace("\n", "<br/>"), style_body))

        doc.build(story)
        log.info("Cover letter saved: %s", out_path.name)
        return out_path
