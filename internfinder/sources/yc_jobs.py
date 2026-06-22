"""YC 'Work at a Startup' coverage (Tier 2), hardware/robotics-biased.

workatastartup.com's full board requires login, so instead of scraping it we use
the public, static YC company dataset (yc-oss/api) to enumerate YC companies in
hardware/robotics/deep-tech industries, then probe each company's *public* ATS
board (Greenhouse/Lever/Ashby) — which we can read legitimately and which carries
reliable post dates. This surfaces real startup roles without touching a
login-walled board (Section 4 Tier 2 + Section 12).
"""

from __future__ import annotations

import logging
import re

from ..models import Listing
from . import ashby, base, greenhouse, lever

log = logging.getLogger("internfinder.sources.yc_jobs")

_YC_API = "https://yc-oss.github.io/api"


def fetch(ctx: base.SourceContext) -> list[Listing]:
    cfg = ctx.config.get("sources", {}).get("yc_jobs", {})
    if not cfg.get("enabled", True):
        return []
    industries = cfg.get("industries", []) or []
    max_companies = int(cfg.get("max_companies", 40))

    companies = _yc_companies(ctx, industries)
    if not companies:
        log.info("yc_jobs: no companies resolved from YC dataset; skipping")
        return []
    companies = companies[:max_companies]
    log.info("yc_jobs: probing ATS boards for %d hardware/deep-tech YC companies", len(companies))

    out: list[Listing] = []
    for n, comp in enumerate(companies, 1):
        found = _probe_company(ctx, comp)
        out.extend(found)
        if n % 10 == 0:
            log.info("yc_jobs: probed %d/%d companies, %d listings so far", n, len(companies), len(out))
    log.info("yc_jobs: %d internship listings", len(out))
    return out


def _yc_companies(ctx: base.SourceContext, industries: list[str]) -> list[dict]:
    seen: dict[str, dict] = {}
    for ind in industries:
        slug = re.sub(r"[^a-z0-9]+", "-", ind.lower()).strip("-")
        for kind in ("tags", "industries"):
            url = f"{_YC_API}/{kind}/{slug}.json"
            res = ctx.http.get(url)
            if not res.ok:
                continue
            try:
                data = res.json()
            except Exception:
                continue
            for c in data if isinstance(data, list) else []:
                name = c.get("name")
                if name and name not in seen:
                    seen[name] = c
            break  # found this industry under one of the kinds
    # Prefer currently-active companies.
    comps = list(seen.values())
    comps.sort(key=lambda c: 0 if str(c.get("status", "")).lower() == "active" else 1)
    return comps


def _slug_candidates(name: str) -> list[str]:
    base_slug = re.sub(r"[^a-z0-9]+", "", name.lower())
    hyphen = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    no_suffix = re.sub(r"(inc|llc|corp|labs|technologies|technology|systems)$", "", base_slug)
    cands = [base_slug, hyphen, no_suffix]
    out, seen = [], set()
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _probe_company(ctx: base.SourceContext, comp: dict) -> list[Listing]:
    name = comp.get("name", "")
    desc = comp.get("one_liner") or comp.get("long_description", "") or ""
    batch = comp.get("batch", "")
    found: list[Listing] = []
    for slug in _slug_candidates(name):
        for module, prefix in ((greenhouse, "greenhouse"), (lever, "lever"), (ashby, "ashby")):
            try:
                rows = module._fetch_board(ctx, slug)
            except Exception:
                rows = []
            if rows:
                for r in rows:
                    r.source = f"yc:{prefix}:{slug}"
                    r.company = name or r.company
                    r.is_startup = True
                    r.company_description = desc[:200]
                    r.funding_stage = f"YC {batch}" if batch else r.funding_stage
                found.extend(rows)
                return found  # first board that resolves wins for this company
    return found
