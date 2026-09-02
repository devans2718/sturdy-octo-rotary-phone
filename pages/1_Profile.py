"""Experience bank: create, edit, duplicate and delete profiles and entries."""

from __future__ import annotations

import json

import streamlit as st

from jobhunt.agent import parse_resume
from jobhunt.db import DEFAULT_DB
from jobhunt.llm import LLMError
from jobhunt.models import ITEM_KINDS, Profile, ProfileItem
from jobhunt.scoring.deterministic import DEFAULT_WEIGHTS
from jobhunt.state import as_list, as_text, get_db, get_llm, sidebar

st.set_page_config(page_title="Profile", page_icon="👤", layout="wide")
db = get_db(DEFAULT_DB)
profile = sidebar(db)

st.title("👤 Master profile")

# ------------------------------------------------------------- profile CRUD
with st.expander("Manage profiles", expanded=profile is None):
    new_col, copy_col, delete_col = st.columns(3)

    with new_col:
        st.markdown("**Create**")
        new_name = st.text_input("Name", key="new_profile_name", placeholder="e.g. Data roles (UK)")
        if st.button("Create profile", width="stretch", disabled=not new_name.strip()):
            try:
                new_id = db.create_profile(Profile(name=new_name.strip()))
                st.session_state["active_profile_id"] = new_id
                st.rerun()
            except Exception as exc:
                st.error(f"Could not create: {exc}")

    with copy_col:
        st.markdown("**Duplicate**")
        st.caption("Fork the active profile to tailor it for a different market.")
        copy_name = st.text_input("New name", key="copy_profile_name", placeholder="e.g. Same CV, ML slant")
        if st.button("Duplicate active", width="stretch", disabled=not (profile and copy_name.strip())):
            new_id = db.duplicate_profile(profile.id, copy_name.strip())
            st.session_state["active_profile_id"] = new_id
            st.rerun()

    with delete_col:
        st.markdown("**Delete**")
        st.caption("Removes the profile, its entries, scores and tracker rows.")
        confirm = st.checkbox("I'm sure", key="confirm_delete_profile")
        if st.button("Delete active profile", type="secondary", width="stretch", disabled=not (profile and confirm)):
            db.delete_profile(profile.id)
            st.session_state.pop("active_profile_id", None)
            st.rerun()

if profile is None:
    st.info("Create a profile to get started.")
    st.stop()

basics_tab, bank_tab, prefs_tab, io_tab = st.tabs(
    ["Basics", f"Experience bank ({len(profile.items)})", "Search preferences", "Import / export"]
)

# ----------------------------------------------------------------- basics
with basics_tab:
    with st.form("basics"):
        name = st.text_input("Profile name", profile.name)
        headline = st.text_input("Headline", profile.headline, placeholder="Senior data engineer · Python, dbt, Airflow")
        location = st.text_input("Based in", profile.location, placeholder="Manchester, UK")
        summary = st.text_area("Professional summary", profile.summary, height=160)
        if st.form_submit_button("Save basics", type="primary"):
            profile.name, profile.headline, profile.location, profile.summary = name, headline, location, summary
            db.update_profile(profile)
            st.success("Saved.")
            st.rerun()

    if profile.items:
        st.markdown("##### Rendered master CV")
        st.caption("This is exactly what gets sent to the AI agent.")
        st.code(profile.as_markdown(), language="markdown")

# --------------------------------------------------------- experience bank
with bank_tab:
    st.caption(
        "One entry per role, degree, project, achievement or skill group. "
        "Highlights are the reusable bullets the agent pulls from when tailoring."
    )

    with st.expander("➕ Add an entry"):
        with st.form("add_item", clear_on_submit=True):
            c1, c2 = st.columns(2)
            kind = c1.selectbox("Type", ITEM_KINDS)
            sort_order = c2.number_input("Sort order", value=0, step=1)
            c1, c2 = st.columns(2)
            title = c1.text_input("Title", placeholder="Senior Data Engineer / BSc Computer Science / Python")
            org = c2.text_input("Organisation", placeholder="Acme Ltd / University of X")
            c1, c2, c3 = st.columns(3)
            item_location = c1.text_input("Location")
            start_date = c2.text_input("Start", placeholder="2021-03")
            end_date = c3.text_input("End", placeholder="Present")
            description = st.text_area("Description", height=100)
            highlights = st.text_area("Highlights (one per line)", height=120)
            tags = st.text_input("Tags / technologies (comma separated)")
            if st.form_submit_button("Add entry", type="primary", disabled=False):
                if not title.strip():
                    st.error("A title is required.")
                else:
                    db.upsert_item(
                        ProfileItem(
                            profile_id=profile.id,
                            kind=kind,
                            title=title.strip(),
                            org=org.strip(),
                            location=item_location.strip(),
                            start_date=start_date.strip(),
                            end_date=end_date.strip(),
                            description=description.strip(),
                            highlights=as_list(highlights),
                            tags=as_list(tags),
                            sort_order=int(sort_order),
                        )
                    )
                    st.rerun()

    if not profile.items:
        st.info("No entries yet. Add one above, or paste a CV in the Import tab.")

    for kind in ITEM_KINDS:
        group = [i for i in profile.items if i.kind == kind]
        if not group:
            continue
        st.markdown(f"#### {kind.title()} ({len(group)})")
        for item in group:
            dates = " – ".join(x for x in [item.start_date, item.end_date] if x)
            label = " · ".join(x for x in [item.title, item.org, dates] if x)
            with st.expander(label or f"{kind} #{item.id}"):
                with st.form(f"item_{item.id}"):
                    c1, c2 = st.columns(2)
                    e_kind = c1.selectbox("Type", ITEM_KINDS, index=ITEM_KINDS.index(item.kind), key=f"k{item.id}")
                    e_order = c2.number_input("Sort order", value=item.sort_order, step=1, key=f"o{item.id}")
                    c1, c2 = st.columns(2)
                    e_title = c1.text_input("Title", item.title, key=f"t{item.id}")
                    e_org = c2.text_input("Organisation", item.org, key=f"g{item.id}")
                    c1, c2, c3 = st.columns(3)
                    e_loc = c1.text_input("Location", item.location, key=f"l{item.id}")
                    e_start = c2.text_input("Start", item.start_date, key=f"s{item.id}")
                    e_end = c3.text_input("End", item.end_date, key=f"e{item.id}")
                    e_desc = st.text_area("Description", item.description, height=100, key=f"d{item.id}")
                    e_high = st.text_area("Highlights (one per line)", as_text(item.highlights), height=120, key=f"h{item.id}")
                    e_tags = st.text_input("Tags", ", ".join(item.tags), key=f"g2{item.id}")

                    save_col, delete_col = st.columns([3, 1])
                    if save_col.form_submit_button("Save", type="primary"):
                        item.kind, item.title, item.org = e_kind, e_title, e_org
                        item.location, item.start_date, item.end_date = e_loc, e_start, e_end
                        item.description = e_desc
                        item.highlights = as_list(e_high)
                        item.tags = as_list(e_tags)
                        item.sort_order = int(e_order)
                        db.upsert_item(item)
                        st.rerun()
                    if delete_col.form_submit_button("Delete"):
                        db.delete_item(item.id)
                        st.rerun()

# ------------------------------------------------------------ preferences
with prefs_tab:
    prefs = dict(profile.preferences or {})
    st.caption("These drive the deterministic scorer and the hard filters, and are shown to the AI agent.")

    with st.form("prefs"):
        st.markdown("##### What you're looking for")
        c1, c2 = st.columns(2)
        target_titles = c1.text_area(
            "Target job titles (one per line)", as_text(prefs.get("target_titles")), height=110
        )
        target_locations = c2.text_area(
            "Target locations (one per line)", as_text(prefs.get("target_locations")), height=110
        )
        c1, c2, c3 = st.columns(3)
        remote_pref = c1.selectbox(
            "Work mode",
            ["any", "remote", "hybrid", "onsite"],
            index=["any", "remote", "hybrid", "onsite"].index(prefs.get("remote_preference", "any")),
        )
        seniority_options = ["", "intern", "junior", "mid", "senior", "staff", "lead"]
        target_seniority = c2.selectbox(
            "Target seniority",
            seniority_options,
            index=seniority_options.index(prefs.get("target_seniority", "")),
        )
        target_salary = c3.number_input(
            "Target salary (annual)", value=float(prefs.get("target_salary") or 0), step=5000.0, min_value=0.0
        )

        st.markdown("##### Hard filters — these remove postings entirely")
        c1, c2 = st.columns(2)
        must_have = c1.text_area("Must contain (one per line)", as_text(prefs.get("must_have_keywords")), height=90)
        exclude_keywords = c2.text_area("Must NOT contain", as_text(prefs.get("exclude_keywords")), height=90)
        c1, c2, c3 = st.columns(3)
        exclude_companies = c1.text_area("Blocked companies", as_text(prefs.get("exclude_companies")), height=90)
        hard_min_salary = c2.number_input(
            "Absolute salary floor", value=float(prefs.get("hard_min_salary") or 0), step=5000.0, min_value=0.0
        )
        max_age_days = c3.number_input(
            "Max posting age (days, 0 = any)", value=int(prefs.get("max_age_days") or 0), step=7, min_value=0
        )
        remote_required = st.checkbox("Drop on-site-only roles", value=bool(prefs.get("remote_required")))

        st.markdown("##### Scoring weights")
        st.caption("Relative importance of each deterministic component. They're normalised, so only ratios matter.")
        weights = {**DEFAULT_WEIGHTS, **(prefs.get("weights") or {})}
        weight_cols = st.columns(len(DEFAULT_WEIGHTS))
        new_weights = {}
        for col, key in zip(weight_cols, DEFAULT_WEIGHTS):
            new_weights[key] = col.slider(key, 0.0, 1.0, float(weights.get(key, 0.0)), 0.02)
        ai_weight = st.slider(
            "AI weight in the final score",
            0.0,
            1.0,
            float(prefs.get("ai_weight", 0.5)),
            0.05,
            help="0 = ignore the agent's score entirely, 1 = the agent decides. Postings the agent hasn't reviewed keep their deterministic score.",
        )

        if st.form_submit_button("Save preferences", type="primary"):
            profile.preferences = {
                "target_titles": as_list(target_titles),
                "target_locations": as_list(target_locations),
                "remote_preference": remote_pref,
                "target_seniority": target_seniority,
                "target_salary": target_salary or None,
                "must_have_keywords": as_list(must_have),
                "exclude_keywords": as_list(exclude_keywords),
                "exclude_companies": as_list(exclude_companies),
                "hard_min_salary": hard_min_salary or None,
                "max_age_days": max_age_days or None,
                "remote_required": remote_required,
                "weights": new_weights,
                "ai_weight": ai_weight,
            }
            db.update_profile(profile)
            st.success("Saved. Re-run scoring on the Search page to apply.")

# -------------------------------------------------------- import / export
with io_tab:
    st.markdown("#### Build the bank from a CV")
    st.caption("Paste plain text. The agent structures it into entries you can then edit by hand.")
    resume_text = st.text_area("CV / résumé text", height=240, key="resume_text")
    replace = st.checkbox("Replace existing entries", value=False)
    if st.button("Parse with the AI agent", type="primary", disabled=not resume_text.strip()):
        llm, error = get_llm(db)
        if error:
            st.error(f"Provider unavailable: {error}")
        elif llm.name == "offline":
            st.warning("The offline provider can't parse a CV. Pick a real provider in Settings.")
        else:
            with st.spinner("Reading your CV…"):
                try:
                    parsed = parse_resume(llm, resume_text)
                except LLMError as exc:
                    st.error(str(exc))
                    parsed = None
            if parsed:
                if replace:
                    for existing in profile.items:
                        db.delete_item(existing.id)
                profile.headline = parsed.get("headline") or profile.headline
                profile.summary = parsed.get("summary") or profile.summary
                profile.location = parsed.get("location") or profile.location
                db.update_profile(profile)
                added = 0
                for index, record in enumerate(parsed.get("items", [])):
                    if not record.get("title"):
                        continue
                    db.upsert_item(
                        ProfileItem(
                            profile_id=profile.id,
                            kind=record.get("kind", "other"),
                            title=record.get("title", ""),
                            org=record.get("org", ""),
                            location=record.get("location", ""),
                            start_date=record.get("start_date", ""),
                            end_date=record.get("end_date", ""),
                            description=record.get("description", ""),
                            highlights=record.get("highlights", []) or [],
                            tags=record.get("tags", []) or [],
                            sort_order=index,
                        )
                    )
                    added += 1
                st.success(f"Added {added} entries. Review them in the Experience bank tab.")
                st.rerun()

    st.divider()
    st.markdown("#### Export / import JSON")
    export = {
        "name": profile.name,
        "headline": profile.headline,
        "summary": profile.summary,
        "location": profile.location,
        "preferences": profile.preferences,
        "items": [
            {k: v for k, v in item.__dict__.items() if k not in {"id", "profile_id"}} for item in profile.items
        ],
    }
    st.download_button(
        "Download this profile as JSON",
        data=json.dumps(export, indent=2),
        file_name=f"{profile.name.replace(' ', '_').lower()}_profile.json",
        mime="application/json",
    )

    uploaded = st.file_uploader("Import a profile JSON", type=["json"])
    if uploaded is not None and st.button("Import as a new profile"):
        try:
            payload = json.load(uploaded)
            imported = Profile(
                name=payload.get("name", "Imported") + " (imported)",
                headline=payload.get("headline", ""),
                summary=payload.get("summary", ""),
                location=payload.get("location", ""),
                preferences=payload.get("preferences", {}) or {},
            )
            new_id = db.create_profile(imported)
            for index, record in enumerate(payload.get("items", [])):
                record.pop("id", None)
                record.pop("profile_id", None)
                db.upsert_item(ProfileItem(profile_id=new_id, sort_order=index, **record))
            st.session_state["active_profile_id"] = new_id
            st.success("Imported.")
            st.rerun()
        except Exception as exc:
            st.error(f"Could not import: {exc}")
