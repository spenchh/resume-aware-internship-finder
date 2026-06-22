"""Lever boards (Tier 1).

Public JSON postings API — no key required:
    https://api.lever.co/v0/postings/<slug>?mode=json

``createdAt`` is a hard epoch-millisecond timestamp — a true post date.
"""

from __future__ import annotations

import logging

from ..models import CONF_VERIFIED, Listing
from . import base

log = logging.getLogger("internfinder.sources.lever")

_API = "https://api.lever.co/v0/postings/{slug}?mode=json"


def fetch(ctx: base.SourceContext) -> list[Listing]:
    cfg = ctx.config.get("sources", {}).get("lever", {})
    if not cfg.get("enabled", True):
        return []
    companies = cfg.get("companies", []) or []
    out: list[Listing] = []
    for slug in companies:
        try:
            out.extend(_fetch_board(ctx, slug))
        except Exception as exc:
            log.warning("lever:%s failed: %s", slug, exc)
    log.info("lever: %d internship listings from %d boards", len(out), len(companies))
    return out


def _fetch_board(ctx: base.SourceContext, slug: str) -> list[Listing]:
    res = ctx.http.get(_API.format(slug=slug))
    if not res.ok:
        if res.status == 404:
            log.info("lever:%s — board not found (check slug)", slug)
        return []
    postings = res.json()
    if not isinstance(postings, list):
        return []

    company = base.slug_to_name(slug)
    listings: list[Listing] = []
    for p in postings:
        title = (p.get("text") or "").strip()
        desc = p.get("descriptionPlain") or base.html_to_text(p.get("description", ""))
        cats = p.get("categories") or {}
        commitment = cats.get("commitment", "") or ""
        if not base.looks_like_internship(f"{title} {commitment}", desc):
            continue
        loc = cats.get("location", "") or ""
        work_mode = (p.get("workplaceType") or base.detect_work_mode(loc, desc, commitment)).lower()
        listing = Listing(
            company=company,
            title=title,
            location=loc,
            apply_url=p.get("hostedUrl") or p.get("applyUrl", ""),
            source=f"lever:{slug}",
            description_text=desc,
            requirements=base.extract_requirements(desc),
            level=base.infer_level(f"{title} {commitment}"),
            work_mode=work_mode if work_mode in ("remote", "hybrid", "onsite") else "unknown",
            posted_date=base.parse_date_loose(p.get("createdAt")),
            date_confidence=CONF_VERIFIED,
            date_source="lever createdAt",
            raw={"id": p.get("id"), "team": cats.get("team")},
        )
        listings.append(listing)
    return listings
