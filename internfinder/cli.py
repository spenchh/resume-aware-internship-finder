"""CLI orchestration: parse -> fetch -> resolve dates -> dedupe -> eligibility
-> live-check -> score -> report (spec Sections 5 & 6)."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import freshness_validator as freshness
from . import matcher, report_generator
from .cache import Cache
from .config import apply_overrides, load_config, load_env
from .http_util import HttpClient
from .models import LIVE_DEAD, LIVE_SKIPPED
from .resume_parser import parse_resume
from .sources import (
    ashby,
    base,
    github_lists,
    greenhouse,
    job_search_api,
    lever,
    schemaorg,
    wellfound,
    yc_jobs,
)

log = logging.getLogger("internfinder")

# (label, fetch). Each self-skips when disabled in config.
SOURCES = [
    ("greenhouse", greenhouse.fetch),
    ("lever", lever.fetch),
    ("ashby", ashby.fetch),
    ("schemaorg", schemaorg.fetch),
    ("github_lists", github_lists.fetch),
    ("yc_jobs", yc_jobs.fetch),
    ("wellfound", wellfound.fetch),
    ("serpapi", job_search_api.fetch),
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="internfinder",
        description="Resume-aware, freshness-validated internship finder "
                    "(field-agnostic, startup-aware).",
    )
    p.add_argument("--resume", "-r", required=True, help="Path to resume (PDF/DOCX/TXT).")
    p.add_argument("--config", "-c", default="config.yaml", help="Config YAML path.")
    p.add_argument("--term", help="Override target term, e.g. 'Summer 2027'.")
    p.add_argument("--recency-days", type=int, help="Posted-within window (default 21).")
    p.add_argument("--deadline-days", type=int, help="Deadline lookahead window (default 14).")
    p.add_argument("--output", "-o", help="Output directory (default reports/).")
    p.add_argument("--format", choices=["markdown", "html", "both"], help="Report format.")
    p.add_argument("--no-live-check", action="store_true",
                   help="Skip the live URL re-check (faster, but freshness is weaker).")
    p.add_argument("--llm", choices=["auto", "always", "never"],
                   help="LLM scoring mode (default auto: use Claude if ANTHROPIC_API_KEY set).")
    p.add_argument("--max-llm", type=int, help="Cap number of listings sent to the LLM.")
    p.add_argument("--cache", default="cache.db", help="SQLite cache path.")
    p.add_argument("--sources", help="Comma list to restrict sources (e.g. greenhouse,lever).")
    p.add_argument("--verbose", "-v", action="store_true", help="Debug logging.")
    return p


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy libraries.
    for noisy in ("urllib3", "requests", "pdfminer", "anthropic", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _overrides_from_args(args) -> dict:
    ov = {
        "search.term": args.term,
        "freshness.recency_days": args.recency_days,
        "freshness.deadline_lookahead_days": args.deadline_days,
        "output.directory": args.output,
        "output.format": args.format,
        "matching.use_llm": args.llm,
        "matching.llm_max_listings": args.max_llm,
    }
    if args.no_live_check:
        ov["freshness.live_check"] = False
    return ov


@dataclass
class PipelineResult:
    """Everything a caller (CLI or web UI) needs after a finder run."""

    reported: list          # list[Listing] above the score threshold, sorted
    profile: object         # ResumeProfile used for matching
    new_keys: list          # dedup keys new since the previous run
    closed_keys: list       # dedup keys no longer listed since the previous run
    total_fetched: int      # raw listings pulled before dedup/eligibility
    seconds: float          # wall-clock duration


# Type of an optional progress reporter: progress(message, fraction_0_to_1_or_None).
ProgressFn = Callable[[str, Optional[float]], None]


def run_pipeline(
    resume_path: str | Path,
    config: dict,
    *,
    cache_path: str = "cache.db",
    sources: str | None = None,
    progress: ProgressFn | None = None,
) -> PipelineResult:
    """Run the full finder pipeline on one resume and return the results.

    This is the single source of truth for both the CLI (``run`` below) and the
    Streamlit web app (``streamlit_app.py``). It performs no printing and writes
    no report files — the caller decides how to present ``PipelineResult``.
    """

    def _p(msg: str, frac: float | None = None) -> None:
        log.info(msg)
        if progress is not None:
            try:
                progress(msg, frac)
            except Exception:  # pragma: no cover - UI callback must never crash the run
                pass

    t0 = time.monotonic()
    _p(f"Parsing resume: {Path(resume_path).name}", 0.04)
    profile = parse_resume(resume_path, config)

    http = HttpClient(config.get("http", {}))
    role_keywords = list(dict.fromkeys(
        [k.lower() for k in config.get("search", {}).get("role_keywords", [])]
        + profile.tools_languages[:6]
    ))
    ctx = base.SourceContext(http=http, config=config, role_keywords=role_keywords)

    # --- 1. Fetch ---------------------------------------------------------
    enabled = None
    if sources:
        parts = sources.split(",") if isinstance(sources, str) else sources
        enabled = {s.strip() for s in parts if str(s).strip()}
    listings = []
    n_src = len(SOURCES)
    for i, (label, fetch) in enumerate(SOURCES):
        if enabled is not None and label not in enabled:
            continue
        _p(f"Searching {label}…", 0.08 + 0.40 * i / n_src)
        try:
            listings.extend(fetch(ctx))
        except Exception as exc:
            log.warning("source %s crashed: %s", label, exc)
    _p(f"Fetched {len(listings)} raw listings from sources", 0.50)
    if not listings:
        log.warning("No listings fetched. Check source slugs in config.yaml and your connection.")

    cache = Cache(cache_path)
    run_id = cache.start_run()

    # --- 2. Observe (first-seen) + resolve dates -------------------------
    for l in listings:
        cache.observe(l)             # sets l.first_seen
        freshness.resolve_date(l)    # hard -> relative -> first-seen -> [unverified]

    # --- 3. Dedupe (before scoring, Section 6.5) -------------------------
    listings = freshness.dedupe(listings)

    # --- 4. Eligibility windows ------------------------------------------
    eligible = [l for l in listings if freshness.mark_eligibility(l, config)]
    _p(f"{len(eligible)} of {len(listings)} listings within recency/deadline windows", 0.55)

    # --- 5. Live-check (immediately before reporting, Section 6.4) -------
    do_live = bool(config.get("freshness", {}).get("live_check", True))
    if do_live:
        n_elig = max(len(eligible), 1)
        dead = 0
        for i, l in enumerate(eligible, 1):
            if i == 1 or i % 5 == 0 or i == len(eligible):
                _p(f"Live-checking listings {i}/{len(eligible)} (verifying still open)…",
                   0.55 + 0.33 * i / n_elig)
            freshness.live_check(l, http, config)
            cache.record_verification(l.cache_key(), l.live_status, l.live_checked_at)
            if l.live_status == LIVE_DEAD:
                dead += 1
        retained = [l for l in eligible if l.live_status != LIVE_DEAD]
        log.info("Live-check: dropped %d dead, %d retained", dead, len(retained))
    else:
        for l in eligible:
            l.live_status = LIVE_SKIPPED
        retained = eligible
        log.info("Live-check disabled — %d listings retained unverified-live", len(retained))

    # --- 6. Score --------------------------------------------------------
    _p(f"Scoring {len(retained)} listings against your resume…", 0.90)
    matcher.score_listings(profile, retained, config)
    min_score = int(config.get("matching", {}).get("min_score_to_report", 0))
    reported = [l for l in retained if l.match_score >= min_score]
    reported.sort(key=lambda x: -x.match_score)
    log.info("Scoring: %d/%d listings scored >= %d", len(reported), len(retained), min_score)

    # --- 7. Cache run records + diff ------------------------------------
    reported_set = set(id(x) for x in reported)
    for l in retained:
        cache.record_run_listing(run_id, l, reported=id(l) in reported_set)
    new_keys, closed_keys = cache.diff_since_last_run(run_id, reported)
    cache.finish_run(run_id, len(listings), len(reported))
    cache.close()

    seconds = time.monotonic() - t0
    _p(f"Done — {len(reported)} listings reported", 1.0)
    return PipelineResult(
        reported=reported,
        profile=profile,
        new_keys=new_keys,
        closed_keys=closed_keys,
        total_fetched=len(listings),
        seconds=seconds,
    )


def run(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    load_env()

    if not Path(args.resume).exists():
        log.error("Resume not found: %s", args.resume)
        return 2

    config = load_config(args.config)
    apply_overrides(config, _overrides_from_args(args))

    result = run_pipeline(args.resume, config, cache_path=args.cache, sources=args.sources)

    paths = report_generator.generate(
        result.reported, result.profile, config,
        diff=(result.new_keys, result.closed_keys),
    )

    print()
    print(f"Done in {result.seconds:.1f}s — {len(result.reported)} listings reported "
          f"({len(result.new_keys)} new, {len(result.closed_keys)} no longer listed since last run).")
    for fmt, path in paths.items():
        print(f"  {fmt:8} -> {path}")
    if not paths:
        print("  (no report written — check output.format in config)")
    return 0


def main(argv=None) -> int:
    try:
        return run(argv)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
