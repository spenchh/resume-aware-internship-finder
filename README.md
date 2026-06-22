# Resume-Aware Internship Finder

Takes your resume, searches multiple **date-reliable** sources for internships
that match your background, **verifies every listing is actually still live before
reporting it**, scores the matches, and emits a detailed Markdown/HTML report.

Built hardware/embedded-first (FPGA, RTL, ASIC, firmware, power electronics,
robotics, DSP/RF) but works for any field — the domain weighting is just config.

> **Design priority:** freshness. A listing only appears if it has a determinable
> recent posted date **or** an upcoming deadline, **and** its application URL
> passes a live re-check in the same run that generates the report. When a date
> can't be verified, it's labeled `[unverified]` — never silently presented as
> fresh. See [Freshness validation](#freshness-validation-the-core).

---

## 🌐 Use it on the web (no install — just upload your resume)

The entire tool runs in the browser via **`streamlit_app.py`**: upload a resume,
click **Find internships**, and it parses, searches, live-verifies, scores, and
lists the results — with filters and downloadable reports. Nothing to install for
whoever uses it.

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
   ANTHROPIC_API_KEY = "sk-ant-..."   # enables Claude AI match scoring
   SERPAPI_API_KEY   = "..."          # enables Google Jobs source
   GITHUB_TOKEN      = "..."          # higher rate limit for curated GitHub lists
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
pip install -r requirements-optional.txt      # Claude scoring + Streamlit dashboard
copy .env.example .env                         # then fill in keys you have
```

- `ANTHROPIC_API_KEY` → enables Claude-based match scoring (otherwise weighted
  keyword scoring is used).
- `SERPAPI_API_KEY` → enables the Google Jobs breadth source.
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
  --llm              auto | always | never  (LLM scoring mode)
  --max-llm          Cap listings sent to the LLM
  --cache            SQLite cache path (default: cache.db)
  --sources          Restrict sources, e.g. greenhouse,lever,ashby
  --verbose, -v      Debug logging
```

Examples:

```bash
# Tighter freshness, HTML report
python main.py -r resume.pdf --recency-days 14 --deadline-days 7 --format html

# Only the most date-reliable sources, with Claude scoring forced on
python main.py -r resume.pdf --sources greenhouse,lever,ashby,schemaorg --llm always

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
 matcher.score_listings (keyword overlap, or Claude) ─▶ report_generator ─▶ report
```

| Module | Responsibility |
|---|---|
| `internfinder/resume_parser.py` | Extract skills/tools/coursework/degree/titles; build weighted keyword map with hardware synonym expansion (Verilog→RTL/HDL/digital design). |
| `internfinder/domain.py` | Hardware/embedded lexicon + synonym graph. |
| `internfinder/sources/*` | One fetcher per source. Each fails soft. |
| `internfinder/freshness_validator.py` | **The core** — date resolution, eligibility windows, live-check, dedup. |
| `internfinder/matcher.py` | 0–100 match scoring (keyword or Claude). |
| `internfinder/report_generator.py` | Markdown/HTML report, verified vs unverified sections. |
| `internfinder/cache.py` | SQLite first-seen tracking + run-over-run diff. |
| `internfinder/cli.py` | Orchestration + CLI. |

---

## Data sources (ranked by date-reliability)

**Tier 1 — most reliable (build on these):**
- **Greenhouse / Lever / Ashby** public JSON board APIs. Structured post dates
  (`updated_at` / `createdAt` / `publishedAt`); a closed role 404s or disappears,
  which makes freshness trivial to confirm. Configure company slugs in
  `config.yaml` — the defaults are *verified-live hardware/deep-tech* boards
  (Anduril, Figure, Relativity, Tenstorrent, Lightmatter, Neuralink, Shield AI,
  Zoox, Physical Intelligence, 1X, Etched, …).
- **schema.org JobPosting JSON-LD** — point `schemaorg_urls` at any career page;
  `datePosted`/`validThrough` are the most reliable date fields available.
- **Curated GitHub tracking repos** — README tables where maintainers mark
  closures. The repo's last-commit date is checked first; a stale repo is skipped.

**Tier 2 — startup-specific:** YC (`yc_jobs`, hardware/robotics-filtered via the
public YC dataset, then probes each company's ATS board), Wellfound (off by
default; ToS-respecting user-export only).

**Tier 3 — breadth, lower trust:** SerpAPI Google Jobs (relative dates only,
always labeled *approximate*; auto-skips without a key).

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
extracted tech stack, and match score + matched/missing skills + rationale.

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
`http` (politeness), `matching` (LLM model/mode, min score), `sources`
(per-source enable + slugs/repos/urls), `domain` (priority keywords/sectors),
`output`. See the inline comments in `config.yaml`.

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
| "No listings fetched" | Check `--sources`, verify slugs in `config.yaml`, confirm connectivity. Run with `-v`. |
| Few/zero results | The recency window may be tight for the current cycle — widen `--recency-days`, or check that boards have intern roles posted yet. |
| Scanned-PDF resume parses empty | Export a text-based PDF or DOCX (image-only PDFs have no extractable text). |
| LLM scoring not used | Set `ANTHROPIC_API_KEY` and `pip install anthropic`; or `--llm always` to see the warning reason. |
| Live-check drops too much | A site may block bots (shows as "could not verify", kept). True dead links are dropped by design. |
