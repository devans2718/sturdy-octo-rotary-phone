"""Harvest postings, score them deterministically, then run the AI pass."""

from __future__ import annotations

import streamlit as st

from jobhunt.db import DEFAULT_DB
from jobhunt.scoring import ai_pass, harvest, rescore
from jobhunt.state import get_db, get_llm, llm_config, sidebar

st.set_page_config(page_title="Search", page_icon="🔎", layout="wide")
db = get_db(DEFAULT_DB)
profile = sidebar(db)

st.title("🔎 Search")

if profile is None:
    st.warning("Create a profile first.")
    st.stop()

sources = db.list_sources(enabled_only=True)
if not sources:
    st.warning("No enabled sources. Add some on the Sources page.")
    st.stop()

# ------------------------------------------------------------- step 1: fetch
st.subheader("1 · Harvest")
c1, c2 = st.columns([3, 1])
query = c1.text_input(
    "Keyword filter",
    value=st.session_state.get("last_query", ""),
    placeholder='e.g. python "data engineer"   — quoted phrases stay together',
    help="Passed to boards that support server-side search; applied locally to the rest. Leave empty to take everything.",
)
limit = c2.number_input("Max per source", value=60, min_value=5, max_value=500, step=10)

chosen = st.multiselect(
    "Sources to run",
    options=[s["id"] for s in sources],
    default=[s["id"] for s in sources],
    format_func=lambda i: next(s["label"] for s in sources if s["id"] == i),
)

if st.button("Harvest now", type="primary", disabled=not chosen):
    st.session_state["last_query"] = query
    llm, llm_error = get_llm(db)
    bar = st.progress(0.0, text="Starting…")
    report = harvest(
        db,
        profile,
        query=query,
        limit_per_source=int(limit),
        source_ids=chosen,
        llm=llm,
        progress=lambda message, fraction: bar.progress(min(fraction, 1.0), text=message),
    )
    bar.empty()
    st.session_state["harvest_report"] = report

    with st.spinner("Scoring…"):
        scored_count = rescore(db, profile)
    st.success(f"Fetched {report.fetched}, stored {report.stored}, filtered out {report.filtered}. Scored {scored_count} postings.")

report = st.session_state.get("harvest_report")
if report:
    with st.expander("Harvest detail", expanded=bool(report.errors)):
        st.markdown("**Per source**")
        st.table([{"source": k, "result": v} for k, v in report.per_source.items()])
        if report.errors:
            st.markdown("**Errors**")
            for error in report.errors:
                st.error(error)
        if report.rejections:
            st.markdown(f"**Filtered out ({len(report.rejections)})** — your hard filters removed these:")
            st.dataframe(
                [{"posting": p, "reason": r} for p, r in report.rejections[:200]],
                width="stretch",
                hide_index=True,
            )

st.divider()

# ------------------------------------------------------- step 2: rescore only
st.subheader("2 · Deterministic scoring")
st.caption(
    "Free and instant. Re-run this after editing your profile or preferences — no network calls, "
    "no model usage."
)
if st.button("Re-score everything"):
    with st.spinner("Scoring…"):
        count = rescore(db, profile)
    st.success(f"Re-scored {count} postings.")

rows = db.scored_rows(profile.id)
if rows:
    top = [r for r in rows if r.get("final") is not None][:10]
    st.dataframe(
        [
            {
                "score": r.get("final"),
                "title": r["title"],
                "company": r["company"],
                "location": r["location"],
                "AI": "✓" if r.get("ai_score") is not None else "",
            }
            for r in top
        ],
        width="stretch",
        hide_index=True,
    )

st.divider()

# ------------------------------------------------------------ step 3: AI pass
st.subheader("3 · AI assessment")
config = llm_config(db)
st.caption(
    f"Runs the agent (**{config.provider}** · `{config.model}`) over the top-ranked postings — "
    "one call each, so start small."
)

if config.provider == "offline":
    st.info("The offline provider is selected. Choose a real provider in Settings to enable this step.")
elif not rows:
    st.info("Nothing to assess yet — harvest some postings first.")
else:
    unreviewed = [r for r in rows if r.get("ai_score") is None]
    c1, c2 = st.columns([1, 3])
    count = c1.number_input("How many", value=min(10, max(len(unreviewed), 1)), min_value=1, max_value=100, step=5)
    only_new = c2.checkbox("Skip postings the agent has already reviewed", value=True)

    pool = unreviewed if only_new else rows
    target_ids = [r["id"] for r in pool[: int(count)]]
    st.caption(f"{len(unreviewed)} of {len(rows)} postings are unreviewed. Will assess {len(target_ids)}.")

    if st.button("Run AI assessment", type="primary", disabled=not target_ids):
        llm, llm_error = get_llm(db)
        if llm_error:
            st.error(f"Provider unavailable: {llm_error}")
        else:
            bar = st.progress(0.0, text="Starting…")
            done, errors = ai_pass(
                db,
                profile,
                llm,
                target_ids,
                progress=lambda message, fraction: bar.progress(min(fraction, 1.0), text=message),
            )
            bar.empty()
            st.success(f"Assessed {done} postings. See the Matches page for the write-ups.")
            for error in errors:
                st.warning(error)

st.divider()
with st.expander("Danger zone"):
    st.caption("Postings are shared across profiles; scores and tracker state are per profile.")
    if st.checkbox("Yes, delete every harvested posting") and st.button("Clear posting database"):
        db.clear_postings()
        st.session_state.pop("harvest_report", None)
        st.rerun()
