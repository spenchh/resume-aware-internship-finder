"""Resume-to-listing matching / scoring (spec Section 7).

Two strategies:
  * Weighted keyword overlap (always available, no API key). Exact tool/language
    matches are weighted highest (see internfinder/domain.py); title hits count
    more than body hits. A saturating curve maps weight -> 0..100.
  * Optional AI scoring. We pre-rank by keyword score, then ask either an
    open-weight OpenRouter model or Claude to re-score the top N (cost cap) with
    a 1-sentence rationale + matched/missing skills. Falls back to the keyword
    score on any API error or missing key.

Default provider order is OpenRouter open-weight first, then Claude. The
open-weight default is z-ai/glm-5.2; Claude default is claude-haiku-4-5.
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
    "etc.) - never assume a domain. Infer the candidate's field from their resume "
    "and stated target roles, then weight exact matches on their concrete skills, "
    "tools, and coursework highest, and give partial credit for adjacent skills. "
    "Reward roles aligned with the candidate's stated target; score down roles "
    "that are clearly in a different field than both their background and target. "
    "Return an integer score 0-100, a one-sentence rationale, and the matched and "
    "notable missing skills."
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

    # 2) optional AI refinement of the top-N by keyword score.
    llm = _maybe_client(config)
    if llm is None:
        return
    cap = int(config.get("matching", {}).get("llm_max_listings", 60))
    ranked = sorted(listings, key=lambda x: x.match_score, reverse=True)[:cap]
    log.info(
        "matcher: AI-scoring top %d/%d listings with %s:%s",
        len(ranked),
        len(listings),
        llm["provider"],
        llm["model"],
    )
    n_ok = 0
    for l in ranked:
        try:
            _llm_score(llm, profile, l)
            n_ok += 1
        except Exception as exc:  # fall back to keyword score for this one
            log.debug("matcher: AI scoring failed for %s (%s); keeping keyword score", l.company, exc)
    log.info("matcher: AI scored %d/%d successfully", n_ok, len(ranked))


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

    # Title-only / no-JD listings (e.g. curated lists) cannot fully match, so
    # richly-described listings rank above them at equal title overlap.
    if not listing.description_text:
        score = min(score, 70.0)

    # Surface the most important resume skills the JD did NOT mention.
    top_priority = [t for t, w in profile.weighted_keywords.items() if w >= 1.4]
    missing = [t for t in top_priority if t not in matched][:6]

    return int(round(score)), matched[:20], missing


def _keyword_rationale(matched: list[str]) -> str:
    if not matched:
        return "Keyword match: no strong overlap with resume skills."
    return "Keyword match on: " + ", ".join(matched[:8]) + (" ..." if len(matched) > 8 else "")


# -------------------------------------------------------------------- AI path
def _maybe_client(config: dict):
    """Return AI-scoring config if enabled and possible, else ``None``."""
    mcfg = config.get("matching", {})
    mode = str(mcfg.get("use_llm", "auto")).lower()
    provider = str(mcfg.get("llm_provider", "auto")).lower().replace("_", "-")
    claude_model = mcfg.get("llm_model", "claude-haiku-4-5")
    openrouter_model = mcfg.get("openrouter_model", "z-ai/glm-5.2")
    if mode == "never":
        return None

    if provider in {"auto", "openrouter", "open-weight", "openweight"}:
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if api_key:
            return {"provider": "openrouter", "api_key": api_key, "model": openrouter_model}
        if provider != "auto":
            log.warning(
                "matching.llm_provider=%s but OPENROUTER_API_KEY is unset - using keyword scoring",
                provider,
            )
            return None

    if provider in {"auto", "anthropic", "claude"}:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            if provider != "auto" or mode == "always":
                log.warning("Claude scoring requested but ANTHROPIC_API_KEY is unset - using keyword scoring")
            else:
                log.info("matcher: no OPENROUTER_API_KEY or ANTHROPIC_API_KEY - using keyword scoring")
            return None
        try:
            import anthropic
        except ImportError:
            log.info("matcher: 'anthropic' not installed - using keyword scoring (pip install anthropic)")
            return None
        try:
            return {"provider": "anthropic", "client": anthropic.Anthropic(), "model": claude_model}
        except Exception as exc:
            log.warning("matcher: could not init Anthropic client (%s) - using keyword scoring", exc)
            return None

    log.warning("Unknown matching.llm_provider=%r - using keyword scoring", provider)
    return None


def _llm_score(llm: dict, profile: ResumeProfile, listing: Listing) -> None:
    if llm["provider"] == "openrouter":
        _openrouter_score(llm, profile, listing)
    else:
        _anthropic_score(llm["client"], llm["model"], profile, listing)


def _score_prompt(profile: ResumeProfile, listing: Listing) -> str:
    jd = (listing.description_text or "")[:4000]
    target = getattr(profile, "target_role", "") or "(not specified - infer from the resume)"
    return (
        f"CANDIDATE PROFILE:\n{profile.summary or '(resume text was sparse)'}\n"
        f"STATED TARGET ROLES/FIELD: {target}\n\n"
        f"INTERNSHIP:\nCompany: {listing.company}\nTitle: {listing.title}\n"
        f"Location: {listing.location}\nDescription:\n{jd or '(no description available; score from the title)'}\n\n"
        "Score the fit."
    )


def _apply_score_data(listing: Listing, data: dict) -> None:
    listing.match_score = max(0, min(100, int(data.get("score", listing.match_score))))
    listing.match_rationale = (data.get("rationale") or listing.match_rationale).strip()
    if data.get("matched_skills"):
        listing.matched_keywords = [str(s) for s in data["matched_skills"]][:20]
    if data.get("missing_skills"):
        listing.missing_keywords = [str(s) for s in data["missing_skills"]][:10]


def _parse_score_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _openrouter_score(llm: dict, profile: ResumeProfile, listing: Listing) -> None:
    import requests

    payload = {
        "model": llm["model"],
        "messages": [
            {"role": "system", "content": _LLM_SYSTEM + " Return only valid JSON."},
            {"role": "user", "content": _score_prompt(profile, listing)},
        ],
        "temperature": 0,
        "max_tokens": 700,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "internship_match_score",
                "strict": True,
                "schema": _SCORE_SCHEMA,
            },
        },
    }
    headers = {
        "Authorization": f"Bearer {llm['api_key']}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.environ.get(
            "OPENROUTER_HTTP_REFERER",
            "https://resume-aware-internship-finder.streamlit.app",
        ),
        "X-Title": "Resume-Aware Internship Finder",
    }
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=45,
    )
    if resp.status_code >= 400 and "response_format" in payload:
        payload.pop("response_format", None)
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=45,
        )
    resp.raise_for_status()
    choice = (resp.json().get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content", "")
    if isinstance(content, list):
        content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    _apply_score_data(listing, _parse_score_json(str(content)))


def _anthropic_score(client, model: str, profile: ResumeProfile, listing: Listing) -> None:
    # Note: Haiku does not accept the `effort` parameter; keep the call minimal.
    resp = client.messages.create(
        model=model,
        max_tokens=600,
        system=_LLM_SYSTEM + " Respond only via the provided schema.",
        messages=[{"role": "user", "content": _score_prompt(profile, listing)}],
        output_config={"format": {"type": "json_schema", "schema": _SCORE_SCHEMA}},
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    _apply_score_data(listing, json.loads(text))
