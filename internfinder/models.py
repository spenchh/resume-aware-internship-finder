"""Core data models shared across the pipeline."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any, Optional


# Date-confidence labels (Section 6 / 8).
CONF_VERIFIED = "verified"        # a hard date from structured data (schema.org, ATS metadata, curated column)
CONF_APPROXIMATE = "approximate"  # a relative string ("posted 5 days ago") or cache first-seen
CONF_UNVERIFIED = "unverified"    # no determinable date

# Live-check statuses (Section 6.4).
LIVE_OK = "live"
LIVE_DEAD = "dead"
LIVE_UNKNOWN = "unknown"
LIVE_SKIPPED = "not_checked"


def _norm_token(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for dedup keys."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Common location noise we strip so "San Francisco, CA, USA" ~ "San Francisco".
_LOC_NOISE = {
    "usa", "us", "united states", "remote", "hybrid", "onsite", "on site",
    "ca", "wa", "ny", "tx", "ma", "or", "co", "area", "metropolitan",
}


def _norm_location(loc: str) -> str:
    toks = [t for t in _norm_token(loc).split() if t not in _LOC_NOISE]
    return " ".join(toks)


@dataclass
class ResumeProfile:
    """Structured view of the candidate's resume (Section 3.1)."""

    raw_text: str = ""
    name: str = ""
    degree: str = ""
    major: str = ""
    target_role: str = ""  # what the candidate said they're looking for (field-agnostic)
    skills: list[str] = field(default_factory=list)
    tools_languages: list[str] = field(default_factory=list)
    coursework: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    experience_titles: list[str] = field(default_factory=list)
    # keyword -> weight (higher = more important to match). Includes synonym
    # expansions (Section 3.1), so a JD mentioning "RTL" matches a "Verilog" resume.
    weighted_keywords: dict[str, float] = field(default_factory=dict)
    summary: str = ""  # compact natural-language summary fed to the LLM matcher

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Listing:
    """A single internship posting with every Section 8 output field."""

    # --- identity ---
    company: str
    title: str
    location: str = ""
    apply_url: str = ""
    source: str = ""  # e.g. "greenhouse:sifive", "github:vanshb03/Summer2027-Internships"

    # --- descriptive ---
    company_description: str = ""
    is_startup: Optional[bool] = None
    funding_stage: str = ""  # Seed / Series A / Public / ...
    level: str = ""          # intern / co-op / new grad / ...
    work_mode: str = ""      # remote / hybrid / onsite / unknown

    # --- JD content ---
    description_text: str = ""
    requirements: list[str] = field(default_factory=list)  # extracted tech stack / key reqs

    # --- dates (raw, as discovered by the source) ---
    posted_date: Optional[date] = None
    deadline: Optional[date] = None
    deadline_is_rolling: bool = False

    # --- freshness metadata (filled by freshness validator) ---
    date_confidence: str = CONF_UNVERIFIED
    date_source: str = ""            # human-readable provenance of the date
    first_seen: Optional[datetime] = None
    live_status: str = LIVE_SKIPPED
    live_checked_at: Optional[datetime] = None
    live_reason: str = ""
    eligible: bool = False
    eligibility_reason: str = ""

    # --- matching ---
    match_score: int = 0
    matched_keywords: list[str] = field(default_factory=list)
    missing_keywords: list[str] = field(default_factory=list)
    match_rationale: str = ""

    # --- raw passthrough for debugging ---
    raw: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ keys
    def dedup_key(self) -> str:
        """Normalized (company + role + location) key for cross-source dedup."""
        return "|".join(
            (
                _norm_token(self.company),
                _norm_token(self.title),
                _norm_location(self.location),
            )
        )

    def cache_key(self) -> str:
        """Stable key for the SQLite cache (same basis as dedup)."""
        return self.dedup_key()

    # --------------------------------------------------------------- helpers
    @property
    def deadline_display(self) -> str:
        if self.deadline_is_rolling:
            return "rolling"
        if self.deadline:
            return self.deadline.isoformat()
        return "—"

    @property
    def posted_display(self) -> str:
        if self.posted_date:
            return self.posted_date.isoformat()
        return "—"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # JSON-friendly date coercion
        for k in ("posted_date", "deadline"):
            v = d.get(k)
            if isinstance(v, (date, datetime)):
                d[k] = v.isoformat()
        for k in ("first_seen", "live_checked_at"):
            v = d.get(k)
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        return d
