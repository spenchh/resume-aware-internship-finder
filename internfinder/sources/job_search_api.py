"""General aggregator via SerpAPI Google Jobs (Tier 3 — breadth, lower trust).

Google Jobs exposes only relative "posted X days ago" strings, so dates here are
always labeled *approximate* (Section 4 Tier 3). Requires SERPAPI_API_KEY; if
unset, the source self-skips with a logged note.
"""

from __future__ import annotations

import logging
import os

from ..models import CONF_APPROXIMATE, CONF_UNVERIFIED, Listing
from . import base

log = logging.getLogger("internfinder.sources.job_search_api")

_ENDPOINT = "https://serpapi.com/search.json"


def fetch(ctx: base.SourceContext) -> list[Listing]:
    cfg = ctx.config.get("sources", {}).get("serpapi_google_jobs", {})
    if not cfg.get("enabled", True):
        return []
    api_key = os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        log.info("serpapi: SERPAPI_API_KEY unset — skipping Google Jobs source")
        return []

    search = ctx.config.get("search", {})
    term = search.get("term", "")
    # Field-agnostic query: the candidate's stated target role/field drives the
    # search if given; otherwise fall back to configured priority keywords. This
    # is the broadest, web-wide source, so it should reflect what the user wants.
    target_role = (search.get("target_role", "") or "").strip()
    if target_role:
        focus = [t.strip() for t in target_role.replace("/", ",").split(",") if t.strip()][:3]
    else:
        focus = ctx.config.get("domain", {}).get("priority_keywords", [])[:4]
    query = " ".join([*focus, "internship", term]).strip()

    locations = ctx.config.get("search", {}).get("locations", []) or ["United States"]
    out: list[Listing] = []
    for loc in locations[:2]:
        out.extend(_search(ctx, api_key, query, loc, int(cfg.get("max_results", 40))))
    log.info("serpapi: %d listings", len(out))
    return out


def _search(ctx, api_key, query, location, max_results) -> list[Listing]:
    params = {
        "engine": "google_jobs",
        "q": query,
        "location": location,
        "api_key": api_key,
        "hl": "en",
    }
    try:
        res = ctx.http.get(_ENDPOINT, params=params, obey_robots=False)
        if not res.ok:
            log.warning("serpapi: HTTP %s", res.status or res.error)
            return []
        data = res.json()
    except Exception as exc:
        log.warning("serpapi: request failed: %s", exc)
        return []

    out: list[Listing] = []
    for job in (data.get("jobs_results") or [])[:max_results]:
        title = (job.get("title") or "").strip()
        desc = job.get("description", "") or ""
        if not base.looks_like_internship(title, desc):
            continue
        ext = job.get("detected_extensions", {}) or {}
        posted, conf, src = _posted(ext)
        apply_url = _apply_link(job)
        loc = job.get("location", "") or location
        out.append(
            Listing(
                company=job.get("company_name", "(unknown)"),
                title=title,
                location=loc,
                apply_url=apply_url,
                source=f"serpapi:{job.get('via', 'google_jobs')}",
                description_text=desc,
                requirements=base.extract_requirements(desc),
                level=base.infer_level(title),
                work_mode=base.detect_work_mode(loc, desc),
                posted_date=posted,
                date_confidence=conf,
                date_source=src,
            )
        )
    return out


def _posted(ext: dict):
    posted_at = ext.get("posted_at")  # e.g. "3 days ago"
    if posted_at:
        d, _ = base.parse_relative_date(posted_at)
        if d:
            return d, CONF_APPROXIMATE, f"google jobs '{posted_at}'"
    return None, CONF_UNVERIFIED, ""


def _apply_link(job: dict) -> str:
    for opt in job.get("apply_options", []) or []:
        link = opt.get("link")
        if link:
            return link
    return job.get("share_link", "") or ""
