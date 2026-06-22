"""Community-curated internship tracking repos (Tier 1, item 3).

These README tables are freshness-gold because maintainers manually mark closures
(usually a 🔒 emoji). We:
  1. Verify the repo is *actively maintained* (last commit within
     ``max_commit_age_days``); a stale repo is skipped entirely.
  2. Pull the README markdown and parse its listing table(s).
  3. Drop rows flagged closed; carry forward the company name across "↳"
     continuation rows; parse the posted-date column.

Date provenance: an explicit date column => verified; an "age" string
("3d", "2 days ago") => approximate.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from ..models import CONF_APPROXIMATE, CONF_VERIFIED, Listing
from . import base

log = logging.getLogger("internfinder.sources.github_lists")

_GH_API = "https://api.github.com"

# Markers that a row is closed / no longer open.
_CLOSED_MARKERS = ("🔒", "❌", "🚫")
_CLOSED_WORDS = re.compile(r"\bclosed\b", re.I)
# Continuation marker: the row belongs to the company named above.
_CONT_CHARS = set("↳⤷∟⟶→⮑⬆️⬆ ")


def fetch(ctx: base.SourceContext) -> list[Listing]:
    cfg = ctx.config.get("sources", {}).get("github_lists", {})
    if not cfg.get("enabled", True):
        return []
    repos = cfg.get("repos", []) or []
    max_age = int(cfg.get("max_commit_age_days", 14))
    out: list[Listing] = []
    for repo in repos:
        try:
            out.extend(_fetch_repo(ctx, repo, max_age))
        except Exception as exc:
            log.warning("github_lists:%s failed: %s", repo, exc)
    log.info("github_lists: %d open listings from %d repos", len(out), len(repos))
    return out


def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _fetch_repo(ctx: base.SourceContext, repo: str, max_age_days: int) -> list[Listing]:
    headers = _gh_headers()
    # 1) Freshness gate: last commit must be recent.
    commits = ctx.http.get(f"{_GH_API}/repos/{repo}/commits?per_page=1", headers=headers)
    if commits.ok:
        try:
            last = commits.json()[0]["commit"]["committer"]["date"]
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - last_dt).days
            if age > max_age_days:
                log.info("github_lists:%s skipped — last commit %dd ago (>%d)", repo, age, max_age_days)
                return []
            log.info("github_lists:%s last commit %dd ago — fresh", repo, age)
        except Exception:
            log.debug("github_lists:%s could not read last-commit date; proceeding", repo)
    else:
        log.info("github_lists:%s commit check failed (%s); proceeding cautiously",
                 repo, commits.status or commits.error)

    # 2) README markdown (via download_url to dodge base64 size limits).
    md = _fetch_readme(ctx, repo, headers)
    if not md:
        return []
    listings = parse_markdown_table(md, repo)
    return listings


def _fetch_readme(ctx: base.SourceContext, repo: str, headers: dict) -> str:
    meta = ctx.http.get(f"{_GH_API}/repos/{repo}/readme", headers=headers)
    if meta.ok:
        try:
            dl = meta.json().get("download_url")
            if dl:
                raw = ctx.http.get(dl)
                if raw.ok:
                    return raw.text
        except Exception:
            pass
    # Fallback: raw on common default branches.
    for branch in ("main", "master", "HEAD"):
        raw = ctx.http.get(f"https://raw.githubusercontent.com/{repo}/{branch}/README.md")
        if raw.ok and raw.text:
            return raw.text
    return ""


# ----------------------------------------------------------------- table parse
def parse_markdown_table(md: str, repo: str) -> list[Listing]:
    lines = md.splitlines()
    listings: list[Listing] = []
    i = 0
    last_company = ""
    last_company_url = ""
    while i < len(lines):
        line = lines[i]
        if _is_table_row(line) and i + 1 < len(lines) and _is_separator(lines[i + 1]):
            header = _split_row(line)
            cols = _map_columns(header)
            i += 2
            if cols.get("role") is None:  # not a listings table
                continue
            while i < len(lines) and _is_table_row(lines[i]):
                row = _split_row(lines[i])
                listing, last_company, last_company_url = _row_to_listing(
                    row, cols, repo, last_company, last_company_url
                )
                if listing:
                    listings.append(listing)
                i += 1
        else:
            i += 1
    return listings


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.count("|") >= 2


def _is_separator(line: str) -> bool:
    return bool(re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", line)) and "-" in line


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _map_columns(header: list[str]) -> dict[str, Optional[int]]:
    cols: dict[str, Optional[int]] = {"company": None, "role": None,
                                      "location": None, "link": None, "date": None}
    for idx, h in enumerate(header):
        hl = h.lower()
        if cols["company"] is None and ("company" in hl or "name" in hl):
            cols["company"] = idx
        elif cols["role"] is None and ("role" in hl or "position" in hl or "title" in hl):
            cols["role"] = idx
        elif cols["location"] is None and "location" in hl:
            cols["location"] = idx
        elif cols["link"] is None and ("application" in hl or "apply" in hl or "link" in hl):
            cols["link"] = idx
        elif cols["date"] is None and ("date" in hl or "posted" in hl or "age" in hl):
            cols["date"] = idx
    return cols


def _cell(row: list[str], idx: Optional[int]) -> str:
    if idx is None or idx >= len(row):
        return ""
    return row[idx]


def _row_to_listing(row, cols, repo, last_company, last_company_url):
    full = " ".join(row)
    if any(m in full for m in _CLOSED_MARKERS) or _CLOSED_WORDS.search(full):
        return None, last_company, last_company_url  # closed — skip

    company_cell = _cell(row, cols["company"])
    company, company_url = _parse_company(company_cell)
    if not company or _is_continuation(company_cell):
        company, company_url = last_company, last_company_url
    else:
        last_company, last_company_url = company, company_url
    if not company:
        return None, last_company, last_company_url

    role = _strip_md(_cell(row, cols["role"]))
    if not role:
        return None, last_company, last_company_url

    location = _clean_location(_cell(row, cols["location"]))
    apply_url = _best_apply_url(_cell(row, cols["link"])) or company_url
    posted, conf, datesrc = _parse_list_date(_cell(row, cols["date"]))

    if not base.looks_like_internship(role):
        # The repo is internship-only, but guard against header/footer noise.
        if "intern" not in role.lower() and "co-op" not in role.lower():
            pass  # keep anyway: these repos are curated internship lists

    listing = Listing(
        company=company,
        title=role,
        location=location,
        apply_url=apply_url,
        source=f"github:{repo}",
        level=base.infer_level(role),
        work_mode=base.detect_work_mode(location),
        posted_date=posted,
        date_confidence=conf,
        date_source=datesrc,
    )
    return listing, last_company, last_company_url


def _is_continuation(cell: str) -> bool:
    stripped = cell.strip().strip("*").strip()
    if not stripped:
        return True
    return all(ch in _CONT_CHARS for ch in stripped)


def _parse_company(cell: str) -> tuple[str, str]:
    cell = cell.strip()
    m = re.search(r"\[([^\]]+)\]\(([^)]+)\)", cell)
    if m:
        return _strip_md(m.group(1)), m.group(2)
    return _strip_md(cell), ""


def _strip_md(text: str) -> str:
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links/images -> text
    text = re.sub(r"<[^>]+>", " ", text)                     # html tags
    text = text.replace("**", "").replace("*", "").replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def _clean_location(cell: str) -> str:
    cell = re.sub(r"<br\s*/?>", "; ", cell, flags=re.I)
    return _strip_md(cell)


def _best_apply_url(cell: str) -> str:
    urls = re.findall(r'href="([^"]+)"', cell)
    urls += [m.group(2) for m in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", cell)]
    candidates = [
        u for u in urls
        if u.startswith("http")
        and not re.search(r"\.(png|svg|gif|jpg|jpeg)(\?|$)", u, re.I)
        and "shields.io" not in u and "/assets/" not in u
    ]
    direct = [u for u in candidates if "simplify.jobs" not in u and "github.com" not in u]
    if direct:
        return direct[0]
    return candidates[0] if candidates else ""


def _parse_list_date(cell: str):
    """Return (date|None, confidence, source_label)."""
    text = _strip_md(cell).strip()
    if not text:
        return None, CONF_APPROXIMATE, ""

    # Compact age: 3d, 12d, 2w, 1mo, 5h, 1y
    m = re.fullmatch(r"(\d+)\s*(h|hr|hrs|d|day|days|w|wk|wks|mo|mos|month|months|y|yr)", text, re.I)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        days = (0 if unit.startswith("h")
                else 1 if unit.startswith("d")
                else 7 if unit.startswith("w")
                else 365 if unit.startswith("y")
                else 30)  # months
        return date.today() - timedelta(days=n * days), CONF_APPROXIMATE, "curated list age"

    # "N days ago" phrasing
    rel, phrase = base.parse_relative_date(text)
    if rel:
        return rel, CONF_APPROXIMATE, "curated list age"

    # Explicit date ("Oct 15", "2025-10-15", "Oct 15, 2025")
    explicit = _parse_explicit_date(text)
    if explicit:
        return explicit, CONF_VERIFIED, "curated list date column"
    return None, CONF_APPROXIMATE, ""


def _parse_explicit_date(text: str) -> Optional[date]:
    from dateutil import parser as dp

    try:
        default = datetime(date.today().year, 1, 1)
        dt = dp.parse(text, default=default, fuzzy=True).date()
    except (ValueError, OverflowError, TypeError):
        return None
    # No-year "Mon DD" inference: a date landing in the future means last year.
    if dt > date.today() + timedelta(days=7) and not re.search(r"\b(19|20)\d\d\b", text):
        dt = dt.replace(year=dt.year - 1)
    return dt
