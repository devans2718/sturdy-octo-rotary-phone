"""Streamlit-side glue: cached singletons, active profile, sidebar."""

from __future__ import annotations

from typing import Any

import streamlit as st

from .db import DEFAULT_DB, Database
from .llm import SETTINGS_KEY, LLMConfig, LLMProvider, build_provider, config_from_dict
from .llm.offline import OfflineProvider
from .models import Profile
from .sources import STARTER_SOURCES


@st.cache_resource
def get_db(path: str = DEFAULT_DB) -> Database:
    db = Database(path)
    _seed(db)
    return db


def _seed(db: Database) -> None:
    """First run: one empty profile and a few working sources."""
    if not db.list_profiles():
        db.create_profile(Profile(name="My profile", headline="", summary=""))
    if not db.list_sources():
        for source in STARTER_SOURCES:
            db.add_source(source["label"], source["kind"], source["config"])


def llm_config(db: Database) -> LLMConfig:
    return config_from_dict(db.get_setting(SETTINGS_KEY, {}))


def get_llm(db: Database) -> tuple[LLMProvider, str]:
    """Build the configured provider. Never raises — falls back to offline.

    Returns (provider, error_message). An error message means the UI should
    show a warning but keep the deterministic features working.
    """
    config = llm_config(db)
    try:
        return build_provider(config), ""
    except Exception as exc:
        return OfflineProvider(), f"{type(exc).__name__}: {exc}"


def active_profile(db: Database) -> Profile | None:
    profiles = db.list_profiles()
    if not profiles:
        return None
    selected = st.session_state.get("active_profile_id")
    if selected not in {p.id for p in profiles}:
        selected = profiles[0].id
        st.session_state["active_profile_id"] = selected
    return db.get_profile(selected)


def sidebar(db: Database) -> Profile | None:
    """Profile picker + provider status, shown on every page."""
    profiles = db.list_profiles()
    with st.sidebar:
        st.markdown("### Active profile")
        if not profiles:
            st.info("Create a profile on the Profile page.")
            return None
        names = {p.id: p.name for p in profiles}
        current = st.session_state.get("active_profile_id", profiles[0].id)
        if current not in names:
            current = profiles[0].id
        chosen = st.selectbox(
            "Profile",
            options=list(names),
            format_func=lambda i: names[i],
            index=list(names).index(current),
            label_visibility="collapsed",
        )
        st.session_state["active_profile_id"] = chosen

        config = llm_config(db)
        st.markdown("### AI provider")
        st.caption(f"**{config.provider}** · `{config.model}`")
        if config.provider == "offline":
            st.caption("AI features disabled — deterministic scoring only.")
        st.divider()
        st.caption(f"DB: `{db.path}`")
    return db.get_profile(chosen)


def as_list(value: Any) -> list[str]:
    """Textarea (one per line) or comma string -> list."""
    if not value:
        return []
    if isinstance(value, str):
        parts = [v.strip() for v in value.replace("\n", ",").split(",")]
        return [p for p in parts if p]
    return [str(v).strip() for v in value if str(v).strip()]


def as_text(values: Any) -> str:
    return "\n".join(as_list(values))
