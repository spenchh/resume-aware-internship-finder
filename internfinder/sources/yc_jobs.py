"""YC startup coverage (Tier 2).

Work at a Startup's full board requires login, so instead of scraping it we use
the public, static YC company dataset (yc-oss/api) to enumerate companies
broadly, rank toward active/hiring/small/recent startups, then check public YC
profile jobs and public ATS boards (Greenhouse/Lever/Ashby). ATS boards carry
reliable posting dates and disappear when roles close.
"""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from ..models import CONF_APPROXIMATE, CONF_UNVERIFIED, Listing
from . import ashby, base, greenhouse, lever

log = logging.getLogger("internfinder.sources.yc_jobs")

_YC_API = "https://yc-oss.github.io/api"
_YC_ALL = f"{_YC_API}/companies/all.json"


def fetch(ctx: base.SourceContext) -> list[Listing]:
    cfg = ctx.config.get("sources", {}).get("yc_jobs", {})
    if not cfg.get("enabled", True):
        return []

    selectors = cfg.get("selectors") or cfg.get("industries", []) or []
    max_companies = int(cfg.get("max_companies", 40))

    companies = _yc_companies(ctx, selectors)
    companies = _filter_and_rank_companies(companies, cfg)
    if not companies:
        log.info("yc_jobs: no companies resolved from YC dataset; skipping")
        return []

    companies = companies[:max_companies]
    log.info("yc_jobs: checking public YC profiles/ATS boards for %d active small/recent companies", len(companies))

    out: list[Listing] = []
    for n, comp in enumerate(companies, 1):
        found = _probe_company(ctx, comp)
        out.extend(found)
        if n % 10 == 0:
            log.info("yc_jobs: probed %d/%d companies, %d listings so far", n, len(companies), len(out))
    log.info("yc_jobs: %d internship listings", len(out))
    return out


def _yc_companies(ctx: base.SourceContext, selectors: list[str]) -> list[dict]:
    if not selectors:
        res = ctx.http.get(_YC_ALL)
        if not res.ok:
            return []
        try:
            data = res.json()
        except Exception:
            return []
        return data if isinstance(data, list) else []

    seen: dict[str, dict] = {}
    for selector in selectors:
        slug = re.sub(r"[^a-z0-9]+", "-", str(selector).lower()).strip("-")
        for kind in ("tags", "industries"):
            url = f"{_YC_API}/{kind}/{slug}.json"
            res = ctx.http.get(url)
            if not res.ok:
                continue
            try:
                data = res.json()
            except Exception:
                continue
            for company in data if isinstance(data, list) else []:
                key = str(company.get("id") or company.get("slug") or company.get("name") or "")
                if key and key not in seen:
                    seen[key] = company
            break
    return list(seen.values())


def _int_or_none(value) -> int | None:
    try:
        if value in ("", None):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _batch_year(company: dict) -> int | None:
    match = re.search(r"(20\d{2})", str(company.get("batch", "")))
    return int(match.group(1)) if match else None


def _filter_and_rank_companies(companies: list[dict], cfg: dict) -> list[dict]:
    """Prefer active, small, recent YC companies before probing ATS boards."""
    active_only = bool(cfg.get("active_only", True))
    hiring_only = bool(cfg.get("hiring_only", True))
    small_team_max = _int_or_none(cfg.get("small_team_max"))
    recent_batch_year_min = _int_or_none(cfg.get("recent_batch_year_min"))
    require_known_team_size = bool(cfg.get("require_known_team_size", False))

    filtered: list[dict] = []
    for company in companies:
        status = str(company.get("status", "")).lower()
        if active_only and status != "active":
            continue
        if hiring_only and not bool(company.get("isHiring")):
            continue

        team_size = _int_or_none(company.get("team_size"))
        if require_known_team_size and team_size is None:
            continue
        if small_team_max is not None and team_size is not None and team_size > small_team_max:
            continue

        year = _batch_year(company)
        if recent_batch_year_min is not None and year is not None and year < recent_batch_year_min:
            continue

        filtered.append(company)

    filtered.sort(key=lambda c: _company_rank(c, small_team_max, recent_batch_year_min))
    return filtered


def _company_rank(
    company: dict,
    small_team_max: int | None,
    recent_batch_year_min: int | None,
) -> tuple:
    team_size = _int_or_none(company.get("team_size"))
    year = _batch_year(company)
    has_small_known_team = team_size is not None and (
        small_team_max is None or team_size <= small_team_max
    )
    has_recent_known_batch = year is not None and (
        recent_batch_year_min is None or year >= recent_batch_year_min
    )
    return (
        0 if bool(company.get("isHiring")) else 1,
        0 if has_small_known_team else 1,
        0 if has_recent_known_batch else 1,
        -(year or 0),
        team_size if team_size is not None else 10**6,
        str(company.get("name", "")).lower(),
    )


def _website_slug(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.netloc or parsed.path).lower().split(":", 1)[0]
    labels = [p for p in host.split(".") if p and p not in {"www", "jobs", "careers"}]
    if len(labels) >= 2:
        return re.sub(r"[^a-z0-9]+", "", labels[-2])
    if labels:
        return re.sub(r"[^a-z0-9]+", "", labels[0])
    return ""


def _slug_candidates(company: str | dict) -> list[str]:
    comp = company if isinstance(company, dict) else {"name": company}
    name = str(comp.get("name", ""))
    base_slug = re.sub(r"[^a-z0-9]+", "", name.lower())
    hyphen = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    no_suffix = re.sub(
        r"(inc|llc|corp|labs|technologies|technology|systems)$",
        "",
        base_slug,
    )
    yc_slug = re.sub(r"[^a-z0-9-]+", "", str(comp.get("slug", "")).lower())
    website_slug = _website_slug(str(comp.get("website", "")))
    candidates = [yc_slug, base_slug, hyphen, no_suffix, website_slug]

    out, seen = [], set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def _probe_company(ctx: base.SourceContext, comp: dict) -> list[Listing]:
    cfg = ctx.config.get("sources", {}).get("yc_jobs", {})
    profile_first = bool(cfg.get("profile_first", True))
    skip_ats_when_profile_has_jobs = bool(cfg.get("skip_ats_when_profile_has_jobs", True))

    if profile_first:
        profile_rows, had_profile_jobs = _profile_jobs(ctx, comp)
        if profile_rows or (had_profile_jobs and skip_ats_when_profile_has_jobs):
            return profile_rows

    name = comp.get("name", "")
    desc = comp.get("one_liner") or comp.get("long_description", "") or ""
    batch = comp.get("batch", "")
    team_size = _int_or_none(comp.get("team_size"))
    found: list[Listing] = []

    for slug in _slug_candidates(comp):
        for module, prefix in ((greenhouse, "greenhouse"), (lever, "lever"), (ashby, "ashby")):
            try:
                rows = module._fetch_board(ctx, slug)
            except Exception:
                rows = []
            if rows:
                for row in rows:
                    row.source = f"yc:{prefix}:{slug}"
                    row.company = name or row.company
                    row.is_startup = True
                    row.company_description = desc[:200]
                    stage_bits = [f"YC {batch}"] if batch else []
                    if team_size:
                        stage_bits.append(f"{team_size}-person team")
                    row.funding_stage = " · ".join(stage_bits) or row.funding_stage
                    row.raw["yc_company_id"] = comp.get("id")
                    row.raw["yc_team_size"] = team_size
                    row.raw["yc_tags"] = comp.get("tags", [])
                found.extend(rows)
                return found
    return _fetch_profile_jobs(ctx, comp) if not profile_first else []


def _fetch_profile_jobs(ctx: base.SourceContext, comp: dict) -> list[Listing]:
    rows, _had_jobs = _profile_jobs(ctx, comp)
    return rows


def _profile_jobs(ctx: base.SourceContext, comp: dict) -> tuple[list[Listing], bool]:
    slug = str(comp.get("slug", "")).strip()
    url = comp.get("url") or (f"https://www.ycombinator.com/companies/{slug}" if slug else "")
    if not url:
        return [], False

    res = ctx.http.get(url)
    if not res.ok:
        return [], False

    data = _extract_profile_payload(res.text or "")
    props = data.get("props", {}) if isinstance(data, dict) else {}
    jobs = props.get("jobPostings") or []
    if not isinstance(jobs, list):
        return [], False

    company = props.get("company") if isinstance(props.get("company"), dict) else {}
    company_name = company.get("name") or comp.get("name") or base.slug_to_name(slug)
    company_desc = (
        company.get("one_liner")
        or comp.get("one_liner")
        or company.get("long_description")
        or comp.get("long_description")
        or ""
    )
    team_size = _int_or_none(company.get("team_size") or comp.get("team_size"))
    batch = company.get("batch_name") or company.get("batch") or comp.get("batch") or ""

    listings: list[Listing] = []
    for job in jobs:
        if not isinstance(job, dict) or job.get("isIncomplete"):
            continue
        title = (job.get("title") or "").strip()
        location = job.get("location") or ""
        skills = [str(s) for s in (job.get("skills") or []) if s]
        job_text = " ".join(
            str(x)
            for x in (
                title,
                job.get("type"),
                job.get("prettyRole"),
                job.get("roleSpecificType"),
                job.get("minExperience"),
                company_desc,
                " ".join(skills),
            )
            if x
        )
        if not base.looks_like_internship(title, job_text):
            continue

        posted_date, phrase = _parse_yc_relative_date(job.get("createdAt"))
        stage_bits = [f"YC {batch}"] if batch else []
        if team_size:
            stage_bits.append(f"{team_size}-person team")
        job_url = job.get("url") or url
        if str(job_url).startswith("/"):
            job_url = "https://www.ycombinator.com" + str(job_url)

        listing = Listing(
            company=company_name,
            title=title,
            location=location,
            apply_url=str(job_url),
            source=f"yc:profile:{slug or comp.get('id', '')}",
            company_description=str(company_desc)[:200],
            is_startup=True,
            funding_stage=" · ".join(stage_bits),
            description_text=job_text,
            requirements=skills or base.extract_requirements(job_text),
            level=base.infer_level(f"{title} {job.get('minExperience') or ''}"),
            work_mode=base.detect_work_mode(location, job_text),
            posted_date=posted_date,
            date_confidence=CONF_APPROXIMATE if posted_date else CONF_UNVERIFIED,
            date_source=f"YC job created {phrase}" if phrase else "YC public profile",
            raw={
                "id": job.get("id"),
                "yc_company_id": company.get("id") or comp.get("id"),
                "yc_team_size": team_size,
                "yc_tags": company.get("tags") or comp.get("tags", []),
                "yc_apply_url": job.get("applyUrl", ""),
            },
        )
        listings.append(listing)
    return listings, bool(jobs)


def _extract_profile_payload(body: str) -> dict:
    match = re.search(r'data-page="([^"]+)"', body or "")
    if not match:
        return {}
    try:
        return json.loads(html.unescape(match.group(1)))
    except Exception:
        return {}


def _parse_yc_relative_date(value) -> tuple:
    text = str(value or "").strip().lower()
    if not text:
        return None, ""
    now = datetime.now(timezone.utc).date()
    if text in {"today", "just posted"}:
        return now, text
    if text == "yesterday":
        return now - timedelta(days=1), text

    match = re.search(r"(?:about|almost|over)?\s*(\d+|a|an)\s+(day|week|month|year)s?", text)
    if not match:
        return None, text
    amount_raw, unit = match.groups()
    amount = 1 if amount_raw in {"a", "an"} else int(amount_raw)
    multiplier = {"day": 1, "week": 7, "month": 30, "year": 365}[unit]
    return now - timedelta(days=amount * multiplier), text
