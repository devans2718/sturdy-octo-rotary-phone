"""Job Hunt Copilot — Streamlit entry point.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import streamlit as st

from jobhunt.db import DEFAULT_DB
from jobhunt.state import active_profile, get_db, llm_config, sidebar

st.set_page_config(page_title="Job Hunt Copilot", page_icon="🎯", layout="wide")

db = get_db(DEFAULT_DB)
profile = sidebar(db)

st.title("🎯 Job Hunt Copilot")
st.caption(
    "A master profile, multi-source job harvesting, and a hybrid "
    "deterministic + AI ranking pass — with a swappable model backend."
)

profiles = db.list_profiles()
sources = db.list_sources()
postings = db.list_postings(limit=5000)
scored = db.scored_rows(profile.id) if profile else []
ai_done = sum(1 for r in scored if r.get("ai_score") is not None)

col1, col2, col3, col4 = st.columns(4)
col1.metric("Profiles", len(profiles))
col2.metric("Sources", f"{sum(1 for s in sources if s['enabled'])} / {len(sources)}", help="enabled / total")
col3.metric("Postings", len(postings))
col4.metric("AI-reviewed", ai_done)

st.divider()

left, right = st.columns([3, 2])

with left:
    st.subheader("Start here")
    st.markdown(
        """
1. **👤 Profile** — build your experience bank (roles, education, projects, achievements,
   skills). Paste a CV and let the agent structure it, or add entries by hand. You can keep
   several profiles and switch between them in the sidebar.
2. **🔌 Sources** — add job boards. ATS boards (Greenhouse, Lever, Ashby, Workable) give the
   cleanest data; aggregators give reach; a **company careers page** source handles anything
   else, using structured data when it exists and the AI agent when it doesn't.
3. **🔎 Search** — harvest postings, apply your hard filters, and score everything
   deterministically. Then send the top N to the AI agent for a real assessment.
4. **📊 Matches** — rank, inspect the score breakdown, read the agent's advice, and track
   applications through the pipeline.
5. **⚙️ Settings** — point the app at Anthropic, OpenAI, a local Ollama/LM Studio model, or
   any OpenAI-compatible endpoint. Tune the scoring weights here too.
"""
    )

    if profile:
        gaps = []
        if not profile.items:
            gaps.append("your experience bank is empty")
        if not (profile.preferences or {}).get("target_titles"):
            gaps.append("no target job titles set")
        if not postings:
            gaps.append("no postings harvested yet")
        if gaps:
            st.warning("Next step: " + "; ".join(gaps) + ".")

with right:
    st.subheader("How ranking works")
    st.markdown(
        """
**Deterministic (free, runs on everything)**
- hard filters: excluded keywords/companies, salary floor, remote requirement, age
- skill overlap against your experience bank
- TF-IDF similarity between your profile and the posting
- target-title, seniority, location, salary and recency fit

**AI agent (paid, runs on the shortlist)**
- reads the full posting against your master CV
- fit score, strengths, concrete gaps, red flags, seniority read
- keywords to mirror and suggested bullets drawn from your real experience

The two are blended with a weight you control, so you can run this fully offline,
fully AI-led, or anywhere in between.
"""
    )
    config = llm_config(db)
    st.info(f"Current model backend: **{config.provider}** · `{config.model}`")

st.divider()
st.caption(
    "Be considerate with sources: requests are rate-limited per host and identify themselves. "
    "Check a site's terms and robots.txt before pointing the careers-page scraper at it."
)
