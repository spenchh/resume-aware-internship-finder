"""Freshness validation — the critical path (spec Section 6).

Pipeline (driven by cli.py in this order):
  1. resolve_date()      — hard date -> relative -> cache first-seen -> [unverified]
  2. dedupe()            — collapse cross-source duplicates (Section 6.5)
  3. mark_eligibility()  — recency window OR deadline window (Section 6.3)
  4. live_check()        — re-request every retained URL right before reporting,
                           classify 404 / generic-redirect / closure-text / open
                           (Section 6.4). Only confirmed-open rows are labeled
                           "verified live"; confirmed-dead rows are dropped.

Design bias (Section 12): never upgrade confidence we don't have. When in doubt
about a *date*, we label it unverified, not fresh. When in doubt about *liveness*
(bot-block, timeout), we mark it inconclusive and keep it flagged — we only drop
on a positive closed/404 signal, so we don't silently present a dead link as live.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from .models import (
    CONF_APPROXIMATE,
    CONF_UNVERIFIED,
    CONF_VERIFIED,
    LIVE_DEAD,
    LIVE_OK,
    LIVE_SKIPPED,
    LIVE_UNKNOWN,
    Listing,
)
from .sources import schemaorg

log = logging.getLogger("internfinder.freshness")


# ----------------------------------------------------------------- 1. dates
def resolve_date(listing: Listing) -> None:
    """Apply the Section 6.2 fallback chain in place.

    Sources already set a hard/relative date where they had one. Here we only
    *fill gaps*: if there's still no date, try the cache's first-seen timestamp
    (set on ``listing.first_seen`` by Cache.observe), else mark unverified.
    """
    if listing.posted_date is not None:
        # Trust the source's confidence label (verified for hard dates,
        # approximate for relative strings). Nothing to do.
        if listing.date_confidence not in (CONF_VERIFIED, CONF_APPROXIMATE):
            listing.date_confidence = CONF_APPROXIMATE
        return

    if listing.first_seen is not None:
        listing.posted_date = listing.first_seen.date()
        listing.date_confidence = CONF_APPROXIMATE
        listing.date_source = "cache first-seen (this tool)"
        return

    listing.date_confidence = CONF_UNVERIFIED
    if not listing.date_source:
        listing.date_source = "no determinable date"


# ----------------------------------------------------------------- 2. dedupe
_SOURCE_TIER = {  # lower = more trusted / richer
    "schemaorg": 0, "greenhouse": 0, "lever": 0, "ashby": 0,
    "github": 1, "yc": 1, "wellfound": 2, "serpapi": 3,
}


def _source_tier(source: str) -> int:
    head = (source or "").split(":", 1)[0]
    return _SOURCE_TIER.get(head, 5)


def _conf_rank(conf: str) -> int:
    return {CONF_VERIFIED: 0, CONF_APPROXIMATE: 1, CONF_UNVERIFIED: 2}.get(conf, 3)


def _quality(listing: Listing) -> tuple:
    # Lower tuple sorts as "better primary".
    return (
        _conf_rank(listing.date_confidence),
        _source_tier(listing.source),
        -len(listing.description_text or ""),
        -len(listing.requirements or []),
    )


def dedupe(listings: list[Listing]) -> list[Listing]:
    """Collapse duplicates by normalized company+role+location (Section 6.5).

    The richest/most-trusted row becomes primary; we backfill its missing fields
    (date, description, requirements, apply URL) from the others and record every
    source that surfaced it.
    """
    groups: dict[str, list[Listing]] = {}
    for l in listings:
        groups.setdefault(l.dedup_key(), []).append(l)

    merged: list[Listing] = []
    for key, group in groups.items():
        group.sort(key=_quality)
        primary = group[0]
        also: list[str] = []
        for other in group[1:]:
            also.append(other.source)
            # Backfill a better date if the primary lacks a verified one.
            if (_conf_rank(other.date_confidence) < _conf_rank(primary.date_confidence)):
                primary.posted_date = other.posted_date
                primary.date_confidence = other.date_confidence
                primary.date_source = other.date_source
            if not primary.deadline and other.deadline:
                primary.deadline = other.deadline
                primary.deadline_is_rolling = other.deadline_is_rolling
            if not primary.description_text and other.description_text:
                primary.description_text = other.description_text
                primary.requirements = other.requirements or primary.requirements
            if not primary.company_description and other.company_description:
                primary.company_description = other.company_description
            if primary.is_startup is None and other.is_startup is not None:
                primary.is_startup = other.is_startup
        if also:
            primary.raw["also_seen_sources"] = also
        merged.append(primary)
    log.info("dedupe: %d listings -> %d unique", len(listings), len(merged))
    return merged


# ------------------------------------------------------------ 3. eligibility
def mark_eligibility(listing: Listing, config: dict, now: date | None = None) -> bool:
    now = now or datetime.now(timezone.utc).date()
    fr = config.get("freshness", {})
    recency = int(fr.get("recency_days", 21))
    lookahead = int(fr.get("deadline_lookahead_days", 14))
    include_unverified = bool(fr.get("include_unverified", True))

    reasons: list[str] = []
    eligible = False

    # Rule A: posted within recency window.
    if listing.posted_date is not None and listing.date_confidence != CONF_UNVERIFIED:
        age = (now - listing.posted_date).days
        if 0 <= age <= recency:
            eligible = True
            reasons.append(f"posted {age}d ago (≤{recency}d)")
        elif age < 0:
            # Future "posted" date — treat as recent but note it.
            eligible = True
            reasons.append("posted date in the future (treated as fresh)")

    # Rule B: deadline within lookahead window.
    if listing.deadline is not None and not listing.deadline_is_rolling:
        dleft = (listing.deadline - now).days
        if 0 <= dleft <= lookahead:
            eligible = True
            reasons.append(f"deadline in {dleft}d (≤{lookahead}d)")

    # Rule C: unverified date — keep if configured, but clearly flagged.
    if not eligible and listing.date_confidence == CONF_UNVERIFIED and include_unverified:
        eligible = True
        reasons.append("date unverified (included, flagged, sorted below verified)")

    listing.eligible = eligible
    listing.eligibility_reason = "; ".join(reasons) if reasons else "outside recency/deadline windows"
    return eligible


# ------------------------------------------------------------- 4. live check
# High-precision closure phrases. Substring match on lowercased body. Kept
# specific to avoid false positives on multi-listing pages.
CLOSURE_PHRASES: tuple[str, ...] = (
    "no longer accepting applications",
    "no longer accepting candidates",
    "we are no longer accepting",
    "this position is closed",
    "this position has been filled",
    "position has been filled",
    "this role is no longer open",
    "this role has been filled",
    "this job is no longer active",
    "this job is no longer available",
    "this opportunity is no longer available",
    "position is no longer available",
    "job posting has expired",
    "this posting has expired",
    "this posting is closed",
    "applications are closed",
    "application period has closed",
    "this listing has been closed",
    "job has been filled",
    "the job you are looking for",   # common ATS "not found" copy
    "job not found",
    "position not found",
    "this job has expired",
)

_GENERIC_LANDING = re.compile(
    r"^/?(jobs|careers|career|positions|openings|opportunities|join|join-us|"
    r"company/careers|search|all-jobs)?/?$",
    re.I,
)


def _is_generic_landing(final_url: str, requested_url: str) -> bool:
    """True if a redirect dumped a specific posting onto a generic listing page."""
    rp = urlsplit(requested_url).path.rstrip("/")
    fp = urlsplit(final_url).path.rstrip("/")
    if not rp or fp == rp:
        return False
    if _GENERIC_LANDING.match(fp or "/"):
        return True
    # Redirected "up" to an ancestor of the original (lost the job-id segment).
    if fp and rp.startswith(fp) and rp != fp:
        return True
    return False


def classify_live(status: int, final_url: str, requested_url: str,
                  body: str, redirected: bool, error: str = "") -> tuple[str, str]:
    """Pure classifier (no network) -> (live_status, reason). Unit-tested."""
    if status in (404, 410):
        return LIVE_DEAD, f"HTTP {status} (removed)"
    if status == 0 or error:
        return LIVE_UNKNOWN, f"unreachable ({error or 'no response'}) — kept, unverified"
    if status in (401, 403, 429) or 500 <= status < 600:
        return LIVE_UNKNOWN, f"HTTP {status} (blocked/transient) — kept, could not verify live"
    if not (200 <= status < 400):
        return LIVE_UNKNOWN, f"HTTP {status} — kept, could not verify live"

    body_l = (body or "")[:40000].lower()
    for phrase in CLOSURE_PHRASES:
        if phrase in body_l:
            return LIVE_DEAD, f"closure signal: '{phrase}'"

    open_flag = schemaorg.has_open_jobposting(body or "")
    if open_flag is False:
        return LIVE_DEAD, "schema.org validThrough indicates expired"

    if redirected and _is_generic_landing(final_url, requested_url):
        return LIVE_DEAD, f"redirected to generic page ({urlsplit(final_url).path or '/'})"

    return LIVE_OK, "verified live"


def live_check(listing: Listing, http, config: dict) -> None:
    """Re-request the apply URL and classify (Section 6.4). Mutates the listing."""
    fr = config.get("freshness", {})
    timeout = float(fr.get("live_check_timeout", 12))
    listing.live_checked_at = datetime.now(timezone.utc)

    url = listing.apply_url
    if not url or not url.startswith("http"):
        listing.live_status = LIVE_UNKNOWN
        listing.live_reason = "no application URL to verify"
        return

    res = http.get(url, timeout=timeout)
    status, reason = classify_live(
        res.status, res.url, res.requested_url, res.text, res.redirected, res.error
    )
    listing.live_status = status
    listing.live_reason = reason


def is_recent_first_seen(listing: Listing, days: int = 30) -> bool:
    """Helper: was this first observed by us within ``days``? (diagnostic)"""
    if not listing.first_seen:
        return False
    return (datetime.now(timezone.utc) - listing.first_seen) <= timedelta(days=days)
