"""Report rendering (spec Sections 8 & 9).

Emits a Markdown and/or HTML report. Listings are sorted by deadline urgency
(soonest first), then match score. Verified-date listings are shown in a separate
section from unverified-date ones so that distinction is never buried.
"""

from __future__ import annotations

import html
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

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

log = logging.getLogger("internfinder.report")


def _deadline_sort_key(l: Listing):
    today = datetime.now(timezone.utc).date()
    if l.deadline and not l.deadline_is_rolling:
        return (0, (l.deadline - today).days, -l.match_score)
    return (1, 0, -l.match_score)  # rolling / no deadline sort after dated ones


def _split_buckets(listings: list[Listing]) -> tuple[list[Listing], list[Listing]]:
    verified, unverified = [], []
    for l in listings:
        (unverified if l.date_confidence == CONF_UNVERIFIED else verified).append(l)
    verified.sort(key=_deadline_sort_key)
    unverified.sort(key=_deadline_sort_key)
    return verified, unverified


def generate(
    listings: list[Listing],
    profile,
    config: dict,
    *,
    diff: tuple[list[str], list[str]] | None = None,
    out_dir: str | Path | None = None,
) -> dict[str, Path]:
    out_dir = Path(out_dir or config.get("output", {}).get("directory", "reports"))
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = str(config.get("output", {}).get("format", "markdown")).lower()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    verified, unverified = _split_buckets(listings)
    written: dict[str, Path] = {}

    # Always emit JSON for the Streamlit dashboard (same backend, Section 9).
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "listings": [l.to_dict() for l in (verified + unverified)],
    }
    (out_dir / "latest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    written["json"] = out_dir / "latest.json"

    if fmt in ("markdown", "both"):
        md = _render_markdown(verified, unverified, profile, config, diff)
        p = out_dir / f"internships-{stamp}.md"
        p.write_text(md, encoding="utf-8")
        (out_dir / "latest.md").write_text(md, encoding="utf-8")
        written["markdown"] = p
    if fmt in ("html", "both"):
        h = _render_html(verified, unverified, profile, config, diff)
        p = out_dir / f"internships-{stamp}.html"
        p.write_text(h, encoding="utf-8")
        (out_dir / "latest.html").write_text(h, encoding="utf-8")
        written["html"] = p
    return written


# --------------------------------------------------------------- field display
def _posted_display(l: Listing) -> str:
    if l.date_confidence == CONF_VERIFIED and l.posted_date:
        return f"{l.posted_date.isoformat()}  _(verified — {l.date_source})_"
    if l.date_confidence == CONF_APPROXIMATE and l.posted_date:
        return f"~{l.posted_date.isoformat()}  _(approximate — {l.date_source or 'relative date'})_"
    return f"**[unverified]**  _({l.date_source or 'no determinable date'})_"


def _live_display(l: Listing) -> str:
    ts = l.live_checked_at.strftime("%Y-%m-%d %H:%M UTC") if l.live_checked_at else "—"
    if l.live_status == LIVE_OK:
        return f"✅ verified live as of {ts}"
    if l.live_status == LIVE_UNKNOWN:
        return f"⚠️ could not verify ({l.live_reason}) — checked {ts}"
    if l.live_status == LIVE_DEAD:
        return f"❌ dead ({l.live_reason})"
    return "— not live-checked (disabled)"


def _startup_display(l: Listing) -> str:
    if l.is_startup is True:
        return f"Startup{f' · {l.funding_stage}' if l.funding_stage else ''}"
    if l.is_startup is False:
        return "Established / non-startup"
    return "Unknown"


def _sources_display(l: Listing) -> str:
    s = l.source or "—"
    also = l.raw.get("also_seen_sources")
    if also:
        s += "  ·  also seen: " + ", ".join(dict.fromkeys(also))
    return s


# ----------------------------------------------------------------- markdown
def _render_markdown(verified, unverified, profile, config, diff) -> str:
    fr = config.get("freshness", {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = []
    lines.append("# Internship Finder — Results")
    lines.append("")
    lines.append(f"_Generated {now}_")
    lines.append("")
    lines.append(
        f"**Windows:** posted ≤ {fr.get('recency_days', 21)}d  ·  "
        f"deadline ≤ {fr.get('deadline_lookahead_days', 14)}d  ·  "
        f"live-check {'ON' if fr.get('live_check', True) else 'OFF'}"
    )
    lines.append("")
    lines.append(
        f"**Totals:** {len(verified)} verified-date  ·  {len(unverified)} unverified-date  "
        f"·  {len(verified) + len(unverified)} retained (all live-checked this run)"
    )
    lines.append("")

    if diff is not None:
        new_keys, closed_keys = diff
        lines.append(f"**Since last run:** {len(new_keys)} new  ·  {len(closed_keys)} no longer listed")
        lines.append("")

    if profile is not None:
        who = " ".join(x for x in [getattr(profile, "degree", ""), getattr(profile, "major", "")] if x)
        tl = ", ".join(getattr(profile, "tools_languages", [])[:14])
        lines.append("<details><summary>Candidate profile used for matching</summary>")
        lines.append("")
        if who:
            lines.append(f"- **Background:** {who}")
        if tl:
            lines.append(f"- **Tools/languages:** {tl}")
        lines.append("</details>")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## ✅ Verified-fresh listings (posted/deadline date confirmed)")
    lines.append("")
    if verified:
        for i, l in enumerate(verified, 1):
            lines.extend(_md_card(i, l))
    else:
        lines.append("_None this run._")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## ⚠️ Unverified-date listings (included, date could not be confirmed)")
    lines.append("")
    lines.append(
        "_These passed the live-check but we could not establish a reliable "
        "posted/deadline date. Treat the date as unknown, not fresh._"
    )
    lines.append("")
    if unverified:
        for i, l in enumerate(unverified, 1):
            lines.extend(_md_card(i, l))
    else:
        lines.append("_None this run._")
        lines.append("")

    return "\n".join(lines)


def _md_card(rank: int, l: Listing) -> list[str]:
    out: list[str] = []
    out.append(f"### {rank}. {l.company} — {l.title}  ·  Match {l.match_score}/100")
    out.append("")
    if l.company_description:
        out.append(f"> {l.company_description}")
        out.append("")
    out.append(f"- **Startup:** {_startup_display(l)}")
    out.append(f"- **Level / mode:** {l.level or '—'} · {l.work_mode or 'unknown'}")
    out.append(f"- **Location:** {l.location or '—'}")
    out.append(f"- **Posted:** {_posted_display(l)}")
    out.append(f"- **Deadline:** {l.deadline_display}")
    out.append(f"- **Live check:** {_live_display(l)}")
    out.append(f"- **Source:** {_sources_display(l)}")
    out.append(f"- **Apply:** {l.apply_url or '—'}")
    if l.requirements:
        out.append(f"- **Tech stack / requirements:** {', '.join(l.requirements)}")
    out.append(f"- **Eligibility:** {l.eligibility_reason}")
    out.append(f"- **Match {l.match_score}/100** — {l.match_rationale}")
    if l.matched_keywords:
        out.append(f"  - Matched: {', '.join(l.matched_keywords)}")
    if l.missing_keywords:
        out.append(f"  - Missing: {', '.join(l.missing_keywords)}")
    out.append("")
    return out


# --------------------------------------------------------------------- HTML
def _render_html(verified, unverified, profile, config, diff) -> str:
    fr = config.get("freshness", {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def esc(s) -> str:
        return html.escape(str(s)) if s is not None else ""

    def card(rank: int, l: Listing) -> str:
        conf_class = {
            CONF_VERIFIED: "conf-verified",
            CONF_APPROXIMATE: "conf-approx",
            CONF_UNVERIFIED: "conf-unverified",
        }.get(l.date_confidence, "")
        live_class = {LIVE_OK: "live-ok", LIVE_UNKNOWN: "live-unknown",
                      LIVE_DEAD: "live-dead", LIVE_SKIPPED: "live-skip"}.get(l.live_status, "")
        reqs = ", ".join(esc(r) for r in l.requirements) if l.requirements else "—"
        matched = ", ".join(esc(k) for k in l.matched_keywords) if l.matched_keywords else "—"
        missing = ", ".join(esc(k) for k in l.missing_keywords) if l.missing_keywords else "—"
        apply = (f'<a href="{esc(l.apply_url)}" target="_blank" rel="noopener">{esc(l.apply_url)}</a>'
                 if l.apply_url else "—")
        return f"""
        <div class="card">
          <div class="card-head">
            <span class="rank">{rank}</span>
            <span class="company">{esc(l.company)}</span> —
            <span class="title">{esc(l.title)}</span>
            <span class="score">Match {l.match_score}/100</span>
          </div>
          <p class="desc">{esc(l.company_description)}</p>
          <ul>
            <li><b>Startup:</b> {esc(_startup_display(l))}</li>
            <li><b>Level / mode:</b> {esc(l.level or '—')} · {esc(l.work_mode or 'unknown')}</li>
            <li><b>Location:</b> {esc(l.location or '—')}</li>
            <li class="{conf_class}"><b>Posted:</b> {esc(l.posted_display)} ({esc(l.date_confidence)} — {esc(l.date_source)})</li>
            <li><b>Deadline:</b> {esc(l.deadline_display)}</li>
            <li class="{live_class}"><b>Live check:</b> {esc(_live_display(l))}</li>
            <li><b>Source:</b> {esc(_sources_display(l))}</li>
            <li><b>Apply:</b> {apply}</li>
            <li><b>Tech stack:</b> {reqs}</li>
            <li><b>Eligibility:</b> {esc(l.eligibility_reason)}</li>
            <li><b>Rationale:</b> {esc(l.match_rationale)}</li>
            <li><b>Matched:</b> {matched}</li>
            <li><b>Missing:</b> {missing}</li>
          </ul>
        </div>"""

    diff_html = ""
    if diff is not None:
        diff_html = f"<p class='diff'>Since last run: <b>{len(diff[0])}</b> new · <b>{len(diff[1])}</b> no longer listed</p>"

    verified_cards = "".join(card(i, l) for i, l in enumerate(verified, 1)) or "<p><i>None this run.</i></p>"
    unverified_cards = "".join(card(i, l) for i, l in enumerate(unverified, 1)) or "<p><i>None this run.</i></p>"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Internship Finder — Results</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background:#f6f7f9; color:#1c1e21; }}
  header {{ background:#0b3d2e; color:#fff; padding:20px 28px; }}
  header h1 {{ margin:0 0 6px; font-size:22px; }}
  .meta {{ color:#cfe3da; font-size:14px; }}
  main {{ max-width: 980px; margin: 0 auto; padding: 20px 16px 60px; }}
  h2 {{ border-bottom:2px solid #d9dde2; padding-bottom:6px; margin-top:32px; }}
  .card {{ background:#fff; border:1px solid #e2e5ea; border-radius:10px; padding:14px 18px; margin:14px 0; box-shadow:0 1px 2px rgba(0,0,0,.04); }}
  .card-head {{ font-size:17px; margin-bottom:4px; }}
  .rank {{ display:inline-block; min-width:22px; height:22px; line-height:22px; text-align:center; background:#0b3d2e; color:#fff; border-radius:50%; font-size:12px; margin-right:8px; }}
  .company {{ font-weight:700; }}
  .score {{ float:right; background:#eef4f1; color:#0b3d2e; border-radius:12px; padding:1px 10px; font-size:13px; font-weight:600; }}
  .desc {{ color:#566; font-style:italic; margin:4px 0 8px; }}
  ul {{ list-style:none; padding:0; margin:0; columns: 2; column-gap: 28px; }}
  li {{ font-size:13.5px; margin:3px 0; break-inside: avoid; }}
  .conf-verified {{ color:#0a7d33; }} .conf-approx {{ color:#b86b00; }} .conf-unverified {{ color:#a11; font-weight:600; }}
  .live-ok {{ color:#0a7d33; }} .live-unknown {{ color:#b86b00; }} .live-dead {{ color:#a11; }} .live-skip {{ color:#888; }}
  .diff {{ background:#fff7e6; border:1px solid #ffe1a8; padding:8px 12px; border-radius:8px; }}
  .warn {{ color:#8a6d00; }}
</style></head>
<body>
<header>
  <h1>Internship Finder — Results</h1>
  <div class="meta">Generated {now} · posted ≤ {fr.get('recency_days',21)}d · deadline ≤ {fr.get('deadline_lookahead_days',14)}d ·
   {len(verified)} verified-date · {len(unverified)} unverified-date</div>
</header>
<main>
  {diff_html}
  <h2>✅ Verified-fresh listings (date confirmed)</h2>
  {verified_cards}
  <h2 class="warn">⚠️ Unverified-date listings (included, date unconfirmed)</h2>
  {unverified_cards}
</main>
</body></html>"""
