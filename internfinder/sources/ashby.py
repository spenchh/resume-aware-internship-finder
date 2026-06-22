"""Ashby boards (Tier 1).

Public posting API — no key required:
    https://api.ashbyhq.com/posting-api/job-board/<slug>?includeCompensation=true

``publishedAt`` is a hard ISO timestamp — a true post date. ``employmentType ==
"Intern"`` is a strong, structured internship signal.
"""

from __future__ import annotations

import logging

from ..models import CONF_VERIFIED, Listing
from . import base

log = logging.getLogger("internfinder.sources.ashby")

_API = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"


def fetch(ctx: base.SourceContext) -> list[Listing]:
    cfg = ctx.config.get("sources", {}).get("ashby", {})
    if not cfg.get("enabled", True):
        return []
    companies = cfg.get("companies", []) or []
    out: list[Listing] = []
    for slug in companies:
        try:
            out.extend(_fetch_board(ctx, slug))
        except Exception as exc:
            log.warning("ashby:%s failed: %s", slug, exc)
    log.info("ashby: %d internship listings from %d boards", len(out), len(companies))
    return out


def _fetch_board(ctx: base.SourceContext, slug: str) -> list[Listing]:
    res = ctx.http.get(_API.format(slug=slug))
    if not res.ok:
        if res.status == 404:
            log.info("ashby:%s — board not found (check slug)", slug)
        return []
    data = res.json()
    jobs = data.get("jobs", []) if isinstance(data, dict) else []

    company = base.slug_to_name(slug)
    listings: list[Listing] = []
    for j in jobs:
        if j.get("isListed") is False:
            continue
        title = (j.get("title") or "").strip()
        emp_type = (j.get("employmentType") or "")
        desc = j.get("descriptionPlain") or base.html_to_text(j.get("descriptionHtml", ""))
        is_intern_type = emp_type.lower() in ("intern", "internship")
        if not (is_intern_type or base.looks_like_internship(title, desc)):
            continue
        loc = j.get("location") or ""
        work_mode = "remote" if j.get("isRemote") else base.detect_work_mode(loc, desc)
        listing = Listing(
            company=company,
            title=title,
            location=loc,
            apply_url=j.get("jobUrl") or j.get("applyUrl", ""),
            source=f"ashby:{slug}",
            description_text=desc,
            requirements=base.extract_requirements(desc),
            level=base.infer_level(title) if not is_intern_type else "intern",
            work_mode=work_mode,
            posted_date=base.parse_date_loose(j.get("publishedAt")),
            date_confidence=CONF_VERIFIED,
            date_source="ashby publishedAt",
            raw={"id": j.get("id"), "department": j.get("department")},
        )
        listings.append(listing)
    return listings
