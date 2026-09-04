"""Step 5 - Application & Email Dispatcher.

Sends the application e-mail (3-4 sentence body) with the tailored resume
PDF - and the cover letter PDF when one was generated - via SMTP.

Safety model:
* ``DRY_RUN=true`` (the default) renders the full e-mail (headers, body and
  attachment list) as a ``.txt`` file into ``./outbox/`` and never opens a
  network connection. Verify those files before flipping to live mode.
* Live mode requires SMTP_USER + SMTP_PASSWORD; missing credentials force
  dry-run behaviour with a warning.
* Live sends retry 3 times with exponential backoff before failing.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import List, Optional

from .config import Settings
from .textutils import sanitize_filename

log = logging.getLogger("jobpilot.dispatcher")

#: (attempt delay seconds) - exponential backoff between live SMTP tries.
RETRY_DELAYS = (5.0, 10.0, 20.0)


class EmailDispatcher:
    """SMTP dispatcher with an outbox-rendering dry-run mode."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.live_mode = (not settings.dry_run
                          and bool(settings.smtp_user and settings.smtp_password))
        if settings.dry_run:
            log.info("DRY_RUN=true - e-mails will be rendered to %s and NOT sent.",
                     settings.outbox_dir)
        elif not self.live_mode:
            log.warning("DRY_RUN=false but SMTP credentials are missing - "
                        "falling back to dry-run mode for this session.")

    # ---------------------------------------------------------------- body
    def build_email_body(self, role: str, company: str,
                         cover_letter_attached: bool) -> str:
        """Brief, professional 3-4 sentence application e-mail."""
        sender = self.settings.sender_name or "the sender"
        if cover_letter_attached:
            middle = (", and a tailored cover letter is attached as well. "
                      "I believe it maps closely to what your team needs")
        else:
            middle = ", and I believe it maps closely to what your team needs"
        return (
            f"Dear Hiring Manager,\n\n"
            f"I am applying for the {role} role at {company}. The attached "
            f"one-page resume summarises my relevant, quantified experience"
            f"{middle}. I would welcome the chance to discuss how I can "
            f"contribute to {company}. Thank you for your time and "
            f"consideration.\n\n"
            f"Best regards,\n"
            f"{sender}\n"
            f"{self.settings.sender_phone}\n"
            f"{self.settings.sender_email or self.settings.smtp_user}"
        )

    # ------------------------------------------------------------- dispatch
    def send_application(self, listing, resume_path: Path,
                         cover_letter_path: Optional[Path]) -> str:
        """Dispatch one application; returns ``"SENT"`` or ``"DRY_RUN"``."""
        subject = f"Application: {listing.role_title} - {self.settings.sender_name}"
        body = self.build_email_body(
            listing.role_title, listing.company,
            cover_letter_attached=cover_letter_path is not None)
        attachments = [resume_path] + ([cover_letter_path] if cover_letter_path else [])

        if not self.live_mode:
            return self._render_dry_run(listing.application_email, subject,
                                        body, attachments)

        message = EmailMessage()
        message["From"] = formataddr((self.settings.sender_name,
                                      self.settings.smtp_user))
        message["To"] = listing.application_email
        message["Subject"] = subject
        message.set_content(body)
        for attachment in attachments:
            data = Path(attachment).read_bytes()
            message.add_attachment(data, maintype="application",
                                   subtype="pdf",
                                   filename=Path(attachment).name)
        self._send_smtp(message)
        log.info("Application e-mail SENT to %s (%s)",
                 listing.application_email, listing.company)
        return "SENT"

    def _render_dry_run(self, to_addr: str, subject: str, body: str,
                        attachments: List[Path]) -> str:
        """Write the would-be e-mail to ./outbox/ as plain text."""
        self.settings.outbox_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = (self.settings.outbox_dir
                    / f"{stamp}_{sanitize_filename(subject)}.txt")
        lines = [f"To: {to_addr}",
                 f"From: {formataddr((self.settings.sender_name, self.settings.smtp_user))}",
                 f"Subject: {subject}",
                 "Attachments: " + ", ".join(str(Path(a).name) for a in attachments),
                 "-" * 60, body,
                 "-" * 60,
                 "[DRY RUN] This e-mail was NOT sent. "
                 "Set DRY_RUN=false in .env to enable live dispatch."]
        out_path.write_text("\n".join(lines), encoding="utf-8")
        log.info("DRY-RUN e-mail rendered to %s", out_path.name)
        return "DRY_RUN"

    # ---------------------------------------------------------- live sending
    def _send_smtp(self, message: EmailMessage) -> None:
        """Send with retry + exponential backoff; raises on final failure."""
        context = ssl.create_default_context()
        last_error: Optional[Exception] = None
        for attempt, delay in enumerate(RETRY_DELAYS, start=1):
            try:
                if self.settings.smtp_use_ssl:
                    with smtplib.SMTP_SSL(self.settings.smtp_host,
                                          self.settings.smtp_port,
                                          timeout=self.settings.request_timeout,
                                          context=context) as server:
                        server.login(self.settings.smtp_user,
                                     self.settings.smtp_password)
                        server.send_message(message)
                else:  # STARTTLS (e.g. smtp.office365.com:587)
                    with smtplib.SMTP(self.settings.smtp_host,
                                      self.settings.smtp_port,
                                      timeout=self.settings.request_timeout) as server:
                        server.ehlo()
                        server.starttls(context=context)
                        server.login(self.settings.smtp_user,
                                     self.settings.smtp_password)
                        server.send_message(message)
                return
            except (smtplib.SMTPException, OSError) as exc:
                last_error = exc
                log.warning("SMTP send attempt %d/%d failed: %s",
                            attempt, len(RETRY_DELAYS), exc)
                if attempt < len(RETRY_DELAYS):
                    import time
                    time.sleep(delay)
        raise RuntimeError(f"SMTP dispatch failed after "
                           f"{len(RETRY_DELAYS)} attempts: {last_error}")
