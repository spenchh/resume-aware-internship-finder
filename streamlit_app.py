"""Resume-Aware Internship Finder — web app (Streamlit).

The whole tool in the browser: upload a resume, tell it what roles you want,
and the same backend that powers the CLI parses it, searches
date-reliable sources, **live-verifies every listing is still open**, scores the
matches to YOUR background (any field — not just engineering), and shows them as
scannable job cards. Nothing to install.

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

st.set_page_config(page_title="Internship Finder", layout="centered")
load_env()


# ----------------------------------------------------------------- styling
st.markdown(
    """
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Lato:wght@400;700;900&display=swap');

      /* ---- Flat dark palette --------------------------------------------
         bg #0E1117 · surface #161B22 · raised #1C2230 · border #2A313C
         text #E6E8EB · muted #9BA3AF · accent #2F81F7
         live #3FB950 · approx #D29922 · risk #F85149                       */
      html, body, .stApp, button, input, textarea, select,
      [class*="css"] { font-family:'Lato', sans-serif !important; }

      .stApp { background:#0E1117; }
      .block-container { padding-top: 2.2rem; max-width: 820px; }

      /* Hero */
      .hero { text-align:center; margin-bottom: 1.1rem; }
      .hero h1 { font-size: 2.1rem; font-weight: 900; letter-spacing:-.01em;
                 color:#E6E8EB; margin-bottom:.3rem; }
      .hero p { color:#9BA3AF; font-size:1.0rem; max-width:560px; margin:.2rem auto 0; }
      .hero p b { color:#E6E8EB; font-weight:700; }

      /* Benefit chips — flat, thin border */
      .chips { display:flex; gap:.5rem; justify-content:center; flex-wrap:wrap; margin:1rem 0 .4rem; }
      .chip { background:#161B22; border:1px solid #2A313C; border-radius:8px; padding:.5rem .85rem;
              font-size:.85rem; color:#9BA3AF; }
      .chip b { color:#E6E8EB; font-weight:700; }

      /* Panels (upload + result cards) — flat surface, thin border */
      div[data-testid="stVerticalBlockBorderWrapper"] {
        border-radius:10px !important; border:1px solid #2A313C !important; background:#161B22; }

      /* Buttons — flat */
      .stButton>button, .stDownloadButton>button, .stLinkButton>a {
        border-radius:8px !important; font-weight:700; border:1px solid #2A313C;
        background:#1C2230; color:#E6E8EB; }
      .stButton>button:hover, .stDownloadButton>button:hover, .stLinkButton>a:hover {
        border-color:#3D4757; color:#E6E8EB; }
      .stButton>button[kind="primary"] {
        background:#2F81F7; border:1px solid #2F81F7; color:#fff; }
      .stButton>button[kind="primary"]:hover { background:#388BFD; border-color:#388BFD; color:#fff; }

      /* Inputs */
      div[data-baseweb="select"]>div, .stTextInput input, .stFileUploader section {
        border-radius:8px !important; }
      .stFileUploader section { border:1px dashed #2A313C; background:#0E1117; }

      /* Privacy note — quiet trust row */
      .privacy { color:#6E7681; font-size:.82rem; text-align:center; margin:.5rem 0 0; }

      /* Score badge + status pills on result cards */
      .badge { display:inline-block; background:#1C2230; color:#E6E8EB; font-weight:700;
               border:1px solid #2A313C; border-radius:6px; padding:.12rem .55rem; font-size:.82rem; }
      .pill  { display:inline-block; border-radius:6px; padding:.1rem .5rem; font-size:.74rem;
               margin-right:.35rem; border:1px solid #2A313C; color:#9BA3AF; background:#0E1117; }
      .legend { font-size:.8rem; color:#9BA3AF; }
      .company { font-weight:700; font-size:1.05rem; color:#E6E8EB; }
      .title   { color:#9BA3AF; font-size:.95rem; }
      footer, #MainMenu { visibility:hidden; }
    </style>
    """,
    unsafe_allow_html=True,
)

# Status shown as colored text — no emoji. (label, color)
_CONF_TEXT = {
    "verified": ("verified", "#3FB950"),
    "approximate": ("approx.", "#D29922"),
    "unverified": ("unverified", "#F85149"),
}
_LIVE_TEXT = {
    "live": ("live", "#3FB950"),
    "unknown": ("unconfirmed", "#D29922"),
    "dead": ("closed", "#F85149"),
    "not_checked": ("not checked", "#6E7681"),
}


def _status(text: str, color: str) -> str:
    return f'<span style="color:{color};font-weight:700">{text}</span>'


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
    return 10**6


# ----------------------------------------------------------------- hero
st.markdown(
    """
    <div class="hero">
      <h1>Internship Finder</h1>
      <p>Upload resume. Set your target. Get <b>verified, still-open</b>
         internship leads — matched to your background, in any field.</p>
    </div>
    <div class="chips">
      <div class="chip"><b>Live-checked</b> roles</div>
      <div class="chip"><b>Resume-based</b> scoring</div>
      <div class="chip"><b>Freshness-first</b> results</div>
    </div>
    """,
    unsafe_allow_html=True,
)


# ----------------------------------------------------------------- run pipeline
def _run(data: bytes, filename: str, *, term, target_role, recency_days,
         deadline_days, live_check, llm_mode) -> None:
    suffix = Path(filename).suffix or ".txt"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        resume_path = tmp.name

    config = load_config("config.yaml")
    apply_overrides(config, {
        "search.term": term,
        "search.target_role": target_role,
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
        with st.spinner("Working… verifying every listing is still open. This can take a minute."):
            result = run_pipeline(
                resume_path, config,
                cache_path=os.path.join(tempfile.gettempdir(), "internfinder_cache.db"),
                progress=_progress,
            )
        out_dir = Path(tempfile.gettempdir()) / "internfinder_reports"
        report_generator.generate(
            result.reported, result.profile, config,
            diff=(result.new_keys, result.closed_keys), out_dir=out_dir,
        )
        st.session_state["result"] = {
            "listings": [l.to_dict() for l in result.reported],
            "profile": result.profile.to_dict(),
            "new": result.new_keys,
            "total_fetched": result.total_fetched,
            "seconds": result.seconds,
            "out_dir": str(out_dir),
        }
    finally:
        bar.empty()
        try:
            os.unlink(resume_path)
        except OSError:
            pass


# ----------------------------------------------------------------- input panel
_TERMS = ["Summer 2027", "Fall 2027", "Winter 2027", "Spring 2028", "Summer 2028",
          "Any term / not sure"]

with st.container(border=True):
    st.markdown("#### 1 · Your resume")
    upload = st.file_uploader("Drop a PDF, DOCX, or TXT", type=["pdf", "docx", "doc", "txt", "md"],
                              label_visibility="collapsed")
    st.markdown(
        '<p class="privacy">Your resume is used only for this search and is '
        "not stored on any server.</p>", unsafe_allow_html=True)

    st.markdown("#### 2 · What are you looking for?")
    c1, c2 = st.columns(2)
    term = c1.selectbox("Target term", _TERMS, index=0)
    target_role = c2.text_input(
        "Roles / field you're targeting",
        placeholder="e.g. marketing, UX design, finance, robotics…",
        help="Any field — this drives the search and ranks matches. "
             "Leave blank and we'll infer it from your resume.",
    )

    with st.expander("Advanced search options"):
        a1, a2 = st.columns(2)
        recency_days = a1.slider("Posted within (days)", 7, 60, 21, 1)
        deadline_days = a2.slider("Deadline within (days)", 7, 60, 14, 1)
        live_check = st.checkbox(
            "Verify every listing is still open (recommended)", value=True,
            help="Re-checks each posting's link live before showing it. Slower, but no dead links.")
        llm_mode = st.selectbox(
            "AI match scoring (Claude)", ["auto", "always", "never"], index=0,
            help="'auto' uses Claude only if an ANTHROPIC_API_KEY is configured; "
                 "otherwise it falls back to keyword scoring.")

    if term == "Any term / not sure":
        term = ""

    run_clicked = st.button("Find internships", type="primary", use_container_width=True,
                            disabled=upload is None)

if run_clicked and upload is not None:
    _run(upload.getvalue(), upload.name, term=term, target_role=target_role,
         recency_days=recency_days, deadline_days=deadline_days,
         live_check=live_check, llm_mode=llm_mode)


# ----------------------------------------------------------------- results
res = st.session_state.get("result")
if not res:
    st.stop()

listings: list[dict] = res["listings"]
st.markdown("### Results")
st.success(
    f"Found **{len(listings)}** matching internships in {res['seconds']:.0f}s "
    f"· scanned {res['total_fetched']} raw listings · {len(res['new'])} new since your last run."
)

# Legend so the status colors are never something to guess at.
st.markdown(
    '<p class="legend">Date: '
    + _status("verified", "#3FB950") + " / " + _status("approx.", "#D29922")
    + " / " + _status("unverified", "#F85149")
    + " &nbsp;|&nbsp; Live status: "
    + _status("live", "#3FB950") + " / " + _status("unconfirmed", "#D29922")
    + " / " + _status("closed", "#F85149") + "</p>",
    unsafe_allow_html=True)

# ----- downloads -----
out_dir = Path(res["out_dir"])
d1, d2, d3 = st.columns(3)
for col, name, label, mime in [
    (d1, "latest.md", "Markdown", "text/markdown"),
    (d2, "latest.html", "Web page", "text/html"),
    (d3, "latest.json", "JSON", "application/json"),
]:
    f = out_dir / name
    if f.exists():
        col.download_button(label, f.read_bytes(), file_name=name, mime=mime,
                            use_container_width=True)

# ----- filters -----
with st.expander("Filter & sort"):
    fc1, fc2, fc3 = st.columns([2, 1, 1])
    query = fc1.text_input("Search company / title").strip().lower()
    min_score = fc2.slider("Min match", 0, 100, 0, 5)
    sort_by = fc3.selectbox("Sort by", ["Match score", "Deadline urgency", "Posted date"])

    def _opts(key):
        return sorted({(l.get(key) or "—") for l in listings})
    g1, g2 = st.columns(2)
    mode_sel = g1.multiselect("Work mode", _opts("work_mode"))
    live_sel = g2.multiselect("Live status", _opts("live_status"))
    deadline_only = st.checkbox("Only listings with an upcoming deadline")


def keep(l: dict) -> bool:
    if query and query not in (l.get("company", "") + " " + l.get("title", "")).lower():
        return False
    if l.get("match_score", 0) < min_score:
        return False
    if mode_sel and (l.get("work_mode") or "—") not in mode_sel:
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

m1, m2, m3 = st.columns(3)
m1.metric("Showing", len(rows))
m2.metric("Verified date", sum(1 for l in rows if l.get("date_confidence") == "verified"))
m3.metric("Live-confirmed", sum(1 for l in rows if l.get("live_status") == "live"))

st.write("")

# ----- job cards -----
if not rows:
    st.warning("No listings match the current filters.")

for l in rows:
    conf = l.get("date_confidence", "unverified")
    live = l.get("live_status", "not_checked")
    dl = "rolling" if l.get("deadline_is_rolling") else (l.get("deadline") or "—")
    with st.container(border=True):
        top_l, top_r = st.columns([5, 1])
        top_l.markdown(
            f'<span class="company">{l.get("company","?")}</span><br>'
            f'<span class="title">{l.get("title","?")}</span>', unsafe_allow_html=True)
        top_r.markdown(f'<div style="text-align:right"><span class="badge">'
                       f'{l.get("match_score",0)}/100</span></div>', unsafe_allow_html=True)

        loc = l.get("location") or "—"
        mode = l.get("work_mode") or "unknown"
        conf_label, conf_color = _CONF_TEXT.get(conf, (conf, "#9BA3AF"))
        live_label, live_color = _LIVE_TEXT.get(live, (live.replace("_", " "), "#9BA3AF"))
        st.markdown(
            f'<span class="pill">{loc}</span>'
            f'<span class="pill">{mode}</span>'
            f'<span class="pill">deadline {dl} · {_status(conf_label, conf_color)}</span>'
            f'<span class="pill">{_status(live_label, live_color)}</span>',
            unsafe_allow_html=True)

        ba, bb = st.columns([1, 3])
        if l.get("apply_url"):
            ba.link_button("Apply", l["apply_url"], use_container_width=True)

        with st.expander("Details — why it matches, skills, source"):
            if l.get("company_description"):
                st.caption(l["company_description"])
            st.markdown(f"**Why it matches:** {l.get('match_rationale','—')}")
            if l.get("matched_keywords"):
                st.markdown(f"**Matched skills:** {', '.join(l['matched_keywords'])}")
            if l.get("missing_keywords"):
                st.markdown(f"**Missing / nice-to-have:** {', '.join(l['missing_keywords'])}")
            reqs = ", ".join(l.get("requirements", []) or []) or "—"
            st.markdown(f"**Listed requirements:** {reqs}")
            st.markdown(
                f"**Posted:** {l.get('posted_date') or '[unverified]'} "
                f"({conf} — {l.get('date_source','') or 'no date source'})  \n"
                f"**Live check:** {live.replace('_',' ')} "
                f"— {l.get('live_reason','') or 'n/a'}  \n"
                f"**Source:** {l.get('source','')}")
