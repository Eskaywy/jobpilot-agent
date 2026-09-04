# JobPilot Agent - Autonomous Job Application Pipeline

A Python agent that runs continuously inside VS Code. Every hour it:

1. **Discovers** job listings for 5 target roles (Financial Data Analyst,
   Junior Data Analyst, Email Customer Service, Data Entry, Customer Service
   Representative) via sanctioned job-board APIs.
2. **Matches** every PDF resume in `./resumes/` against each JD using
   TF-IDF + cosine similarity and keyword coverage (ATS-style). If the best
   existing resume scores **>= 90%**, it is reused; otherwise a new tailored
   resume is generated.
3. **Generates** strictly **one-page** ATS PDF resumes with the mandated
   section order: Contact Header -> Professional Summary -> Key Skills &
   Competencies -> Professional Experience -> **Education last**, with
   metric-led bullets prioritised.
4. **Drafts** a 3-paragraph cover letter PDF when the JD requires one.
5. **Dispatches** the application e-mail (3-4 sentence body + PDFs) via
   SMTP - defaulting to **DRY_RUN** so nothing is ever sent until you
   explicitly opt in.
6. **Logs** every action into `./tracker.xlsx` (open it with the Excel
   Viewer extension).

## Workspace Layout

```
AI Assistance/
├── main.py                  # entry point: hourly scheduler
├── agent/                   # modular pipeline (steps 1-6)
│   ├── config.py            # env-driven settings (incl. Adzuna keys)
│   ├── logger.py            # rotating log -> logs/agent.log
│   ├── discovery.py         # Step 1: job discovery + metadata extraction
│   ├── matcher.py           # Step 2: TF-IDF/cosine + 90% gate
│   ├── resume_generator.py  # Step 3: strict 1-page ATS PDFs
│   ├── cover_letter.py      # Step 4: 3-paragraph cover letters
│   ├── dispatcher.py        # Step 5: SMTP dispatch (dry-run safe)
│   ├── tracker.py           # Step 6: Excel ledger
│   ├── pipeline.py          # per-cycle orchestration
│   └── textutils.py         # shared keyword/email/filename helpers
├── resumes/                 # your base PDFs + generated resumes
├── cover_letters/           # generated cover letters
├── outbox/                  # DRY_RUN e-mail renders (never sent)
├── logs/                    # rotating agent log
├── fixtures/jobs.json       # offline demo listings (ENABLE_FIXTURES=true)
├── profile.yaml             # YOUR master candidate data
├── tracker.xlsx             # created automatically on first run
└── .vscode/
    ├── tasks.json           # "Agent: Run Hourly Cycle Loop" / "Run Once Now"
    └── mcp.json             # Filesystem + Puppeteer MCP servers
```

## Setup Guide

### 1. Python environment

Requires Python 3.10+. In the VS Code terminal:

```powershell
python -m venv venv
venv\\Scripts\\activate
pip install -r requirements.txt
```

(Or run the VS Code task **Terminal > Run Task > Agent: Install Dependencies**.)

### 2. Environment variables & SMTP credentials

```powershell
Copy-Item .env.example .env
```

Then edit `.env`:

* `SENDER_NAME` / `SENDER_PHONE` / `SENDER_LOCATION` - used in e-mails + PDFs.
* **Gmail**: enable 2-Factor Authentication, then create an App Password at
  <https://myaccount.google.com/apppasswords>. Set `SMTP_USER` to your Gmail
  address and `SMTP_PASSWORD` to the 16-character app password (not your
  normal password).
* **Outlook/Office365**: `SMTP_HOST=smtp.office365.com`, `SMTP_PORT=587`,
  `SMTP_USE_SSL=false`.
* `DRY_RUN=true` (default) renders every e-mail to `./outbox/` as text
  instead of sending. Only set `DRY_RUN=false` **after** you have reviewed
  dry-run output. The agent refuses to live-send with missing credentials.

### 3. Candidate profile

Edit `profile.yaml` with your real contact details, summary, skills,
experience bullets (lead with numbers: %, currency, counts) and education.
The generator tailors from this file - it never invents facts.

### 4. Base resumes

Drop 1+ existing PDF resumes into `./resumes/`. They are the matcher's
corpus; generated resumes land in the same folder with the naming pattern
`[Role]_[Company]_Resume.pdf`.

### 5. MCP servers (Filesystem + Puppeteer)

`.vscode/mcp.json` registers both servers with VS Code's native MCP support.
Requires Node.js (npx). Reload VS Code and check the MCP icon / Output panel
for both servers starting. The agent itself is API-based; use the Puppeteer
MCP interactively inside Copilot Chat for sites that need a real browser.

### 6. Excel Viewer

Install the **Excel Viewer** VS Code extension to inspect `tracker.xlsx`
without Excel. Columns: Timestamp, Company, Job Role, Recipient Email,
Keyword Match %, Cosine Similarity, Keyword Coverage, Resume Used, Cover
Letter Generated, Application Status, Source, Job URL, Notes. Close the file
in the viewer before cycles run - the agent logs a warning (and withholds
the row) if the workbook is locked.

## Running the Agent

* **Terminal > Run Task > Agent: Run Hourly Cycle Loop** - runs one cycle
  immediately, then every 60 minutes (configurable via
  `CYCLE_INTERVAL_MINUTES`). This is the default build task (Ctrl+Shift+B).
  Stop with Ctrl+C.
* **Agent: Run Once Now** - single cycle for testing (`python main.py --once`).
* Offline rehearsal: set `ENABLE_FIXTURES=true` in `.env` to process the
  three demo listings in `fixtures/jobs.json` end-to-end (still DRY_RUN).

## The 90% Threshold - How It Is Scored

Raw TF-IDF cosine similarity between a JD and a resume of different document
types rarely exceeds ~0.45 even for perfect matches, so the engine blends
two measures:

* **Calibrated cosine** = `min(raw_cosine / 0.55, 1.0)` - maps the realistic
  JD-vs-resume cosine band onto 0-1.
* **Keyword coverage** = share of the JD's top-weighted keywords present in
  the resume (what real ATS keyword gates measure).

**ATS Match Score = 0.4 x calibrated cosine + 0.6 x keyword coverage**, and
the spec's 90% threshold applies to that score. All three components are
recorded in `tracker.xlsx` so every gate decision is auditable.

## Operating Notes & Limitations

* Job sources are public, keyless APIs (Arbeitnow, Remotive), the free
  **Adzuna API** (register at <https://developer.adzuna.com> and set
  `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` / `ADZUNA_COUNTRY` in `.env` - Nigeria
  itself is not covered; use e.g. `us`, `gb`, `za`), and **SerpApi Google
  Jobs** (key from <https://serpapi.com/manage-api-key> in
  `SERPAPI_API_KEY`; free plan = 100 searches/month, one credit per search
  term per cycle, engine `google_jobs` because plain `engine=google`
  returns web results, not jobs). Some sites (LinkedIn, Indeed) forbid
  scraping - add such sources only via compliant APIs. Fixtures provide
  offline testing.
* Listings whose JDs do not contain an application e-mail are skipped
  (logged) rather than guessed.
* Auto-applying is subject to each board's terms and local law; DRY_RUN
  exists so you stay in control of everything that leaves your machine.
* If `tracker.xlsx` is open in Excel Viewer during a write, the write fails
  softly and the row is logged instead.
* The agent processes a maximum of `MAX_APPLICATIONS_PER_CYCLE` (default 5)
  listings per hourly cycle to stay rate-friendly.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError` | Activate the venv (`venv\\Scripts\\activate`) and reinstall requirements. |
| Tracker write errors | Close `tracker.xlsx` in the Excel Viewer; rerun. |
| No listings found | Network blocked, or filters too narrow - set `ENABLE_FIXTURES=true` to verify the pipeline. |
| Every resume < 90% | Expected for new JDs; the generator then builds a tailored resume. Add richer, quantified content to `profile.yaml` to lift scores. |
| SMTP auth errors | Use a Gmail **App Password**, not your login password; ensure 2FA is on. |
