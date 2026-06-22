"""schema.org JobPosting JSON-LD extractor (Tier 1, item 2).

Most modern career pages embed ``<script type="application/ld+json">`` with a
JobPosting object carrying ``datePosted`` and ``validThrough`` — the single most
reliable date source available. Point ``sources.schemaorg_urls.urls`` at any
career-page or listing URL and we parse those fields directly.

The ``parse_jobposting_jsonld`` helper is reused by the live-checker to confirm a
page still advertises an open posting.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterator, Optional

from bs4 import BeautifulSoup

from ..models import CONF_VERIFIED, Listing
from . import base

log = logging.getLogger("internfinder.sources.schemaorg")


def fetch(ctx: base.SourceContext) -> list[Listing]:
    cfg = ctx.config.get("sources", {}).get("schemaorg_urls", {})
    if not cfg.get("enabled", True):
        return []
    urls = cfg.get("urls", []) or []
    out: list[Listing] = []
    for url in urls:
        try:
            res = ctx.http.get(url)
            if not res.ok:
                log.info("schemaorg: %s -> %s", url, res.status or res.error)
                continue
            out.extend(parse_jobposting_jsonld(res.text, res.url))
        except Exception as exc:
            log.warning("schemaorg: %s failed: %s", url, exc)
    log.info("schemaorg: %d listings from %d URLs", len(out), len(urls))
    return out


def parse_jobposting_jsonld(html: str, page_url: str = "") -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    listings: list[Listing] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if "JobPosting" not in raw:
            continue
        for obj in _iter_jsonld(raw):
            if not _is_jobposting(obj):
                continue
            listing = _obj_to_listing(obj, page_url)
            if listing:
                listings.append(listing)
    return listings


def has_open_jobposting(html: str) -> Optional[bool]:
    """For the live-checker: True/False if JSON-LD asserts open/closed, else None.

    Uses ``validThrough`` (expired => closed) when present.
    """
    from datetime import date

    found = False
    for listing in parse_jobposting_jsonld(html):
        found = True
        if listing.deadline and listing.deadline < date.today():
            return False  # explicitly expired
    return True if found else None


# ----------------------------------------------------------------- internals
def _iter_jsonld(raw: str) -> Iterator[dict]:
    raw = raw.strip()
    # Some sites concatenate multiple JSON objects or wrap in CDATA.
    raw = re.sub(r"^//<!\[CDATA\[|//\]\]>$", "", raw).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return
    yield from _walk(data)


def _walk(data) -> Iterator[dict]:
    if isinstance(data, list):
        for item in data:
            yield from _walk(item)
    elif isinstance(data, dict):
        if isinstance(data.get("@graph"), list):
            for item in data["@graph"]:
                yield from _walk(item)
        yield data


def _is_jobposting(obj: dict) -> bool:
    t = obj.get("@type")
    if isinstance(t, list):
        return any("JobPosting" in str(x) for x in t)
    return "JobPosting" in str(t or "")


def _org_name(obj: dict) -> str:
    org = obj.get("hiringOrganization")
    if isinstance(org, dict):
        return org.get("name", "") or ""
    if isinstance(org, str):
        return org
    return ""


def _location(obj: dict) -> str:
    loc = obj.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, dict):
        addr = loc.get("address", loc)
        if isinstance(addr, dict):
            parts = [addr.get("addressLocality"), addr.get("addressRegion"),
                     addr.get("addressCountry")]
            parts = [p.get("name") if isinstance(p, dict) else p for p in parts]
            return ", ".join(p for p in parts if p)
    if obj.get("jobLocationType") == "TELECOMMUTE" or obj.get("applicantLocationRequirements"):
        return "Remote"
    return ""


def _obj_to_listing(obj: dict, page_url: str) -> Optional[Listing]:
    title = (obj.get("title") or "").strip()
    if not title:
        return None
    desc = base.html_to_text(obj.get("description", ""))
    if not (base.looks_like_internship(title, desc)
            or str(obj.get("employmentType", "")).upper().find("INTERN") >= 0):
        return None
    url = obj.get("url") or obj.get("directApply") or page_url
    if not isinstance(url, str):
        url = page_url
    loc = _location(obj)
    return Listing(
        company=_org_name(obj) or "(unknown)",
        title=title,
        location=loc,
        apply_url=url,
        source="schemaorg",
        description_text=desc,
        requirements=base.extract_requirements(desc),
        level=base.infer_level(title),
        work_mode=base.detect_work_mode(loc, desc),
        posted_date=base.parse_date_loose(obj.get("datePosted")),
        deadline=base.parse_date_loose(obj.get("validThrough")),
        date_confidence=CONF_VERIFIED,
        date_source="schema.org datePosted",
    )
