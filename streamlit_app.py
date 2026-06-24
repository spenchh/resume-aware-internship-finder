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
for _key in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "SERPAPI_API_KEY", "GITHUB_TOKEN"):
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

      html, body, .stApp { overflow-x:clip; }
      .stApp { background:var(--bg); color:var(--text); }
      .block-container {
        width:min(92vw, 96rem);
        max-width:calc(100vw - clamp(1rem, 4vw, 3rem));
        padding:clamp(1.1rem, 3vw, 2.35rem) clamp(.75rem, 2.4vw, 1.75rem) 3rem;
        margin-inline:auto;
      }

      /* Hero */
      .hero { text-align:left; margin:.15rem 0 1.05rem; }
      .hero h1 { font-size:2.35rem; font-weight:760; letter-spacing:0;
                 color:var(--text); margin-bottom:.45rem; }
      .hero p { color:var(--muted); font-size:1rem; max-width:44rem; margin:.2rem 0 0; line-height:1.68; }
      .hero p b { color:var(--text); font-weight:680; }

      /* Panels (upload + result cards) — flat surface, thin border */
      div[data-testid="stVerticalBlockBorderWrapper"] {
        border-radius:8px !important; border:1px solid var(--line) !important;
        background:var(--surface); box-shadow:none; }
      div[data-testid="stHorizontalBlock"] {
        align-items:stretch;
        flex-wrap:wrap !important;
        gap:.72rem !important;
      }
      div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
        width:auto !important;
        min-width:min(100%, 13.5rem) !important;
        flex:1 1 13.5rem !important;
      }

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
      div[data-baseweb="select"]>div, .stTextInput input, .stNumberInput input, .stFileUploader section {
        border-radius:7px !important; }
      div[data-baseweb="select"]>div, .stTextInput input, .stNumberInput input {
        background:var(--surface-3) !important; border-color:var(--line) !important; color:var(--text) !important;
        min-width:0 !important; }
      div[data-baseweb="select"] { min-width:0 !important; }
      div[data-baseweb="select"] span, div[data-baseweb="select"] div {
        min-width:0 !important; overflow-wrap:anywhere; }
      .stFileUploader section {
        border:1px dashed var(--line-strong); background:var(--surface-3);
        min-height:auto; }
      .stFileUploader section * { max-width:100%; }
      label[data-baseweb="radio"] > div:first-child {
        background:transparent !important; border:1px solid var(--line-strong) !important; box-shadow:none !important; }
      label[data-baseweb="radio"] > div:first-child > div {
        background:transparent !important; }
      label[data-baseweb="radio"]:has(input[type="radio"]:checked) > div:first-child {
        background:var(--accent) !important; border-color:var(--accent-strong) !important; }
      label[data-baseweb="radio"]:has(input[type="radio"]:checked) > div:first-child > div {
        background:var(--accent-ink) !important; }
      label[data-baseweb="radio"] p { color:var(--text) !important; }

      /* Quiet utility rows */
      .privacy { color:var(--muted-2); font-size:.82rem; text-align:left; margin:.5rem 0 0; }
      .coverage { color:var(--muted-2); font-size:.82rem; line-height:1.55; margin:.15rem 0 1rem; }
      .coverage b { color:var(--text); font-weight:680; }
      .coverage .ok { color:var(--accent-strong); }
      .coverage .warn { color:var(--warn); }

      /* Score badge + status pills on result cards */
      .badge { display:inline-block; background:#1D221C; color:var(--accent-strong); font-weight:680;
               border:1px solid #3A4636; border-radius:6px; padding:.12rem .55rem; font-size:.82rem; }
      .pill  { display:inline-block; border-radius:6px; padding:.1rem .5rem; font-size:.74rem;
               margin-right:.35rem; border:1px solid var(--line); color:var(--muted); background:var(--surface-3); }
      .legend { font-size:.8rem; color:var(--muted); }
      .company { font-weight:680; font-size:1.05rem; color:var(--text); }
      .title   { color:var(--muted); font-size:.95rem; }
      @media (min-width: 1180px) {
        .block-container { width:min(88vw, 96rem); }
      }
      @media (max-width: 760px) {
        .block-container {
          width:calc(100vw - 1rem);
          max-width:calc(100vw - 1rem);
          padding-top:1.2rem;
        }
        .hero h1 { font-size:2rem; }
      }
      @media (max-width: 420px) {
        .block-container { padding-left:.5rem; padding-right:.5rem; }
        .hero h1 { font-size:1.85rem; }
        .hero p { font-size:.96rem; }
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
      <p>Upload resume, pick a few search settings, then get <b>fresh, still-open</b>
         internship leads sorted by fit.</p>
    </div>
    """,
    unsafe_allow_html=True,
)


# ----------------------------------------------------------------- run pipeline
def _run(data: bytes, filename: str, *, term, target_role, remote_preference,
         include_startups, recency_days, deadline_days, live_check, llm_mode, llm_provider) -> None:
    suffix = Path(filename).suffix or ".txt"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        resume_path = tmp.name

    config = load_config("config.yaml")
    apply_overrides(config, {
        "search.term": term,
        "search.target_role": target_role,
        "search.remote_preference": remote_preference,
        "search.include_startups": include_startups,
        "sources.yc_jobs.enabled": include_startups,
        "sources.serpapi_google_jobs.startup_breadth": include_startups,
        "freshness.recency_days": recency_days,
        "freshness.deadline_lookahead_days": deadline_days,
        "freshness.live_check": live_check,
        "matching.use_llm": llm_mode,
        "matching.llm_provider": llm_provider,
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
_AI_OPTIONS = {
    "Auto: open-weight first, then Claude": ("auto", "auto"),
    "Open-weight GLM 5.2": ("always", "openrouter"),
    "Claude": ("always", "anthropic"),
    "Keyword only": ("never", "auto"),
}
_WORK_MODE_OPTIONS = {
    "Any work mode": "any",
    "Remote": "remote",
    "Hybrid": "hybrid",
    "On-site": "onsite",
}
_CUSTOM_TERM = "Custom term..."
_CUSTOM_FOCUS = "Custom focus..."
_ROLE_FOCUS_OPTIONS = {
    "Infer from resume": "",
    _CUSTOM_FOCUS: "",
    "Business / finance": "business finance accounting investment analyst",
    "Marketing / communications": "marketing communications social media brand",
    "Design / product": "design UX product creative",
    "Data / analytics": "data analytics research business intelligence",
    "Healthcare / life sciences": "healthcare biology public health life sciences",
    "Policy / nonprofit": "policy nonprofit government public affairs",
    "Research / lab": "research lab undergraduate",
    "Software / tech": "software computer science technology",
    "Engineering / hardware": "engineering hardware mechanical electrical",
}
_RECENCY_OPTIONS = {
    "Last 7 days": 7,
    "Last 14 days": 14,
    "Last 21 days": 21,
    "Last 30 days": 30,
    "Last 60 days": 60,
    "Custom...": None,
}
_DEADLINE_OPTIONS = {
    "Next 7 days": 7,
    "Next 14 days": 14,
    "Next 30 days": 30,
    "Next 60 days": 60,
    "Next 90 days": 90,
    "Custom...": None,
}
_LIVE_CHECK_OPTIONS = {
    "Verify links": True,
    "Skip link checks": False,
}
_SOURCE_MIX_OPTIONS = {
    "General internships": False,
    "General + startup add-on": True,
}


def _term_options(today: date) -> list[str]:
    seasons = [("Spring", 1), ("Summer", 5), ("Fall", 8), ("Winter", 11)]
    options = ["Any term", _CUSTOM_TERM]
    for year in range(today.year, today.year + 3):
        for season, month in seasons:
            if year == today.year and month < today.month:
                continue
            options.append(f"{season} {year}")
    return options


def _selected_days(choice: str, options: dict[str, int | None], *, label: str, default: int) -> int:
    value = options[choice]
    if value is not None:
        return value
    return int(st.number_input(label, 1, 365, default, 1))

with st.container(border=True):
    st.markdown("#### 1 · Your resume")
    upload = st.file_uploader("Drop a PDF, DOCX, or TXT", type=["pdf", "docx", "doc", "txt", "md"],
                              label_visibility="collapsed")
    st.markdown(
        '<p class="privacy">Your resume is processed only for this search and is '
        "not saved after the run.</p>", unsafe_allow_html=True)

    st.markdown("#### 2 · Search setup")
    broad_web_on = bool(os.getenv("SERPAPI_API_KEY"))
    open_weight_on = bool(os.getenv("OPENROUTER_API_KEY"))
    term_col, focus_col, mode_col, source_col = st.columns(4)
    term_choice = term_col.selectbox(
        "Target term",
        _term_options(TODAY),
        index=0,
        help="Choose Any term to search without a term preset.",
    )
    focus_choice = focus_col.selectbox(
        "Search focus",
        list(_ROLE_FOCUS_OPTIONS),
        index=0,
        help="Choose Infer from resume to avoid preset field bias.",
    )
    work_mode_choice = mode_col.selectbox(
        "Work mode",
        list(_WORK_MODE_OPTIONS),
        index=0,
        help="Used in general web queries. You can still filter final results below.",
    )
    source_mix_choice = source_col.selectbox(
        "Source mix",
        list(_SOURCE_MIX_OPTIONS),
        index=0,
        help="General internships searches normal postings first. Startup add-on adds YC and startup-specific queries.",
    )
    include_startups = _SOURCE_MIX_OPTIONS[source_mix_choice]
    startup_status = "on" if include_startups else "off"
    startup_class = "ok" if include_startups else ""
    st.markdown(
        f"""
        <p class="coverage">
          General web <b class="{'ok' if broad_web_on else 'warn'}">{'on' if broad_web_on else 'needs key'}</b>
          · Startup add-on <b class="{startup_class}">{startup_status}</b>
          · Preset boards <b>off</b>
          · Open-weight AI <b class="{'ok' if open_weight_on else 'warn'}">{'GLM 5.2' if open_weight_on else 'needs key'}</b>
        </p>
        """,
        unsafe_allow_html=True,
    )

    term = "" if term_choice == "Any term" else term_choice
    if term_choice == _CUSTOM_TERM:
        term = st.text_input(
            "Custom target term",
            placeholder="e.g. Fall 2026, Summer 2027, rolling...",
        ).strip()

    target_role = _ROLE_FOCUS_OPTIONS[focus_choice]
    if focus_choice == _CUSTOM_FOCUS:
        target_role = st.text_input(
            "Custom role, field, or keyword focus",
            placeholder="e.g. fashion merchandising, sports analytics, urban planning...",
            help="This drives search and ranking. Leave blank only if you want resume-only inference.",
        ).strip()
    remote_preference = _WORK_MODE_OPTIONS[work_mode_choice]

    with st.expander("Advanced search options"):
        adv1, adv2 = st.columns(2)
        recency_choice = adv1.selectbox("Posted within", list(_RECENCY_OPTIONS), index=2)
        deadline_choice = adv2.selectbox("Deadline within", list(_DEADLINE_OPTIONS), index=1)
        recency_days = _selected_days(
            recency_choice, _RECENCY_OPTIONS, label="Custom posted-within days", default=21)
        deadline_days = _selected_days(
            deadline_choice, _DEADLINE_OPTIONS, label="Custom deadline window days", default=14)
        live_choice = st.selectbox(
            "Live checks",
            list(_LIVE_CHECK_OPTIONS),
            index=0,
            help="Verify links is slower, but avoids showing dead postings.",
        )
        live_check = _LIVE_CHECK_OPTIONS[live_choice]
        ai_choice = st.selectbox(
            "AI match scoring",
            list(_AI_OPTIONS),
            index=0,
            help="Auto uses OpenRouter's open-weight GLM 5.2 if OPENROUTER_API_KEY is set, "
                 "then Claude if ANTHROPIC_API_KEY is set, then keyword scoring.",
        )
        llm_mode, llm_provider = _AI_OPTIONS[ai_choice]

    run_clicked = st.button("Find internships", type="primary", use_container_width=True,
                            disabled=upload is None)

if run_clicked and upload is not None:
    _run(upload.getvalue(), upload.name, term=term, target_role=target_role,
         remote_preference=remote_preference, include_startups=include_startups,
         recency_days=recency_days, deadline_days=deadline_days,
         live_check=live_check, llm_mode=llm_mode, llm_provider=llm_provider)


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
