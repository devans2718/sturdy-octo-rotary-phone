"""Model backend configuration — the swappable-agent control panel."""

from __future__ import annotations

import json
import os

import streamlit as st

from jobhunt.db import DEFAULT_DB
from jobhunt.llm import (
    ANTHROPIC_MODELS,
    PRESETS,
    PROVIDERS,
    SETTINGS_KEY,
    LLMConfig,
    build_provider,
    config_to_dict,
)
from jobhunt.state import get_db, llm_config, sidebar

st.set_page_config(page_title="Settings", page_icon="⚙️", layout="wide")
db = get_db(DEFAULT_DB)
sidebar(db)

st.title("⚙️ Settings")

st.subheader("AI backend")
st.caption(
    "The app talks to one small interface (`LLMProvider.complete`). Anything that speaks the "
    "Anthropic Messages API or the OpenAI `/chat/completions` shape drops in without code changes."
)

config = llm_config(db)

preset_names = list(PRESETS)
current_preset = next(
    (name for name, values in PRESETS.items() if values["provider"] == config.provider and values["model"] == config.model),
    None,
)
preset = st.selectbox(
    "Preset",
    ["(keep current)"] + preset_names,
    index=(preset_names.index(current_preset) + 1) if current_preset else 0,
)
if preset != "(keep current)" and st.button(f"Apply “{preset}”"):
    values = PRESETS[preset]
    config.provider, config.model, config.base_url = values["provider"], values["model"], values["base_url"]
    db.set_setting(SETTINGS_KEY, config_to_dict(config))
    st.rerun()

with st.form("llm_settings"):
    c1, c2 = st.columns(2)
    provider = c1.selectbox("Provider", list(PROVIDERS), index=list(PROVIDERS).index(config.provider))

    if provider == "anthropic":
        model_options = ANTHROPIC_MODELS + ([config.model] if config.model not in ANTHROPIC_MODELS else [])
        model = c2.selectbox("Model", model_options, index=model_options.index(config.model) if config.model in model_options else 0)
    else:
        model = c2.text_input("Model", config.model, help="Whatever id the endpoint expects, e.g. `llama3.1`, `gpt-4o-mini`.")

    c1, c2 = st.columns(2)
    base_url = c1.text_input(
        "Base URL",
        config.base_url,
        placeholder="http://localhost:11434/v1  ·  leave blank for the provider default",
        disabled=provider == "offline",
    )
    api_key = c2.text_input(
        "API key",
        config.api_key,
        type="password",
        help="Stored in the local SQLite file. Leave blank to use the environment variable instead.",
        disabled=provider == "offline",
    )

    c1, c2, c3 = st.columns(3)
    max_tokens = c1.number_input("Max output tokens", value=int(config.max_tokens), min_value=256, max_value=64000, step=500)
    timeout = c2.number_input("Timeout (s)", value=int(config.timeout), min_value=10, max_value=900, step=10)
    effort_options = ["low", "medium", "high", "xhigh", "max"]
    effort = c3.selectbox(
        "Reasoning effort (Anthropic)",
        effort_options,
        index=effort_options.index(config.extra.get("effort", "medium")),
        disabled=provider != "anthropic",
        help="Higher effort means better judgement on borderline postings, at more cost.",
    )
    temperature = st.slider(
        "Temperature (OpenAI-compatible only)",
        0.0,
        1.5,
        float(config.temperature if config.temperature is not None else 0.2),
        0.05,
        disabled=provider != "openai-compatible",
    )

    if st.form_submit_button("Save backend settings", type="primary"):
        saved = LLMConfig(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_tokens=int(max_tokens),
            temperature=temperature if provider == "openai-compatible" else None,
            timeout=int(timeout),
            extra={"effort": effort},
        )
        db.set_setting(SETTINGS_KEY, config_to_dict(saved))
        st.success("Saved.")
        st.rerun()

# ------------------------------------------------------------------- testing
c1, c2 = st.columns([1, 3])
if c1.button("Test connection"):
    with st.spinner("Calling the model…"):
        try:
            provider_instance = build_provider(llm_config(db))
            ok, message = provider_instance.health_check()
        except Exception as exc:
            ok, message = False, f"{type(exc).__name__}: {exc}"
    (c2.success if ok else c2.error)(message or "(empty response)")

with st.expander("Current configuration (key redacted)"):
    st.json(llm_config(db).redacted())

st.divider()

# --------------------------------------------------------------- environment
st.subheader("Environment")
st.caption("Environment variables are used when the matching field above is left blank.")
env_rows = [
    {"variable": name, "set": "✅" if os.environ.get(name) else "—"}
    for name in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LOCAL_LLM_BASE_URL", "LOCAL_LLM_API_KEY", "JOBHUNT_DB"]
]
st.table(env_rows)

st.markdown(
    """
**Adding a provider takes one file.** Subclass `LLMProvider` in `jobhunt/llm/`, implement
`complete(...)`, and register it in `jobhunt/llm/registry.py::PROVIDERS`. Every scoring,
extraction and advice feature picks it up immediately — they only ever call `complete` and
`complete_json`.
"""
)

st.divider()

# ----------------------------------------------------------------- data admin
st.subheader("Data")
c1, c2, c3 = st.columns(3)
c1.metric("Profiles", len(db.list_profiles()))
c2.metric("Sources", len(db.list_sources()))
c3.metric("Postings", len(db.list_postings(limit=100000)))
st.caption(f"Everything lives in `{db.path}`. Delete that file to start over.")

export = {
    "profiles": [
        {
            "name": p.name,
            "headline": p.headline,
            "summary": p.summary,
            "location": p.location,
            "preferences": p.preferences,
            "items": [
                {k: v for k, v in item.__dict__.items() if k not in {"id", "profile_id"}}
                for item in (db.get_profile(p.id).items if p.id else [])
            ],
        }
        for p in db.list_profiles()
    ],
    "sources": [{"label": s["label"], "kind": s["kind"], "config": s["config"]} for s in db.list_sources()],
}
st.download_button(
    "Back up profiles and sources (JSON)",
    data=json.dumps(export, indent=2),
    file_name="jobhunt_backup.json",
    mime="application/json",
)
