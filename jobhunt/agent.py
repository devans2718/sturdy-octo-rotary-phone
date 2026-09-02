"""The AI half of the app.

Every function here takes an `LLMProvider` as its first argument — nothing in
this module knows or cares which vendor is behind it. Each task ships its own
JSON schema, which is passed to the provider for structured output *and*
restated in the prompt so weaker local models still comply.
"""

from __future__ import annotations

import json
from typing import Any

from .llm.base import LLMError, LLMProvider
from .models import Posting, Profile

SYSTEM = (
    "You are a pragmatic career advisor and technical recruiter. You are blunt about fit, "
    "you never invent experience the candidate does not have, and you always answer with "
    "JSON only — no prose, no markdown fences."
)

# --------------------------------------------------------------------- schemas

MATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fit_score": {"type": "integer", "description": "0-100 overall fit"},
        "verdict": {"type": "string", "description": "one line, <=140 chars"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "keywords_to_mirror": {"type": "array", "items": {"type": "string"}},
        "tailoring_advice": {"type": "string"},
        "suggested_bullets": {
            "type": "array",
            "items": {"type": "string"},
            "description": "CV bullets drawn ONLY from the candidate's real experience",
        },
        "red_flags": {"type": "array", "items": {"type": "string"}},
        "seniority_match": {"type": "string", "enum": ["under", "match", "over", "unclear"]},
        "apply_priority": {"type": "string", "enum": ["now", "soon", "maybe", "skip"]},
    },
    "required": [
        "fit_score",
        "verdict",
        "strengths",
        "gaps",
        "keywords_to_mirror",
        "tailoring_advice",
        "suggested_bullets",
        "red_flags",
        "seniority_match",
        "apply_priority",
    ],
    "additionalProperties": False,
}

EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "postings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "company": {"type": "string"},
                    "location": {"type": "string"},
                    "remote": {"type": "string", "enum": ["remote", "hybrid", "onsite", ""]},
                    "employment_type": {"type": "string"},
                    "url": {"type": "string"},
                    "salary_text": {"type": "string"},
                    "posted_at": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["title", "company", "location", "remote", "employment_type", "url", "salary_text", "posted_at", "description"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["postings"],
    "additionalProperties": False,
}

RESUME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "location": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["experience", "education", "project", "achievement", "skill", "certification", "publication", "other"],
                    },
                    "title": {"type": "string"},
                    "org": {"type": "string"},
                    "location": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "description": {"type": "string"},
                    "highlights": {"type": "array", "items": {"type": "string"}},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["kind", "title", "org", "location", "start_date", "end_date", "description", "highlights", "tags"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "summary", "location", "items"],
    "additionalProperties": False,
}

STRATEGY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "themes": {"type": "array", "items": {"type": "string"}},
        "skill_gaps": {"type": "array", "items": {"type": "string"}},
        "profile_edits": {"type": "array", "items": {"type": "string"}},
        "search_suggestions": {"type": "array", "items": {"type": "string"}},
        "weekly_plan": {"type": "string"},
    },
    "required": ["themes", "skill_gaps", "profile_edits", "search_suggestions", "weekly_plan"],
    "additionalProperties": False,
}


def _ask(llm: LLMProvider, prompt: str, schema: dict[str, Any], max_tokens: int | None = None) -> Any:
    """One call, schema enforced twice: via the API and via the prompt."""
    full = f"{prompt}\n\nReturn JSON matching exactly this schema:\n{json.dumps(schema)}"
    return llm.complete_json(full, system=SYSTEM, json_schema=schema, max_tokens=max_tokens)


# ----------------------------------------------------------------- scoring


def score_posting(llm: LLMProvider, profile: Profile, posting: Posting, notes: str = "") -> dict[str, Any]:
    """Judge one posting against the master profile.

    The deterministic scorer has already ranked candidates; this runs on the
    shortlist, where a paid call is worth it.
    """
    prefs = profile.preferences or {}
    prompt = f"""Assess how well this candidate fits this job.

## Candidate master profile
{profile.as_markdown()[:12000]}

## Stated preferences
{json.dumps(prefs, indent=2)[:1500]}

## Job posting
Title: {posting.title}
Company: {posting.company}
Location: {posting.location} ({posting.remote or 'unspecified'})
Employment type: {posting.employment_type or 'unspecified'}
Salary (parsed): {posting.salary_min or '?'} – {posting.salary_max or '?'} {posting.salary_currency}
URL: {posting.url}

Description:
{posting.description[:14000]}

{('## Extra context from the user\n' + notes) if notes else ''}

Rules:
- `fit_score` reflects evidence in the profile, not enthusiasm.
- `suggested_bullets` must be rewrites of things the candidate has actually done.
- `gaps` should name concrete missing requirements, not vague worries.
- `red_flags` covers the posting itself (vague scope, unrealistic stack, visa/location blockers)."""
    data = _ask(llm, prompt, MATCH_SCHEMA)
    if not isinstance(data, dict) or "fit_score" not in data:
        raise LLMError("Model response did not contain a fit_score.")
    data["fit_score"] = max(0, min(100, int(float(data["fit_score"]))))
    return data


def rank_batch(llm: LLMProvider, profile: Profile, postings: list[Posting]) -> dict[str, int]:
    """Cheap triage pass: one call scores many postings on titles alone.

    Used to narrow a large harvest before spending a full call per posting.
    Returns {fingerprint: 0-100}.
    """
    schema = {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "score": {"type": "integer"}},
                    "required": ["id", "score"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["scores"],
        "additionalProperties": False,
    }
    listing = "\n".join(
        f"- id={p.fingerprint} | {p.title} @ {p.company} | {p.location} {p.remote}" for p in postings
    )
    prompt = f"""Triage this list of jobs for the candidate below. Score each 0-100 on likely fit
using only the title, company and location. Be decisive: most roles should not score above 60.

## Candidate
{profile.headline}
{profile.summary[:800]}
Key skills: {', '.join(profile.skills()[:40])}
Preferences: {json.dumps(profile.preferences or {})[:600]}

## Jobs
{listing[:20000]}"""
    data = _ask(llm, prompt, schema, max_tokens=8000)
    return {row["id"]: max(0, min(100, int(row["score"]))) for row in data.get("scores", []) if row.get("id")}


# --------------------------------------------------------------- extraction


def extract_postings_from_text(llm: LLMProvider, text: str, base_url: str, company: str) -> list[dict[str, Any]]:
    """Read an unstructured careers page and pull out the openings."""
    prompt = f"""Extract every job opening listed on this careers page.

Company (best guess): {company}
Page URL: {base_url}

Page text:
{text[:18000]}

Rules:
- Only include actual openings, not navigation links or perks.
- `url` should be an absolute or page-relative link to the job if one is visible, else "".
- Leave a field as "" rather than guessing.
- If the page lists no openings, return an empty array."""
    data = _ask(llm, prompt, EXTRACT_SCHEMA, max_tokens=8000)
    postings = data.get("postings", []) if isinstance(data, dict) else []
    return [p for p in postings if isinstance(p, dict) and p.get("title")]


def parse_resume(llm: LLMProvider, text: str) -> dict[str, Any]:
    """Bootstrap the experience bank from a pasted CV."""
    prompt = f"""Convert this résumé/CV into structured profile entries for an experience bank.
Preserve the candidate's own wording for achievements; do not embellish or invent.
Split each role's bullet points into `highlights`. Put technologies and skills into `tags`,
and add separate `skill` items for standalone skill lists.

Résumé:
{text[:24000]}"""
    return _ask(llm, prompt, RESUME_SCHEMA, max_tokens=12000)


# ------------------------------------------------------------------ advice


def cover_letter(llm: LLMProvider, profile: Profile, posting: Posting, tone: str = "direct and specific") -> str:
    """Free-text output — no schema, this one is meant to be read by a human."""
    prompt = f"""Write a cover letter for this application. Tone: {tone}. Max 300 words.
Ground every claim in the candidate's real experience below. No filler openings
("I am writing to apply..."), no invented metrics.

## Candidate
{profile.as_markdown()[:10000]}

## Role
{posting.title} at {posting.company} ({posting.location})
{posting.description[:8000]}"""
    return llm.complete(prompt, system="You are an editor who writes plainly and cuts clichés.", max_tokens=1500)


def strategy_review(llm: LLMProvider, profile: Profile, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Look across the whole shortlist and advise on the search itself."""
    summary = "\n".join(
        f"- {r['title']} @ {r['company']} ({r.get('location','')}) score={r.get('final') or 0:.0f}"
        for r in rows[:60]
    )
    prompt = f"""Review this candidate's current job-search shortlist and advise on the search itself,
not on individual applications.

## Candidate
{profile.headline}
{profile.summary[:1000]}
Skills: {', '.join(profile.skills()[:50])}

## Shortlist ({len(rows)} roles, top 60 shown)
{summary[:12000]}

Cover: recurring themes in what they're matching on, skills that keep appearing as gaps,
concrete edits to the master profile, better search queries or sources to add, and a
one-paragraph plan for the next week."""
    return _ask(llm, prompt, STRATEGY_SCHEMA, max_tokens=6000)
