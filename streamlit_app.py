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

st.set_page_config(page_title="Internship Finder", layout="wide")
load_env()


# ----------------------------------------------------------------- styling
st.markdown(
    """
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

      /* Matte dark palette: charcoal surfaces, warm off-white text, sage accent. */
      :root {
        --bg:#0C0D0E;
        --surface:#141518;
        --surface-2:#191B1E;
        --surface-3:#101113;
        --line:#2A2D31;
        --line-strong:#373A3F;
        --text:#EEECE7;
        --muted:#A7A29A;
        --muted-2:#7F7B74;
        --accent:#9AA894;
        --accent-strong:#B6C1AE;
        --accent-ink:#151912;
        --warn:#C2A66D;
        --risk:#C9857F;
      }
      html, body, .stApp, button, input, textarea, select,
      [class*="css"] { font-family:'Inter', sans-serif !important; }

      html, body, .stApp { overflow-x:hidden; }
      .stApp { background:var(--bg); color:var(--text); }
      .block-container {
        width:min(100%, 70rem);
        max-width:min(70rem, calc(100vw - 2rem));
        padding:2.1rem clamp(.75rem, 2.5vw, 1.5rem) 3rem;
        margin-inline:auto;
      }

      /* Hero */
      .hero { text-align:left; margin:.15rem 0 1.05rem; }
      .hero h1 { font-size:2.35rem; font-weight:760; letter-spacing:0;
                 color:var(--text); margin-bottom:.45rem; }
      .hero p { color:var(--muted); font-size:1rem; max-width:44rem; margin:.2rem 0 0; line-height:1.68; }
      .hero p b { color:var(--text); font-weight:680; }

      /* Benefit chips — flat, thin border */
      .chips { display:flex; gap:.55rem; justify-content:flex-start; flex-wrap:wrap; margin:1rem 0 .7rem; }
      .chip { background:var(--surface-3); border:1px solid var(--line); border-radius:7px; padding:.52rem .8rem;
              font-size:.84rem; color:var(--muted); }
      .chip b { color:var(--text); font-weight:680; }

      /* Panels (upload + result cards) — flat surface, thin border */
      div[data-testid="stVerticalBlockBorderWrapper"] {
        border-radius:8px !important; border:1px solid var(--line) !important;
        background:var(--surface); box-shadow:none; }

      /* Buttons — flat */
      .stButton>button, .stDownloadButton>button, .stLinkButton>a {
        border-radius:7px !important; font-weight:680; border:1px solid var(--line-strong);
        background:var(--surface-2); color:var(--text); min-height:2.55rem; }
      .stButton>button:hover, .stDownloadButton>button:hover, .stLinkButton>a:hover {
        border-color:var(--accent); color:var(--text); background:#1E211F; }
      .stButton>button[kind="primary"] {
        background:var(--accent); border:1px solid var(--accent); color:var(--accent-ink); }
      .stButton>button[kind="primary"]:hover {
        background:var(--accent-strong); border-color:var(--accent-strong); color:var(--accent-ink); }

      /* Inputs */
      div[data-baseweb="select"]>div, .stTextInput input, .stFileUploader section {
        border-radius:7px !important; }
      div[data-baseweb="select"]>div, .stTextInput input {
        background:var(--surface-3) !important; border-color:var(--line) !important; color:var(--text) !important; }
      .stFileUploader section { border:1px dashed var(--line-strong); background:var(--surface-3); }

      /* Privacy note — quiet trust row */
      .privacy { color:var(--muted-2); font-size:.82rem; text-align:left; margin:.5rem 0 0; }
      .search-meta {
        display:grid;
        grid-template-columns:repeat(auto-fit, minmax(min(100%, 13rem), 1fr));
        gap:.55rem;
        margin:.55rem 0 1rem;
      }
      .search-meta .item { border:1px solid var(--line); border-radius:7px; background:var(--surface-3); padding:.58rem .68rem; }
      .search-meta span { display:block; color:var(--muted-2); font-size:.74rem; line-height:1.2; }
      .search-meta b { display:block; color:var(--text); font-size:.9rem; line-height:1.35; margin-top:.12rem; }
      .search-meta b.ok { color:var(--accent-strong); }
      .search-meta b.warn { color:var(--warn); }

      /* Score badge + status pills on result cards */
      .badge { display:inline-block; background:#1D221C; color:var(--accent-strong); font-weight:680;
               border:1px solid #3A4636; border-radius:6px; padding:.12rem .55rem; font-size:.82rem; }
      .pill  { display:inline-block; border-radius:6px; padding:.1rem .5rem; font-size:.74rem;
               margin-right:.35rem; border:1px solid var(--line); color:var(--muted); background:var(--surface-3); }
      .legend { font-size:.8rem; color:var(--muted); }
      .company { font-weight:680; font-size:1.05rem; color:var(--text); }
      .title   { color:var(--muted); font-size:.95rem; }
      @media (min-width: 1180px) {
        .block-container { width:min(76vw, 70rem); }
      }
      @media (max-width: 760px) {
        .block-container { max-width:calc(100vw - 1rem); padding-top:1.2rem; }
        .hero h1 { font-size:2rem; }
      }
      @media (max-width: 420px) {
        .block-container { padding-left:.5rem; padding-right:.5rem; }
        .hero h1 { font-size:1.85rem; }
        .hero p { font-size:.96rem; }
        .chip { padding:.48rem .7rem; }
      }
      footer, #MainMenu, [data-testid="stToolbar"] { visibility:hidden; }
      header[data-testid="stHeader"] { background:transparent; }
    </style>
    """,
    unsafe_allow_html=True,
)

# Status shown as colored text — no emoji. (label, color)
_CONF_TEXT = {
    "verified": ("verified", "#B6C1AE"),
    "approximate": ("approx.", "#C2A66D"),
    "unverified": ("unverified", "#C9857F"),
}
_LIVE_TEXT = {
    "live": ("live", "#B6C1AE"),
    "unknown": ("unconfirmed", "#C2A66D"),
    "dead": ("closed", "#C9857F"),
    "not_checked": ("not checked", "#7F7B74"),
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
      <p>Upload resume. Search broadly. Get <b>fresh, still-open</b>
         internship leads from across the web, then sort by fit.</p>
    </div>
    <div class="chips">
      <div class="chip"><b>Broad web</b> search</div>
      <div class="chip"><b>No preset</b> field bias</div>
      <div class="chip"><b>Live link</b> checks</div>
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
        with st.spinner("Searching the web and verifying open listings. This can take a minute."):
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
_TERMS = ["Any term / not sure", "Summer 2027", "Fall 2027", "Winter 2027",
          "Spring 2028", "Summer 2028"]

with st.container(border=True):
    st.markdown("#### 1 · Your resume")
    upload = st.file_uploader("Drop a PDF, DOCX, or TXT", type=["pdf", "docx", "doc", "txt", "md"],
                              label_visibility="collapsed")
    st.markdown(
        '<p class="privacy">Your resume is processed only for this search and is '
        "not saved after the run.</p>", unsafe_allow_html=True)

    st.markdown("#### 2 · What are you looking for?")
    broad_web_on = bool(os.getenv("SERPAPI_API_KEY"))
    st.markdown(
        f"""
        <div class="search-meta">
          <div class="item"><span>Broad web</span><b class="{'ok' if broad_web_on else 'warn'}">{'on' if broad_web_on else 'needs key'}</b></div>
          <div class="item"><span>Preset boards</span><b>off</b></div>
          <div class="item"><span>Startup jobs</span><b>YC public profiles</b></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    c1, c2 = st.columns(2)
    term = c1.selectbox("Target term", _TERMS, index=0)
    target_role = c2.text_input(
        "Role, field, or keyword focus",
        placeholder="e.g. marketing, finance, UX design, healthcare, policy...",
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
    f"Found **{len(listings)}** internships in {res['seconds']:.0f}s "
    f"· scanned {res['total_fetched']} raw listings · {len(res['new'])} new since your last run."
)

# Legend so the status colors are never something to guess at.
st.markdown(
    '<p class="legend">Date: '
    + _status("verified", "#B6C1AE") + " / " + _status("approx.", "#C2A66D")
    + " / " + _status("unverified", "#C9857F")
    + " &nbsp;|&nbsp; Live status: "
    + _status("live", "#B6C1AE") + " / " + _status("unconfirmed", "#C2A66D")
    + " / " + _status("closed", "#C9857F") + "</p>",
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
