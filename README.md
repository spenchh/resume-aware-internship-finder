# Resume-Aware Internship Finder

Takes your resume, searches multiple **date-reliable** sources for internships
that match your background, **verifies every listing is actually still live before
reporting it**, scores the matches, and emits a detailed Markdown/HTML report.

**Works for any field.** Upload a marketing, finance, design, biology, nursing,
or engineering resume — it reads *your* skills (from your skills, projects, and
experience) and the roles you say you're targeting, then matches against those.
A curated hardware/embedded domain lexicon adds synonym-expansion on top for
those candidates, but nothing assumes you're an engineer.

> **Design priority:** freshness. A listing only appears if it has a determinable
> recent posted date **or** an upcoming deadline, **and** its application URL
> passes a live re-check in the same run that generates the report. When a date
> can't be verified, it's labeled `[unverified]` — never silently presented as
> fresh. See [Freshness validation](#freshness-validation-the-core).

---

## 🌐 Use it on the web (no install — just upload your resume)

The entire tool runs in the browser via **`streamlit_app.py`**: upload a resume,
choose your role focus and preferred work mode, click **Find internships**, and
it parses, searches, live-verifies, scores, and lists the results — with filters
and downloadable reports. Nothing to install for whoever uses it.

**Deploy your own copy (free, ~3 minutes):**

1. Make sure this repo is on GitHub (it is: `spenchh/resume-aware-internship-finder`).
2. Go to **[share.streamlit.io](https://share.streamlit.io)** and sign in with GitHub.
3. Click **Create app → Deploy a public app from GitHub** and choose:
   - **Repository:** `spenchh/resume-aware-internship-finder`
   - **Branch:** `main`
   - **Main file path:** `streamlit_app.py`
4. *(Optional)* Under **Advanced settings → Secrets**, paste any API keys you have
   (all optional — the app works without them):
   ```toml
   OPENROUTER_API_KEY = "sk-or-..."   # enables open-weight GLM 5.2 match scoring
   ANTHROPIC_API_KEY  = "sk-ant-..."  # optional Claude fallback scoring
   SERPAPI_API_KEY    = "..."         # enables Google Jobs source
   GITHUB_TOKEN       = "..."         # higher rate limit for curated GitHub lists
   ```
5. Click **Deploy**. You'll get a public URL like
   `https://spenchh-resume-aware-internship-finder.streamlit.app` — that's your app.

Re-deploys are automatic on every `git push`.

**Run the web app locally** (optional):

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

---

## Quickstart (command line)

```bash
# 1. Install (Python 3.10+)
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS/Linux
pip install -r requirements.txt

# 2. Run against your resume
python main.py --resume path/to/your_resume.pdf

# 3. Open the report
#    reports/latest.md   (and reports/latest.html)
```

First run with the bundled sample:

```bash
python main.py --resume sample_data/sample_resume.txt --format both
```

### Optional extras

```bash
pip install -r requirements-optional.txt      # optional Claude fallback + Streamlit dashboard
copy .env.example .env                         # then fill in keys you have
```

- `OPENROUTER_API_KEY` → enables open-weight AI match scoring through OpenRouter
  using `z-ai/glm-5.2` by default.
- `ANTHROPIC_API_KEY` → optional Claude fallback scoring if OpenRouter is not set.
- `SERPAPI_API_KEY` → enables the Google Jobs breadth source, including broad
  startup web queries beyond YC.
- `GITHUB_TOKEN` → higher GitHub API rate limit for curated-list freshness checks.

The tool runs fully without any of these.

---

## Usage

```
python main.py --resume RESUME [options]

  --resume, -r       Resume file (PDF / DOCX / TXT)              [required]
  --config, -c       Config YAML (default: config.yaml)
  --term             Target term, e.g. "Summer 2027"
  --recency-days     Posted-within window (default 21)
  --deadline-days    Deadline lookahead window (default 14)
  --output, -o       Output directory (default: reports/)
  --format           markdown | html | both
  --no-live-check    Skip the live URL re-check (faster, weaker freshness)
  --llm              auto | always | never  (AI scoring mode)
  --llm-provider     auto | openrouter | anthropic
  --llm-model        Override provider model, e.g. z-ai/glm-5.2
  --max-llm          Cap listings sent to the LLM
  --cache            SQLite cache path (default: cache.db)
  --sources          Restrict sources, e.g. greenhouse,lever,ashby
  --verbose, -v      Debug logging
```

Examples:

```bash
# Tighter freshness, HTML report
python main.py -r resume.pdf --recency-days 14 --deadline-days 7 --format html

# Only the most date-reliable sources, with open-weight scoring forced on
python main.py -r resume.pdf --sources greenhouse,lever,ashby,schemaorg --llm always --llm-provider openrouter

# Fast pass without the live re-check
python main.py -r resume.pdf --no-live-check
```

Also runnable as a module: `python -m internfinder --resume resume.pdf`.

---

## How it works

```
resume ─▶ resume_parser ─▶ weighted keywords (+ synonym expansion)
                                   │
 sources/ (Greenhouse, Lever, Ashby, schema.org, GitHub lists, YC, Wellfound, SerpAPI)
                                   │  raw listings
                                   ▼
 cache.observe (first-seen) ─▶ freshness.resolve_date ─▶ dedupe
                                   ▼
 freshness.mark_eligibility (recency OR deadline window)
                                   ▼
 freshness.live_check  ── re-request every URL, drop dead ──▶ retained
                                   ▼
 matcher.score_listings (keyword, OpenRouter, or Claude) ─▶ report_generator ─▶ report
```

| Module | Responsibility |
|---|---|
| `internfinder/resume_parser.py` | Extract skills/tools/coursework/degree/titles; build a field-agnostic weighted keyword map from the resume and optional target focus. |
| `internfinder/domain.py` | Generic term extraction plus optional domain synonym expansion when specialized terms appear. |
| `internfinder/sources/*` | One fetcher per source. Each fails soft. |
| `internfinder/freshness_validator.py` | **The core** — date resolution, eligibility windows, live-check, dedup. |
| `internfinder/matcher.py` | 0–100 match scoring (keyword, open-weight OpenRouter, or Claude). |
| `internfinder/report_generator.py` | Markdown/HTML report, verified vs unverified sections. |
| `internfinder/cache.py` | SQLite first-seen tracking + run-over-run diff. |
| `internfinder/cli.py` | Orchestration + CLI. |

---

## Data sources (ranked by date-reliability)

**Tier 1 — most reliable when explicitly configured:**
- **Greenhouse / Lever / Ashby** public JSON board APIs. Structured post dates
  (`updated_at` / `createdAt` / `publishedAt`); a closed role 404s or disappears,
  which makes freshness trivial to confirm. Configure company slugs in
  `config.yaml` only when you intentionally want specific boards. The default
  lists are empty so the app does not secretly favor one field or company set.
- **schema.org JobPosting JSON-LD** — point `schemaorg_urls` at any career page;
  `datePosted`/`validThrough` are the most reliable date fields available.
- **Curated GitHub tracking repos** — README tables where maintainers mark
  closures. The default repo list is empty; add repos only when you want that
  specific community list. The repo's last-commit date is checked first; a stale
  repo is skipped.

**Tier 2 — startup-specific:** YC (`yc_jobs`, broad public YC company dataset by
default, ranked toward active/hiring/small/recent companies, then public YC
profile jobs and ATS boards), Wellfound (off by default; ToS-respecting
user-export only).

**Tier 3 — broad web search, lower date trust:** SerpAPI Google Jobs. This is the
main "search the internet broadly" source: query = internship + optional role
focus + optional term. Relative dates are always labeled *approximate*; the
source auto-skips without `SERPAPI_API_KEY`.

> LinkedIn is intentionally **not** scraped (ToS). robots.txt is respected on
> every host; requests are rate-limited per host with retry/backoff.

---

## Freshness validation (the core)

For every candidate listing (`freshness_validator.py`):

1. **Hard date** from schema.org / ATS metadata / curated date column → confidence
   `verified`.
2. **Fallback chain** when no hard date: relative string ("posted 5 days ago",
   `approximate`) → this tool's **cache first-seen** (`approximate`) →
   **`[unverified]`**. Never silently assumed fresh.
3. **Eligibility:** keep if posted within the recency window **OR** deadline within
   the lookahead window. Unverified-date listings are kept only if
   `include_unverified` is on, and are flagged + sorted into their own section.
4. **Live-check (immediately before the report):** re-request the application URL
   and classify —
   - `404`/`410`, or redirect to a generic "all jobs" page, or page text with a
     closure signal ("no longer accepting applications", "position filled", …), or
     an expired schema.org `validThrough` → **dropped as dead**;
   - reachable and open → **"verified live as of <timestamp>"**;
   - blocked/timeout (e.g. `403`) → **kept but flagged "could not verify"** (we
     only drop on a *positive* dead signal, so a bot-block never silently
     discards a good listing).
5. **Dedup** across sources by normalized company + role + location before scoring.

Bias throughout: when unsure about a date, label it unverified rather than fresh;
when unsure about liveness, flag it rather than drop or over-claim.

---

## Output

Each listing reports (Section 8 of the spec): company + one-line description,
startup flag + funding stage (best-effort), role title + level, location +
remote/hybrid/onsite, **posted date with confidence label**, deadline (or
"rolling"), **live-check status + timestamp**, source + direct apply link,
extracted requirements/skills, and match score + matched/missing skills + rationale.

The report is sorted by deadline urgency then match score, and **separates
verified-date listings from unverified-date listings** so that distinction is
never buried. A run-over-run diff ("N new, M no longer listed") is included when a
prior run exists in the cache.

Optional interactive dashboard:

```bash
streamlit run dashboard.py
```

---

## Configuration

All behavior is in `config.yaml` (every value is CLI-overridable). Key sections:
`search` (term, role keywords, locations), `freshness` (windows + live-check),
`http` (politeness), `matching` (AI provider/model/mode, min score), `sources`
(per-source enable + slugs/repos/urls), `domain` (priority keywords/sectors),
`output`. Defaults intentionally avoid preset company boards, curated repo lists,
and YC sector selectors; see the inline comments in `config.yaml`.

**Adding company boards:** open a company's careers page and read the slug from
the URL — `boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`,
`jobs.ashbyhq.com/<slug>` — then add it under the matching `sources` list. Wrong
slugs are harmless (a 404 logs and yields nothing).

---

## Implementation phases (per spec)

- **Phase 1 (done):** resume parser + Greenhouse/Lever + curated GitHub list +
  date-based recency filter + Markdown report.
- **Phase 2 (done):** Ashby + schema.org + YC + full live-check + scoring (keyword
  and Claude).
- **Phase 3 (done):** SQLite caching with run-over-run diff + Streamlit dashboard.
  *(Scheduled re-runs: drive `main.py` from Task Scheduler/cron.)*

---

## Tests

```bash
python -m unittest tests.test_core
```

Offline unit tests cover the freshness logic (date resolution, eligibility,
live-check classification, dedup), parsing, matching, caching/diff, and report
rendering.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| "No listings fetched" | For broad web search, set `SERPAPI_API_KEY`; otherwise add source slugs/repos/URLs or enable a source with public listings. Run with `-v`. |
| Few/zero results | The recency window may be tight for the current cycle — widen `--recency-days`, or check that boards have intern roles posted yet. |
| Scanned-PDF resume parses empty | Export a text-based PDF or DOCX (image-only PDFs have no extractable text). |
| AI scoring not used | Set `OPENROUTER_API_KEY` for open-weight GLM 5.2 scoring, or `ANTHROPIC_API_KEY` for Claude; use `--llm always` to see the warning reason. |
| Live-check drops too much | A site may block bots (shows as "could not verify", kept). True dead links are dropped by design. |
