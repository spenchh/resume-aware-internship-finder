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
_DEFAULT_STARTUP_QUERY_TERMS = [
    "startup internship",
    "early stage startup internship",
    "small startup internship",
    "venture backed startup internship",
]
_WORK_MODE_QUERY_TERMS = {
    "remote": "remote",
    "hybrid": "hybrid",
    "onsite": "on-site",
    "on-site": "on-site",
    "in_person": "on-site",
    "in-person": "on-site",
}


def fetch(ctx: base.SourceContext) -> list[Listing]:
    cfg = ctx.config.get("sources", {}).get("serpapi_google_jobs", {})
    if not cfg.get("enabled", True):
        return []
    api_key = os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        log.info("serpapi: SERPAPI_API_KEY unset — skipping Google Jobs source")
        return []

    search = ctx.config.get("search", {})
    queries = _build_queries(ctx.config)
    locations = _configured_locations(search, cfg)
    max_total = int(cfg.get("max_results", 40))
    per_query = int(
        cfg.get("max_results_per_query")
        or max(1, (max_total + (len(queries) * len(locations)) - 1) // (len(queries) * len(locations)))
    )

    out: list[Listing] = []
    for loc in locations:
        for query, startup_query in queries:
            remaining = max_total - len(out)
            if remaining <= 0:
                break
            out.extend(_search(ctx, api_key, query, loc, min(per_query, remaining), startup_query=startup_query))
    log.info("serpapi: %d listings from %d queries", len(out), len(queries))
    return out


def _build_queries(config: dict) -> list[tuple[str, bool]]:
    search = config.get("search", {})
    source_cfg = config.get("sources", {}).get("serpapi_google_jobs", {})
    term = (search.get("term", "") or "").strip()
    target_role = (search.get("target_role", "") or "").strip()

    # Field-agnostic query: the candidate's stated target role/field drives the
    # search if given; otherwise fall back to configured priority keywords. This
    # is the broadest, web-wide source, so it should reflect what the user wants.
    if target_role:
        focus = [t.strip() for t in target_role.replace("/", ",").split(",") if t.strip()][:3]
    else:
        focus = [
            str(t).strip()
            for t in config.get("domain", {}).get("priority_keywords", [])[:4]
            if str(t).strip()
        ]

    work_term = _WORK_MODE_QUERY_TERMS.get(str(search.get("remote_preference", "any")).lower().strip(), "")
    suffix = [p for p in (term, work_term) if p]
    queries: list[tuple[str, bool]] = []
    seen: set[str] = set()

    def add(parts: list[str], startup_query: bool) -> None:
        query = " ".join(p for p in parts if p).strip()
        if not query:
            return
        key = query.lower()
        if key in seen:
            return
        seen.add(key)
        queries.append((query, startup_query))

    add([*focus, "internship", *suffix], False)

    if source_cfg.get("startup_breadth", False):
        startup_terms = source_cfg.get("startup_query_terms") or _DEFAULT_STARTUP_QUERY_TERMS
        for phrase in startup_terms:
            phrase = str(phrase).strip()
            if phrase:
                add([*focus, phrase, *suffix], True)

    if not queries:
        add(["internship", *suffix], False)
    return queries


def _configured_locations(search: dict, cfg: dict) -> list[str]:
    locations = search.get("locations", []) or ["United States"]
    max_locations = int(cfg.get("max_locations", 2))
    out = [str(loc).strip() for loc in locations if str(loc).strip()][:max_locations]
    return out or ["United States"]


def _search(ctx, api_key, query, location, max_results, *, startup_query=False) -> list[Listing]:
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
        via = job.get("via", "google_jobs")
        out.append(
            Listing(
                company=job.get("company_name", "(unknown)"),
                title=title,
                location=loc,
                apply_url=apply_url,
                source=f"serpapi:{'startup:' if startup_query else ''}{via}",
                description_text=desc,
                company_description=desc[:240],
                is_startup=True if startup_query else None,
                requirements=base.extract_requirements(desc),
                level=base.infer_level(title),
                work_mode=base.detect_work_mode(loc, desc),
                posted_date=posted,
                date_confidence=conf,
                date_source=src,
                raw={
                    "serpapi_query": query,
                    "serpapi_location": location,
                    "startup_query": startup_query,
                },
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
