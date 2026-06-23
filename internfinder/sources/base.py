"""Shared helpers for source fetchers."""

from __future__ import annotations

import html as _html
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from .. import domain
from ..http_util import HttpClient

log = logging.getLogger("internfinder.sources")


@dataclass
class SourceContext:
    """Everything a source needs to run."""

    http: HttpClient
    config: dict
    role_keywords: list[str] = field(default_factory=list)


# Internship-ish title signals. We filter to these so we don't ingest every
# full-time role on a board.
_INTERN_RE = re.compile(
    r"\b(intern(?:ship)?s?|co-?op|industrial placement|summer (?:analyst|associate)|"
    r"student (?:engineer|researcher)|undergraduate|university graduate|new ?grad)\b",
    re.I,
)
# Things that look like internships but usually aren't entry student roles.
_INTERN_NEGATIVE = re.compile(r"\binternal\b", re.I)


def looks_like_internship(title: str, text: str = "") -> bool:
    blob = f"{title}\n{text[:400]}"
    if _INTERN_NEGATIVE.search(title or ""):
        # "internal" tripping the 'intern' stem — require a real signal elsewhere.
        return bool(_INTERN_RE.search(text or ""))
    return bool(_INTERN_RE.search(blob))


def infer_level(title: str) -> str:
    t = (title or "").lower()
    if "co-op" in t or "coop" in t:
        return "co-op"
    if "intern" in t:
        return "intern"
    if "new grad" in t or "new-grad" in t or "university graduate" in t:
        return "new grad"
    return "intern"  # we only keep internship-ish rows, so default sensibly


def detect_work_mode(*texts: str) -> str:
    blob = " ".join(t for t in texts if t).lower()
    if re.search(r"\b(fully remote|remote[- ]first|100% remote|work from home|wfh)\b", blob):
        return "remote"
    if "hybrid" in blob:
        return "hybrid"
    if re.search(r"\b(on-?site|in office|in-person)\b", blob):
        return "onsite"
    if re.search(r"\bremote\b", blob):
        return "remote"
    return "unknown"


def html_to_text(html_str: str) -> str:
    if not html_str:
        return ""
    # Greenhouse double-encodes entities; unescape once before parsing.
    soup = BeautifulSoup(_html.unescape(html_str), "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_requirements(text: str, limit: int = 18) -> list[str]:
    """Pull recognizable requirement terms out of a JD for any field."""
    out: list[str] = []
    seen: set[str] = set()
    for term in domain.extract_known_terms(text):
        if term not in seen:
            seen.add(term)
            out.append(term)
    for term in domain.extract_generic_terms(text, top_n=limit * 2):
        if term not in seen:
            seen.add(term)
            out.append(term)
        if len(out) >= limit:
            break
    return out[:limit]


def parse_date_loose(value) -> Optional[date]:
    """Parse many date shapes into a ``date``. Returns None on failure."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    # epoch milliseconds (Lever) or seconds
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:  # milliseconds
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return parse_date_loose(int(s))
    try:
        return dateparser.parse(s).date()
    except (ValueError, OverflowError, TypeError):
        return None


def slug_to_name(slug: str) -> str:
    """Best-effort human company name from an ATS slug."""
    return re.sub(r"[-_]+", " ", slug).strip().title()


# Relative-date strings from aggregators ("Posted 5 days ago"). Returns an
# approximate date and the matched phrase, or (None, "").
_REL_RE = re.compile(
    r"\b(?:posted|updated)?\s*"
    r"(?:(\d+)\+?\s*(day|days|week|weeks|month|months|hour|hours)\s*ago"
    r"|(today|just posted|yesterday))",
    re.I,
)


def parse_relative_date(text: str, now: Optional[date] = None) -> tuple[Optional[date], str]:
    from datetime import timedelta

    if not text:
        return None, ""
    now = now or datetime.now(timezone.utc).date()
    m = _REL_RE.search(text)
    if not m:
        return None, ""
    phrase = m.group(0).strip()
    if m.group(3):
        kw = m.group(3).lower()
        if kw == "yesterday":
            return now - timedelta(days=1), phrase
        return now, phrase  # today / just posted
    n = int(m.group(1))
    unit = m.group(2).lower()
    days = {"hour": 0, "hours": 0, "day": 1, "days": 1,
            "week": 7, "weeks": 7, "month": 30, "months": 30}[unit]
    return now - timedelta(days=n * days), phrase
