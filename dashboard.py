"""Optional Streamlit dashboard (spec Section 9) — interactive filtering/sorting
on top of the same backend.

It reads ``reports/latest.json`` (written by every `main.py` run), so run the
finder first, then:  streamlit run dashboard.py
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import streamlit as st

REPORT = Path("reports/latest.json")
TODAY = datetime.now(timezone.utc).date()

st.set_page_config(page_title="Internship Finder", layout="wide")
st.title("🔎 Internship Finder")

if not REPORT.exists():
    st.warning("No results yet. Run:  `python main.py --resume your_resume.pdf`  then reload.")
    st.stop()

data = json.loads(REPORT.read_text(encoding="utf-8"))
listings: list[dict] = data.get("listings", [])
st.caption(f"Loaded {len(listings)} listings · generated {data.get('generated', '?')}")


def _to_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _days_to_deadline(l):
    d = _to_date(l.get("deadline"))
    if d and not l.get("deadline_is_rolling"):
        return (d - TODAY).days
    return 10**6  # rolling / none sort last


# ----- sidebar filters -----
sb = st.sidebar
sb.header("Filters")
query = sb.text_input("Search company / title").strip().lower()
min_score = sb.slider("Minimum match score", 0, 100, 0, 5)

def _opts(key):
    return sorted({(l.get(key) or "—") for l in listings})

sources_sel = sb.multiselect("Source", _opts_src := sorted({l.get("source", "").split(":", 1)[0] for l in listings}))
mode_sel = sb.multiselect("Work mode", _opts("work_mode"))
conf_sel = sb.multiselect("Date confidence", _opts("date_confidence"))
live_sel = sb.multiselect("Live status", _opts("live_status"))
deadline_only = sb.checkbox("Only with an upcoming deadline")
sort_by = sb.selectbox("Sort by", ["Deadline urgency", "Match score", "Posted date"])


def keep(l: dict) -> bool:
    if query and query not in (l.get("company", "") + " " + l.get("title", "")).lower():
        return False
    if l.get("match_score", 0) < min_score:
        return False
    if sources_sel and l.get("source", "").split(":", 1)[0] not in sources_sel:
        return False
    if mode_sel and (l.get("work_mode") or "—") not in mode_sel:
        return False
    if conf_sel and (l.get("date_confidence") or "—") not in conf_sel:
        return False
    if live_sel and (l.get("live_status") or "—") not in live_sel:
        return False
    if deadline_only and _days_to_deadline(l) > 365:
        return False
    return True


rows = [l for l in listings if keep(l)]
if sort_by == "Match score":
    rows.sort(key=lambda l: -l.get("match_score", 0))
elif sort_by == "Posted date":
    rows.sort(key=lambda l: (_to_date(l.get("posted_date")) or date.min), reverse=True)
else:
    rows.sort(key=_days_to_deadline)

# ----- summary -----
c1, c2, c3 = st.columns(3)
c1.metric("Showing", len(rows))
c2.metric("Verified date", sum(1 for l in rows if l.get("date_confidence") == "verified"))
c3.metric("Live-confirmed", sum(1 for l in rows if l.get("live_status") == "live"))

_CONF_ICON = {"verified": "🟢", "approximate": "🟡", "unverified": "🔴"}
_LIVE_ICON = {"live": "✅", "unknown": "⚠️", "dead": "❌", "not_checked": "—"}

# ----- listings -----
for l in rows:
    conf = l.get("date_confidence", "unverified")
    dl = "rolling" if l.get("deadline_is_rolling") else (l.get("deadline") or "—")
    header = (f"{_CONF_ICON.get(conf,'')} {l.get('match_score',0):>3}/100 · "
              f"{l.get('company','?')} — {l.get('title','?')}  ·  deadline: {dl}")
    with st.expander(header):
        left, right = st.columns([3, 2])
        with left:
            st.markdown(f"**{l.get('company','')}** — {l.get('company_description','') or '_no description_'}")
            st.markdown(f"- **Location:** {l.get('location') or '—'}  ·  **Mode:** {l.get('work_mode') or 'unknown'}")
            st.markdown(f"- **Level:** {l.get('level') or '—'}  ·  **Startup:** "
                        f"{'Yes' if l.get('is_startup') else ('No' if l.get('is_startup') is False else 'Unknown')}"
                        f"{(' · ' + l['funding_stage']) if l.get('funding_stage') else ''}")
            reqs = ", ".join(l.get("requirements", []) or []) or "—"
            st.markdown(f"- **Tech stack:** {reqs}")
            st.markdown(f"- **Rationale:** {l.get('match_rationale','')}")
            if l.get("matched_keywords"):
                st.markdown(f"- **Matched:** {', '.join(l['matched_keywords'])}")
            if l.get("missing_keywords"):
                st.markdown(f"- **Missing:** {', '.join(l['missing_keywords'])}")
        with right:
            st.markdown(f"**Posted:** {l.get('posted_date') or '[unverified]'}  "
                        f"({conf} — {l.get('date_source','')})")
            st.markdown(f"**Live:** {_LIVE_ICON.get(l.get('live_status'),'')} "
                        f"{l.get('live_status','')} — {l.get('live_reason','')}")
            st.markdown(f"**Source:** {l.get('source','')}")
            if l.get("apply_url"):
                st.link_button("Apply ↗", l["apply_url"])
