"""Resume-Aware Internship Finder — web app (Streamlit).

This is the *whole tool in the browser*: upload a resume, click a button, and the
same backend that powers the CLI parses it, searches date-reliable sources,
**live-verifies every listing is still open**, scores matches, and shows them
here — nothing to install.

Run locally:   streamlit run streamlit_app.py
Deployed on:   Streamlit Community Cloud (entry file = this file).
"""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import streamlit as st

# --- Make secrets available to the backend BEFORE it loads env / API keys -----
# On Streamlit Cloud you set these under  Settings → Secrets  (TOML format).
# Locally they fall back to a .env file (loaded by the backend) or real env vars.
for _key in ("ANTHROPIC_API_KEY", "SERPAPI_API_KEY", "GITHUB_TOKEN"):
    try:
        if _key in st.secrets and st.secrets[_key]:
            os.environ.setdefault(_key, str(st.secrets[_key]))
    except Exception:
        pass  # no secrets file configured — that's fine, all keys are optional

from internfinder import report_generator
from internfinder.cli import run_pipeline
from internfinder.config import apply_overrides, load_config, load_env

TODAY = datetime.now(timezone.utc).date()

st.set_page_config(page_title="Internship Finder", page_icon="🔎", layout="wide")
load_env()  # local .env, if present (no-op on cloud)


# ----------------------------------------------------------------- helpers
def _to_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _days_to_deadline(l: dict) -> int:
    d = _to_date(l.get("deadline"))
    if d and not l.get("deadline_is_rolling"):
        return (d - TODAY).days
    return 10**6  # rolling / none sort last


_CONF_ICON = {"verified": "🟢", "approximate": "🟡", "unverified": "🔴"}
_LIVE_ICON = {"live": "✅", "unknown": "⚠️", "dead": "❌", "not_checked": "—"}


# ----------------------------------------------------------------- header
st.title("🔎 Resume-Aware Internship Finder")
st.caption(
    "Upload your resume → it searches date-reliable sources, **verifies every "
    "listing is still open**, scores the matches to your skills, and lists them "
    "below. Freshness first: a date is shown as **[unverified]** rather than guessed."
)


# ----------------------------------------------------------------- sidebar / inputs
sb = st.sidebar
sb.header("1 · Your resume")
upload = sb.file_uploader("Upload PDF, DOCX, or TXT", type=["pdf", "docx", "doc", "txt", "md"])

sb.header("2 · Search options")
term = sb.text_input("Target term", value="Summer 2027")
recency_days = sb.slider("Posted within (days)", 7, 60, 21, 1)
deadline_days = sb.slider("Deadline within (days)", 7, 60, 14, 1)
live_check = sb.checkbox(
    "Verify every listing is still open (recommended)", value=True,
    help="Re-checks each posting's URL live before showing it. Slower, but no dead links.",
)
llm_mode = sb.selectbox(
    "AI scoring (Claude)", ["auto", "always", "never"], index=0,
    help="'auto' uses Claude only if an ANTHROPIC_API_KEY is configured; "
         "otherwise it falls back to keyword scoring.",
)

run_clicked = sb.button("🚀 Find internships", type="primary", use_container_width=True,
                        disabled=upload is None)
if upload is None:
    sb.info("Upload a resume to enable the search.")


# ----------------------------------------------------------------- run pipeline
def _run(upload, *, term, recency_days, deadline_days, live_check, llm_mode) -> None:
    suffix = Path(upload.name).suffix or ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(upload.getvalue())
        resume_path = tmp.name

    config = load_config("config.yaml")
    apply_overrides(config, {
        "search.term": term,
        "freshness.recency_days": recency_days,
        "freshness.deadline_lookahead_days": deadline_days,
        "freshness.live_check": live_check,
        "matching.use_llm": llm_mode,
        "output.format": "both",
    })

    bar = st.progress(0.0, text="Starting…")

    def _progress(msg: str, frac):
        bar.progress(min(max(frac or 0.0, 0.0), 1.0), text=msg)

    try:
        with st.spinner("Working… this can take a minute while listings are verified live."):
            result = run_pipeline(
                resume_path, config,
                cache_path=os.path.join(tempfile.gettempdir(), "internfinder_cache.db"),
                progress=_progress,
            )
        # Render report files (markdown/html/json) into a temp dir for download.
        out_dir = Path(tempfile.gettempdir()) / "internfinder_reports"
        report_generator.generate(
            result.reported, result.profile, config,
            diff=(result.new_keys, result.closed_keys), out_dir=out_dir,
        )
        st.session_state["result"] = {
            "listings": [l.to_dict() for l in result.reported],
            "profile": result.profile.to_dict(),
            "new": result.new_keys,
            "closed": result.closed_keys,
            "total_fetched": result.total_fetched,
            "seconds": result.seconds,
            "generated": datetime.now(timezone.utc).isoformat(),
            "out_dir": str(out_dir),
        }
    finally:
        bar.empty()
        try:
            os.unlink(resume_path)
        except OSError:
            pass


if run_clicked and upload is not None:
    _run(upload, term=term, recency_days=recency_days, deadline_days=deadline_days,
         live_check=live_check, llm_mode=llm_mode)


# ----------------------------------------------------------------- results
res = st.session_state.get("result")
if not res:
    st.info("👈 Upload your resume and click **Find internships** to begin.")
    st.stop()

listings: list[dict] = res["listings"]
st.success(
    f"Found {len(listings)} matching internships in {res['seconds']:.0f}s "
    f"(scanned {res['total_fetched']} raw listings · "
    f"{len(res['new'])} new since your last run)."
)

# ----- filters (in main area so they're obvious) -----
with st.expander("Filters & sorting", expanded=True):
    fc1, fc2, fc3 = st.columns([2, 1, 1])
    query = fc1.text_input("Search company / title").strip().lower()
    min_score = fc2.slider("Min match score", 0, 100, 0, 5)
    sort_by = fc3.selectbox("Sort by", ["Match score", "Deadline urgency", "Posted date"])

    def _opts(key):
        return sorted({(l.get(key) or "—") for l in listings})

    gc1, gc2, gc3, gc4 = st.columns(4)
    src_opts = sorted({l.get("source", "").split(":", 1)[0] for l in listings if l.get("source")})
    sources_sel = gc1.multiselect("Source", src_opts)
    mode_sel = gc2.multiselect("Work mode", _opts("work_mode"))
    conf_sel = gc3.multiselect("Date confidence", _opts("date_confidence"))
    live_sel = gc4.multiselect("Live status", _opts("live_status"))
    deadline_only = st.checkbox("Only listings with an upcoming deadline")


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
if sort_by == "Deadline urgency":
    rows.sort(key=_days_to_deadline)
elif sort_by == "Posted date":
    rows.sort(key=lambda l: (_to_date(l.get("posted_date")) or date.min), reverse=True)
else:
    rows.sort(key=lambda l: -l.get("match_score", 0))

# ----- summary metrics -----
m1, m2, m3, m4 = st.columns(4)
m1.metric("Showing", len(rows))
m2.metric("Verified date", sum(1 for l in rows if l.get("date_confidence") == "verified"))
m3.metric("Live-confirmed", sum(1 for l in rows if l.get("live_status") == "live"))
m4.metric("New since last run", len(res["new"]))

# ----- downloads -----
out_dir = Path(res["out_dir"])
dl1, dl2, dl3 = st.columns(3)
for col, name, label, mime in [
    (dl1, "latest.md", "⬇️ Markdown", "text/markdown"),
    (dl2, "latest.html", "⬇️ HTML page", "text/html"),
    (dl3, "latest.json", "⬇️ JSON data", "application/json"),
]:
    f = out_dir / name
    if f.exists():
        col.download_button(label, f.read_bytes(), file_name=name, mime=mime,
                            use_container_width=True)

st.divider()

# ----- listings -----
if not rows:
    st.warning("No listings match the current filters.")
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
            st.markdown(f"- **Why it matches:** {l.get('match_rationale','')}")
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
