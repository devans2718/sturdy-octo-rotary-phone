"""Ranked matches: filter, inspect the score, read the agent, track applications."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from jobhunt.agent import cover_letter, strategy_review
from jobhunt.db import DEFAULT_DB
from jobhunt.llm import LLMError
from jobhunt.models import STATUSES
from jobhunt.scoring.deterministic import DEFAULT_WEIGHTS
from jobhunt.state import get_db, get_llm, sidebar


def _salary_label(row: dict) -> str:
    """Compact salary range, or '' when the posting didn't disclose one."""
    low, high = row.get("salary_min"), row.get("salary_max")
    if not low and not high:
        return ""
    currency = row.get("salary_currency") or ""
    if low and high and low != high:
        return f"{low:,.0f}–{high:,.0f} {currency}".strip()
    return f"{(low or high):,.0f} {currency}".strip()


st.set_page_config(page_title="Matches", page_icon="📊", layout="wide")
db = get_db(DEFAULT_DB)
profile = sidebar(db)

st.title("📊 Matches")

if profile is None:
    st.warning("Create a profile first.")
    st.stop()

rows = db.scored_rows(profile.id)
if not rows:
    st.info("No postings yet. Head to the Search page and harvest some.")
    st.stop()

# --------------------------------------------------------------- filter bar
c1, c2, c3, c4 = st.columns([2, 2, 2, 2])
text_filter = c1.text_input("Filter text", placeholder="title, company or location")
status_filter = c2.multiselect("Status", STATUSES, default=[s for s in STATUSES if s != "archived"])
min_score = c3.slider("Min score", 0, 100, 0, 5)
sort_by = c4.selectbox("Sort by", ["final score", "deterministic", "AI score", "newest", "company"])

filtered = []
for row in rows:
    if row["status"] not in status_filter:
        continue
    if (row.get("final") or 0) < min_score:
        continue
    if text_filter:
        blob = f"{row['title']} {row['company']} {row['location']}".lower()
        if text_filter.lower() not in blob:
            continue
    filtered.append(row)

sort_keys = {
    "final score": lambda r: -(r.get("final") or 0),
    "deterministic": lambda r: -(r.get("deterministic") or 0),
    "AI score": lambda r: -(r.get("ai_score") or -1),
    "newest": lambda r: r.get("posted_at") or "",
    "company": lambda r: (r.get("company") or "").lower(),
}
filtered.sort(key=sort_keys[sort_by], reverse=sort_by == "newest")

st.caption(f"{len(filtered)} of {len(rows)} postings shown.")

table_tab, detail_tab, advice_tab = st.tabs(["Table", "Detail & advice", "Search strategy"])

# -------------------------------------------------------------------- table
with table_tab:
    frame = pd.DataFrame(
        [
            {
                "id": r["id"],
                "score": r.get("final"),
                "det": r.get("deterministic"),
                "ai": r.get("ai_score"),
                "priority": (r.get("ai") or {}).get("apply_priority", ""),
                "title": r["title"],
                "company": r["company"],
                "location": r["location"],
                "mode": r["remote"],
                "salary": _salary_label(r),
                "posted": (r.get("posted_at") or "")[:10],
                "status": r["status"],
                "url": r["url"],
            }
            for r in filtered
        ]
    )
    st.dataframe(
        frame,
        width="stretch",
        hide_index=True,
        column_config={
            "url": st.column_config.LinkColumn("link", display_text="open"),
            "score": st.column_config.ProgressColumn("score", min_value=0, max_value=100, format="%.0f"),
            "id": None,
        },
    )
    st.download_button(
        "Download as CSV",
        data=frame.to_csv(index=False),
        file_name="job_matches.csv",
        mime="text/csv",
    )

# ------------------------------------------------------------------- detail
with detail_tab:
    if not filtered:
        st.info("Nothing matches the current filters.")
    else:
        options = {r["id"]: f"{(r.get('final') or 0):.0f} · {r['title']} @ {r['company']}" for r in filtered}
        chosen_id = st.selectbox("Posting", list(options), format_func=lambda i: options[i])
        row = next(r for r in filtered if r["id"] == chosen_id)

        head, meta = st.columns([3, 1])
        with head:
            st.markdown(f"### {row['title']}")
            st.markdown(f"**{row['company']}** · {row['location'] or '—'} · {row['remote'] or 'mode unspecified'}")
            if row["url"]:
                st.markdown(f"[Open the posting ↗]({row['url']})")
            st.caption(f"Source: {row['source']} · posted {(row.get('posted_at') or 'unknown')[:10]}")
        with meta:
            st.metric("Final", f"{row.get('final') or 0:.0f}")
            st.metric("Deterministic", f"{row.get('deterministic') or 0:.0f}")
            st.metric("AI", f"{row['ai_score']:.0f}" if row.get("ai_score") is not None else "—")

        # ---- tracker
        c1, c2 = st.columns([1, 3])
        status = c1.selectbox("Status", STATUSES, index=STATUSES.index(row["status"]), key=f"st{row['id']}")
        notes = c2.text_input("Notes", row.get("notes", ""), key=f"nt{row['id']}")
        if st.button("Save tracker", key=f"sv{row['id']}"):
            db.set_status(row["id"], profile.id, status, notes)
            st.rerun()

        # ---- deterministic breakdown
        breakdown = row.get("breakdown") or {}
        if breakdown:
            st.markdown("#### Why it scored that way")
            weights = {**DEFAULT_WEIGHTS, **((profile.preferences or {}).get("weights") or {})}
            total = sum(weights.get(k, 0) for k in breakdown) or 1
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "component": key,
                            "value": value,
                            "weight": round(weights.get(key, 0) / total, 3),
                            "contribution": round(value * weights.get(key, 0) / total * 100, 1),
                        }
                        for key, value in breakdown.items()
                    ]
                ).sort_values("contribution", ascending=False),
                width="stretch",
                hide_index=True,
                column_config={"value": st.column_config.ProgressColumn("value", min_value=0, max_value=1, format="%.2f")},
            )

        # ---- AI verdict
        verdict = row.get("ai") or {}
        st.markdown("#### Agent assessment")
        if not verdict:
            st.info("Not assessed yet — run the AI pass on the Search page, or use the button below.")
            if st.button("Assess this one now", key=f"ai{row['id']}"):
                from jobhunt.scoring import ai_pass

                llm, error = get_llm(db)
                if error:
                    st.error(error)
                else:
                    with st.spinner("Thinking…"):
                        done, errors = ai_pass(db, profile, llm, [row["id"]])
                    for err in errors:
                        st.error(err)
                    if done:
                        st.rerun()
        else:
            priority = verdict.get("apply_priority", "")
            colour = {"now": "🟢", "soon": "🟡", "maybe": "🟠", "skip": "🔴"}.get(priority, "⚪")
            st.markdown(f"{colour} **{priority.upper() or 'UNRATED'}** — {verdict.get('verdict', '')}")
            st.caption(f"Seniority read: {verdict.get('seniority_match', 'unclear')}")

            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Strengths**")
                for point in verdict.get("strengths", []):
                    st.markdown(f"- {point}")
                if verdict.get("keywords_to_mirror"):
                    st.markdown("**Keywords to mirror**")
                    st.markdown(" · ".join(f"`{k}`" for k in verdict["keywords_to_mirror"]))
            with c2:
                st.markdown("**Gaps**")
                for point in verdict.get("gaps", []):
                    st.markdown(f"- {point}")
                if verdict.get("red_flags"):
                    st.markdown("**Red flags**")
                    for flag in verdict["red_flags"]:
                        st.markdown(f"- ⚠️ {flag}")

            if verdict.get("tailoring_advice"):
                st.markdown("**How to tailor**")
                st.write(verdict["tailoring_advice"])
            if verdict.get("suggested_bullets"):
                st.markdown("**Suggested CV bullets**")
                st.code("\n".join(f"• {b}" for b in verdict["suggested_bullets"]))

        # ---- cover letter
        with st.expander("✍️ Draft a cover letter"):
            tone = st.text_input("Tone", "direct and specific", key=f"tn{row['id']}")
            if st.button("Draft it", key=f"cl{row['id']}"):
                llm, error = get_llm(db)
                if error:
                    st.error(error)
                elif llm.name == "offline":
                    st.warning("Pick a real provider in Settings.")
                else:
                    posting = db.get_posting(row["id"])
                    with st.spinner("Writing…"):
                        try:
                            st.session_state[f"letter{row['id']}"] = cover_letter(llm, profile, posting, tone)
                        except LLMError as exc:
                            st.error(str(exc))
            if st.session_state.get(f"letter{row['id']}"):
                st.text_area("Draft", st.session_state[f"letter{row['id']}"], height=320, key=f"la{row['id']}")

        with st.expander("Full posting text"):
            st.text(row["description"][:20000] or "(no description captured)")

# ------------------------------------------------------------------- advice
with advice_tab:
    st.caption("The agent looks across the whole shortlist and critiques the search itself, not individual roles.")
    if st.button("Review my search strategy", type="primary"):
        llm, error = get_llm(db)
        if error:
            st.error(error)
        elif llm.name == "offline":
            st.warning("Pick a real provider in Settings.")
        else:
            with st.spinner("Reviewing…"):
                try:
                    st.session_state["strategy"] = strategy_review(llm, profile, filtered)
                except LLMError as exc:
                    st.error(str(exc))

    strategy = st.session_state.get("strategy")
    if strategy:
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Recurring themes**")
            for theme in strategy.get("themes", []):
                st.markdown(f"- {theme}")
            st.markdown("**Skill gaps that keep appearing**")
            for gap in strategy.get("skill_gaps", []):
                st.markdown(f"- {gap}")
        with c2:
            st.markdown("**Edits to your master profile**")
            for edit in strategy.get("profile_edits", []):
                st.markdown(f"- {edit}")
            st.markdown("**Searches / sources to try**")
            for suggestion in strategy.get("search_suggestions", []):
                st.markdown(f"- {suggestion}")
        if strategy.get("weekly_plan"):
            st.markdown("**Plan for the week**")
            st.info(strategy["weekly_plan"])
