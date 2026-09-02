"""Harvest -> filter -> deterministic score -> (optional) AI pass.

The split matters: deterministic scoring is free and runs on everything, so the
model only ever sees a shortlist. `blend` decides how much the model is allowed
to move a posting once it has looked at it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..agent import score_posting as ai_score_posting
from ..db import Database
from ..llm.base import LLMError, LLMProvider
from ..models import Posting, Profile, Score
from ..sources import build_source
from .deterministic import Corpus, hard_filter, score_posting


@dataclass
class HarvestReport:
    fetched: int = 0
    stored: int = 0
    filtered: int = 0
    per_source: dict[str, str] = field(default_factory=dict)
    rejections: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def harvest(
    db: Database,
    profile: Profile,
    query: str = "",
    limit_per_source: int = 60,
    source_ids: list[int] | None = None,
    llm: LLMProvider | None = None,
    progress: Callable[[str, float], None] | None = None,
) -> HarvestReport:
    """Pull from every enabled source, apply hard filters, store survivors."""
    report = HarvestReport()
    sources = [s for s in db.list_sources(enabled_only=True) if source_ids is None or s["id"] in source_ids]
    prefs = profile.preferences or {}

    for index, row in enumerate(sources):
        if progress:
            progress(f"{row['label']}…", index / max(len(sources), 1))
        try:
            adapter = build_source(row["kind"], row["config"], llm=llm)
            postings = list(adapter.fetch(query=query, limit=limit_per_source))
        except Exception as exc:  # one broken board must not kill the run
            message = f"{type(exc).__name__}: {exc}"
            report.errors.append(f"{row['label']}: {message}")
            report.per_source[row["label"]] = f"error — {message[:120]}"
            db.update_source(row["id"], last_run=_now(), last_status=f"error: {message[:200]}")
            continue

        kept = 0
        for posting in postings:
            report.fetched += 1
            reason = hard_filter(posting, prefs)
            if reason:
                report.filtered += 1
                report.rejections.append((f"{posting.title} @ {posting.company}", reason))
                continue
            db.upsert_posting(posting)
            report.stored += 1
            kept += 1
        report.per_source[row["label"]] = f"{kept} kept / {len(postings)} fetched"
        db.update_source(row["id"], last_run=_now(), last_status=f"ok: {kept} kept")

    if progress:
        progress("done", 1.0)
    return report


def rescore(db: Database, profile: Profile) -> int:
    """Recompute the deterministic score for every stored posting."""
    postings = db.list_postings(limit=5000)
    if not postings:
        return 0
    corpus = Corpus([p.as_text() for p in postings] + [profile.as_text()])
    profile_vec = corpus.vector(profile.as_text())
    for posting in postings:
        value, breakdown = score_posting(posting, profile, corpus, profile_vec)
        existing = _existing_ai(db, posting.id, profile.id)
        db.save_score(
            Score(
                posting_id=posting.id,
                profile_id=profile.id,
                deterministic=value,
                breakdown=breakdown,
                ai_score=existing,
                final=blend(value, existing, (profile.preferences or {}).get("ai_weight", 0.5)),
            )
        )
    return len(postings)


def ai_pass(
    db: Database,
    profile: Profile,
    llm: LLMProvider,
    posting_ids: list[int],
    progress: Callable[[str, float], None] | None = None,
) -> tuple[int, list[str]]:
    """Run the full AI assessment on the chosen postings. Returns (done, errors)."""
    errors: list[str] = []
    done = 0
    ai_weight = float((profile.preferences or {}).get("ai_weight", 0.5))
    for index, posting_id in enumerate(posting_ids):
        posting = db.get_posting(posting_id)
        if posting is None:
            continue
        if progress:
            progress(f"{posting.title} @ {posting.company}", index / max(len(posting_ids), 1))
        try:
            verdict = ai_score_posting(llm, profile, posting)
        except LLMError as exc:
            errors.append(f"{posting.title}: {exc}")
            continue
        except Exception as exc:
            errors.append(f"{posting.title}: {type(exc).__name__}: {exc}")
            continue

        deterministic = _existing_deterministic(db, posting_id, profile.id)
        db.save_score(
            Score(
                posting_id=posting_id,
                profile_id=profile.id,
                deterministic=deterministic,
                breakdown=_existing_breakdown(db, posting_id, profile.id),
                ai_score=float(verdict["fit_score"]),
                ai=verdict,
                final=blend(deterministic, float(verdict["fit_score"]), ai_weight),
            )
        )
        done += 1
    if progress:
        progress("done", 1.0)
    return done, errors


def blend(deterministic: float, ai: float | None, ai_weight: float = 0.5) -> float:
    """Combine the two halves. Unscored-by-AI postings keep their own score."""
    if ai is None:
        return round(deterministic, 1)
    ai_weight = max(0.0, min(1.0, ai_weight))
    return round(deterministic * (1 - ai_weight) + ai * ai_weight, 1)


# ------------------------------------------------------------------- helpers


def _existing_ai(db: Database, posting_id: int, profile_id: int) -> float | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT ai_score FROM scores WHERE posting_id=? AND profile_id=?", (posting_id, profile_id)
        ).fetchone()
    return row["ai_score"] if row else None


def _existing_deterministic(db: Database, posting_id: int, profile_id: int) -> float:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT deterministic FROM scores WHERE posting_id=? AND profile_id=?", (posting_id, profile_id)
        ).fetchone()
    return float(row["deterministic"]) if row and row["deterministic"] is not None else 0.0


def _existing_breakdown(db: Database, posting_id: int, profile_id: int) -> dict[str, Any]:
    import json

    with db.connect() as conn:
        row = conn.execute(
            "SELECT breakdown FROM scores WHERE posting_id=? AND profile_id=?", (posting_id, profile_id)
        ).fetchone()
    if not row or not row["breakdown"]:
        return {}
    try:
        return json.loads(row["breakdown"])
    except ValueError:
        return {}


def _now() -> str:
    from ..models import utcnow

    return utcnow()
