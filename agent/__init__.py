"""JobPilot - autonomous job-search, resume-tailoring and application agent.

Package layout (one module per pipeline step):
    discovery        Step 1 - job discovery & metadata extraction
    matcher          Step 2 - TF-IDF/cosine matching + 90% threshold gate
    resume_generator Step 3 - strict 1-page ATS PDF generation
    cover_letter     Step 4 - 3-paragraph tailored cover letter PDFs
    dispatcher       Step 5 - SMTP application dispatch (dry-run safe)
    tracker          Step 6 - Excel tracking ledger (tracker.xlsx)
"""

__version__ = "1.0.0"
