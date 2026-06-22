"""Resume-to-listing matching / scoring (spec Section 7).

Two strategies:
  * Weighted keyword overlap (always available, no API key). Exact tool/language
    matches are weighted highest (see internfinder/domain.py); title hits count
    more than body hits. A saturating curve maps weight -> 0..100.
  * Optional Claude scoring. We pre-rank by keyword score, then ask Claude to
    re-score the top N (cost cap) with a 1-sentence rationale + matched/missing
    skills. Falls back to the keyword score on any API error or missing key.

Model default is claude-haiku-4-5 ($1/$5 per 1M tokens) — cheap and fast for
high-volume per-listing scoring. Swap to claude-sonnet-4-6 / claude-opus-4-8 via
config `matching.llm_model` for higher-quality scoring.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re

from .models import Listing, ResumeProfile

log = logging.getLogger("internfinder.matcher")

# JSON schema for structured LLM output (Section 7).
_SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "rationale": {"type": "string"},
        "matched_skills": {"type": "array", "items": {"type": "string"}},
        "missing_skills": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "rationale", "matched_skills", "missing_skills"],
    "additionalProperties": False,
}

_LLM_SYSTEM = (
    "You score how well a candidate's background fits an internship, for ANY "
    "field (engineering, business, design, science, healthcare, the humanities, "
    "etc.) — never assume a domain. Infer the candidate's field from their resume "
    "and stated target roles, then weight exact matches on their concrete skills, "
    "tools, and coursework highest, and give partial credit for adjacent skills. "
    "Reward roles aligned with the candidate's stated target; score down roles "
    "that are clearly in a different field than both their background and target. "
    "Return an integer score 0-100, a one-sentence rationale, and the matched and "
    "notable missing skills. Respond only via the provided schema."
)


def score_listings(profile: ResumeProfile, listings: list[Listing], config: dict) -> None:
    """Score every listing in place (sets match_score / matched / missing / rationale)."""
    # 1) keyword baseline for all.
    for l in listings:
        s, matched, missing = keyword_score(profile, l)
        l.match_score = s
        l.matched_keywords = matched
        l.missing_keywords = missing
        l.match_rationale = _keyword_rationale(matched)

    # 2) optional LLM refinement of the top-N by keyword score.
    client, model = _maybe_client(config)
    if client is None:
        return
    cap = int(config.get("matching", {}).get("llm_max_listings", 60))
    ranked = sorted(listings, key=lambda x: x.match_score, reverse=True)[:cap]
    log.info("matcher: LLM-scoring top %d/%d listings with %s", len(ranked), len(listings), model)
    n_ok = 0
    for l in ranked:
        try:
            _llm_score(client, model, profile, l)
            n_ok += 1
        except Exception as exc:  # fall back to keyword score for this one
            log.debug("matcher: LLM scoring failed for %s (%s); keeping keyword score",
                      l.company, exc)
    log.info("matcher: LLM scored %d/%d successfully", n_ok, len(ranked))


# ---------------------------------------------------------------- keyword path
def _present(term: str, text: str) -> bool:
    if not term:
        return False
    pat = r"(?<![A-Za-z0-9+#])" + re.escape(term) + r"(?![A-Za-z0-9+#])"
    return re.search(pat, text, re.IGNORECASE) is not None


def keyword_score(profile: ResumeProfile, listing: Listing) -> tuple[int, list[str], list[str]]:
    title = (listing.title or "").lower()
    body = " ".join([listing.description_text or "", " ".join(listing.requirements or [])]).lower()

    matched: list[str] = []
    weight = 0.0
    for term, w in profile.weighted_keywords.items():
        in_title = _present(term, title)
        in_body = _present(term, body)
        if in_title or in_body:
            matched.append(term)
            weight += w * (1.6 if in_title else 1.0)

    # Saturating map: weight 7 -> ~63, 14 -> ~86, 21 -> ~95.
    score = 100.0 * (1.0 - math.exp(-weight / 7.0))

    # Title-only / no-JD listings (e.g. curated lists) can't fully match — cap
    # them so richly-described listings rank above them at equal title overlap.
    if not listing.description_text:
        score = min(score, 70.0)

    # Surface the most important resume skills the JD did NOT mention.
    top_priority = [t for t, w in profile.weighted_keywords.items() if w >= 1.4]
    missing = [t for t in top_priority if t not in matched][:6]

    return int(round(score)), matched[:20], missing


def _keyword_rationale(matched: list[str]) -> str:
    if not matched:
        return "Keyword match: no strong overlap with resume skills."
    return "Keyword match on: " + ", ".join(matched[:8]) + (" …" if len(matched) > 8 else "")


# -------------------------------------------------------------------- LLM path
def _maybe_client(config: dict):
    """Return (client, model) if LLM scoring is enabled and possible, else (None, '')."""
    mcfg = config.get("matching", {})
    mode = str(mcfg.get("use_llm", "auto")).lower()
    model = mcfg.get("llm_model", "claude-haiku-4-5")
    if mode == "never":
        return None, ""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        if mode == "always":
            log.warning("matching.use_llm=always but ANTHROPIC_API_KEY is unset — using keyword scoring")
        else:
            log.info("matcher: no ANTHROPIC_API_KEY — using keyword scoring (set it to enable Claude scoring)")
        return None, ""
    try:
        import anthropic
    except ImportError:
        log.info("matcher: 'anthropic' not installed — using keyword scoring (pip install anthropic)")
        return None, ""
    try:
        return anthropic.Anthropic(), model
    except Exception as exc:
        log.warning("matcher: could not init Anthropic client (%s) — using keyword scoring", exc)
        return None, ""


def _llm_score(client, model: str, profile: ResumeProfile, listing: Listing) -> None:
    jd = (listing.description_text or "")[:4000]
    target = getattr(profile, "target_role", "") or "(not specified — infer from the resume)"
    user = (
        f"CANDIDATE PROFILE:\n{profile.summary or '(resume text was sparse)'}\n"
        f"STATED TARGET ROLES/FIELD: {target}\n\n"
        f"INTERNSHIP:\nCompany: {listing.company}\nTitle: {listing.title}\n"
        f"Location: {listing.location}\nDescription:\n{jd or '(no description available; score from the title)'}\n\n"
        "Score the fit."
    )
    # Note: Haiku does not accept the `effort` parameter; keep the call minimal.
    resp = client.messages.create(
        model=model,
        max_tokens=600,
        system=_LLM_SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": _SCORE_SCHEMA}},
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    data = json.loads(text)
    listing.match_score = max(0, min(100, int(data.get("score", listing.match_score))))
    listing.match_rationale = (data.get("rationale") or listing.match_rationale).strip()
    if data.get("matched_skills"):
        listing.matched_keywords = [str(s) for s in data["matched_skills"]][:20]
    if data.get("missing_skills"):
        listing.missing_keywords = [str(s) for s in data["missing_skills"]][:10]
