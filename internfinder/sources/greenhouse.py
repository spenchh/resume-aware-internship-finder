"""Greenhouse boards (Tier 1).

Public JSON board API — no key required:
    https://boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true

``updated_at`` is a hard ISO timestamp (labeled honestly as an update, not a
strict post date). A closed role 404s / disappears from the board, which makes
freshness trivial to verify downstream.
"""

from __future__ import annotations

import logging

from ..models import CONF_VERIFIED, Listing
from . import base

log = logging.getLogger("internfinder.sources.greenhouse")

_API = "https://boards-api.greenhouse.io/v1/boards/{slug}"


def fetch(ctx: base.SourceContext) -> list[Listing]:
    cfg = ctx.config.get("sources", {}).get("greenhouse", {})
    if not cfg.get("enabled", True):
        return []
    companies = cfg.get("companies", []) or []
    out: list[Listing] = []
    for slug in companies:
        try:
            out.extend(_fetch_board(ctx, slug))
        except Exception as exc:  # fail soft per board
            log.warning("greenhouse:%s failed: %s", slug, exc)
    log.info("greenhouse: %d internship listings from %d boards", len(out), len(companies))
    return out


def _fetch_board(ctx: base.SourceContext, slug: str) -> list[Listing]:
    base_url = _API.format(slug=slug)
    # Company display name (one cheap call; tolerate failure).
    company = base.slug_to_name(slug)
    meta = ctx.http.get(base_url)
    if meta.ok:
        try:
            company = meta.json().get("name") or company
        except Exception:
            pass

    res = ctx.http.get(base_url + "/jobs?content=true")
    if not res.ok:
        if res.status == 404:
            log.info("greenhouse:%s — board not found (check slug)", slug)
        return []
    jobs = res.json().get("jobs", []) or []

    listings: list[Listing] = []
    for job in jobs:
        title = (job.get("title") or "").strip()
        content = base.html_to_text(job.get("content", ""))
        if not base.looks_like_internship(title, content):
            continue
        loc = (job.get("location") or {}).get("name", "")
        listing = Listing(
            company=company,
            title=title,
            location=loc,
            apply_url=job.get("absolute_url", ""),
            source=f"greenhouse:{slug}",
            description_text=content,
            requirements=base.extract_requirements(content),
            level=base.infer_level(title),
            work_mode=base.detect_work_mode(loc, content),
            posted_date=base.parse_date_loose(job.get("updated_at")),
            date_confidence=CONF_VERIFIED,
            date_source="greenhouse updated_at",
            raw={"id": job.get("id")},
        )
        listings.append(listing)
    return listings
