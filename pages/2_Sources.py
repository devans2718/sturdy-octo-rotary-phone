"""Job source management: add, configure, test and toggle boards."""

from __future__ import annotations

import streamlit as st

from jobhunt.db import DEFAULT_DB
from jobhunt.sources import SOURCES, build_source
from jobhunt.state import get_db, get_llm, sidebar

st.set_page_config(page_title="Sources", page_icon="🔌", layout="wide")
db = get_db(DEFAULT_DB)
sidebar(db)

st.title("🔌 Job sources")
st.caption(
    "Three tiers, deliberately: company ATS boards are first-party and clean, aggregators "
    "give reach, and the careers-page adapter handles everything that fits neither."
)

# ------------------------------------------------------------------- add new
with st.expander("➕ Add a source", expanded=not db.list_sources()):
    kind = st.selectbox(
        "Source type",
        list(SOURCES),
        format_func=lambda k: f"{SOURCES[k].spec.label}  ·  {SOURCES[k].spec.help[:70]}",
    )
    spec = SOURCES[kind].spec
    st.info(spec.help)
    if spec.needs_llm:
        st.caption("⚡ Falls back to the AI agent when the page has no structured data.")

    with st.form(f"add_source_{kind}", clear_on_submit=True):
        label = st.text_input("Label", placeholder=f"{spec.label}: …")
        config: dict = {}
        for field in spec.fields:
            key = field["name"]
            if field.get("type") == "bool":
                config[key] = st.checkbox(field["label"], value=False, help=field.get("help"))
            elif field.get("secret"):
                config[key] = st.text_input(field["label"], type="password", help=field.get("help"))
            else:
                config[key] = st.text_input(field["label"], help=field.get("help"))
        if st.form_submit_button("Add source", type="primary"):
            missing = [f["label"] for f in spec.fields if f.get("required") and not config.get(f["name"])]
            if missing:
                st.error("Required: " + ", ".join(missing))
            else:
                clean = {k: v for k, v in config.items() if v not in ("", None)}
                db.add_source(label.strip() or spec.label, kind, clean)
                st.rerun()

st.divider()

# ------------------------------------------------------------------ existing
sources = db.list_sources()
if not sources:
    st.info("No sources yet.")
    st.stop()

for source in sources:
    spec = SOURCES[source["kind"]].spec if source["kind"] in SOURCES else None
    status = source.get("last_status") or "never run"
    icon = "✅" if source["enabled"] else "⏸️"
    with st.expander(f"{icon}  {source['label']}  ·  _{source['kind']}_  —  {status[:80]}"):
        if spec is None:
            st.error(f"Unknown source type `{source['kind']}` — the adapter may have been removed.")
            if st.button("Delete", key=f"del_unknown_{source['id']}"):
                db.delete_source(source["id"])
                st.rerun()
            continue

        with st.form(f"edit_source_{source['id']}"):
            label = st.text_input("Label", source["label"])
            config = dict(source["config"])
            for field in spec.fields:
                key = field["name"]
                current = config.get(key, "")
                if field.get("type") == "bool":
                    config[key] = st.checkbox(field["label"], value=bool(current), key=f"{key}{source['id']}")
                elif field.get("secret"):
                    config[key] = st.text_input(
                        field["label"], value=str(current), type="password", key=f"{key}{source['id']}"
                    )
                else:
                    config[key] = st.text_input(field["label"], value=str(current), key=f"{key}{source['id']}")
            enabled = st.checkbox("Enabled", value=bool(source["enabled"]), key=f"en{source['id']}")

            save_col, test_col, delete_col = st.columns([2, 2, 1])
            save = save_col.form_submit_button("Save", type="primary")
            test = test_col.form_submit_button("Test fetch (5 postings)")
            delete = delete_col.form_submit_button("Delete")

        if save:
            db.update_source(
                source["id"],
                label=label,
                config={k: v for k, v in config.items() if v not in ("", None)},
                enabled=enabled,
            )
            st.rerun()
        if delete:
            db.delete_source(source["id"])
            st.rerun()
        if test:
            llm, _ = get_llm(db)
            with st.spinner("Fetching…"):
                try:
                    adapter = build_source(source["kind"], source["config"], llm=llm)
                    sample = list(adapter.fetch(limit=5))[:5]
                except Exception as exc:
                    st.error(f"{type(exc).__name__}: {exc}")
                    sample = []
            if sample:
                st.success(f"Got {len(sample)} postings.")
                st.dataframe(
                    [
                        {
                            "title": p.title,
                            "company": p.company,
                            "location": p.location,
                            "remote": p.remote,
                            "salary": f"{p.salary_min or ''}–{p.salary_max or ''} {p.salary_currency}".strip(" –"),
                            "posted": p.posted_at[:10],
                            "url": p.url,
                        }
                        for p in sample
                    ],
                    width="stretch",
                    hide_index=True,
                )
            elif not st.session_state.get("_error_shown"):
                st.warning("No postings returned. Check the token/URL, or the board may be empty.")
