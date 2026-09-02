"""Deterministic scoring: fast, free, explainable, runs on every posting.

Nothing here calls a model. The output is a 0-100 score plus a per-component
breakdown so the UI can show *why* something ranked where it did — and so the
expensive AI pass can be pointed at the top of the list only.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Iterable

from ..models import Posting, Profile
from ..normalize import days_since, detect_seniority, tokenize

DEFAULT_WEIGHTS: dict[str, float] = {
    "skills": 0.30,
    "similarity": 0.22,
    "title": 0.16,
    "seniority": 0.10,
    "location": 0.12,
    "salary": 0.06,
    "recency": 0.04,
}

SENIORITY_ORDER = ["intern", "junior", "mid", "senior", "staff", "lead"]


class Corpus:
    """IDF over the harvested postings.

    Rebuilt per scoring run — the corpus is small (hundreds to thousands of
    postings), so this is cheaper and more predictable than persisting vectors.
    """

    def __init__(self, documents: Iterable[str]) -> None:
        self.n = 0
        df: Counter[str] = Counter()
        for doc in documents:
            self.n += 1
            df.update(set(tokenize(doc)))
        self.idf = {term: math.log((self.n + 1) / (count + 1)) + 1.0 for term, count in df.items()}
        self._default_idf = math.log(self.n + 1) + 1.0

    def vector(self, text: str) -> dict[str, float]:
        counts = Counter(tokenize(text))
        if not counts:
            return {}
        peak = max(counts.values())
        vec = {
            term: (0.5 + 0.5 * count / peak) * self.idf.get(term, self._default_idf)
            for term, count in counts.items()
        }
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}

    def cosine(self, a: dict[str, float], b: dict[str, float]) -> float:
        if len(a) > len(b):
            a, b = b, a
        return sum(weight * b.get(term, 0.0) for term, weight in a.items())


def hard_filter(posting: Posting, prefs: dict[str, Any]) -> str:
    """Return a rejection reason, or '' if the posting survives.

    Hard filters are deliberately separate from scoring: a visa-blocked or
    wrong-continent role should disappear, not merely rank low.
    """
    blob = posting.as_text().lower()

    for company in _list(prefs.get("exclude_companies")):
        if company.lower() in (posting.company or "").lower():
            return f"excluded company: {company}"

    for term in _list(prefs.get("exclude_keywords")):
        if term.lower() in blob:
            return f"excluded keyword: {term}"

    required = _list(prefs.get("must_have_keywords"))
    missing = [t for t in required if t.lower() not in blob]
    if missing:
        return f"missing required keyword(s): {', '.join(missing)}"

    hard_min = prefs.get("hard_min_salary")
    if hard_min:
        top = posting.salary_max or posting.salary_min
        if top is not None and top < float(hard_min):
            return f"salary below floor ({top:,.0f} < {float(hard_min):,.0f})"

    if prefs.get("remote_required") and posting.remote == "onsite":
        return "on-site only"

    max_age = prefs.get("max_age_days")
    if max_age:
        age = days_since(posting.posted_at)
        if age is not None and age > float(max_age):
            return f"older than {max_age} days"

    return ""


def score_posting(posting: Posting, profile: Profile, corpus: Corpus, profile_vec: dict[str, float]) -> tuple[float, dict[str, float]]:
    """-> (0-100 score, component breakdown in 0-1)."""
    prefs = profile.preferences or {}
    blob = posting.as_text().lower()

    components = {
        "skills": _skill_overlap(profile.skills(), blob),
        "similarity": corpus.cosine(profile_vec, corpus.vector(posting.as_text())),
        "title": _title_match(posting.title, _list(prefs.get("target_titles"))),
        "seniority": _seniority_match(posting, prefs.get("target_seniority", "")),
        "location": _location_match(posting, prefs),
        "salary": _salary_match(posting, prefs),
        "recency": _recency(posting),
    }

    weights = {**DEFAULT_WEIGHTS, **(prefs.get("weights") or {})}
    total_weight = sum(weights.get(k, 0.0) for k in components) or 1.0
    score = sum(components[k] * weights.get(k, 0.0) for k in components) / total_weight
    return round(score * 100, 1), {k: round(v, 3) for k, v in components.items()}


# ------------------------------------------------------------------ components


def _skill_overlap(skills: list[str], blob: str) -> float:
    """Fraction of the candidate's skills the posting actually asks for.

    Saturating rather than linear: matching 12 of 40 skills is already a strong
    signal, and long skill lists shouldn't dilute the score.
    """
    if not skills:
        return 0.0
    hits = sum(1 for skill in skills if _mentions(blob, skill))
    return min(1.0, math.log1p(hits) / math.log1p(8))


def _mentions(blob: str, term: str) -> bool:
    term = term.strip().lower()
    if not term:
        return False
    if len(term) <= 3 or not term.isalnum():
        # Short/symbolic tokens (go, r, c++) need word boundaries to avoid
        # matching inside other words.
        return re.search(rf"(?<![a-z0-9+#]){re.escape(term)}(?![a-z0-9+#])", blob) is not None
    return term in blob


def _title_match(title: str, targets: list[str]) -> float:
    if not targets:
        return 0.5  # neutral when the user stated no target titles
    title_tokens = set(tokenize(title))
    if not title_tokens:
        return 0.0
    best = 0.0
    for target in targets:
        target_tokens = set(tokenize(target))
        if not target_tokens:
            continue
        overlap = len(title_tokens & target_tokens) / len(target_tokens)
        if target.lower().strip() in title.lower():
            overlap = 1.0
        best = max(best, overlap)
    return min(1.0, best)


def _seniority_match(posting: Posting, target: str) -> float:
    if not target:
        return 0.5
    found = detect_seniority(posting.title, posting.description[:2000])
    if not found:
        return 0.5
    try:
        distance = abs(SENIORITY_ORDER.index(found) - SENIORITY_ORDER.index(target))
    except ValueError:
        return 0.5
    return max(0.0, 1.0 - distance * 0.35)


def _location_match(posting: Posting, prefs: dict[str, Any]) -> float:
    preferred_mode = (prefs.get("remote_preference") or "").lower()  # remote|hybrid|onsite|any
    locations = [l.lower() for l in _list(prefs.get("target_locations"))]
    posting_location = (posting.location or "").lower()

    mode_score = 0.5
    if preferred_mode and preferred_mode != "any":
        if posting.remote == preferred_mode:
            mode_score = 1.0
        elif not posting.remote:
            mode_score = 0.5
        elif preferred_mode == "hybrid" and posting.remote in {"remote", "onsite"}:
            mode_score = 0.6
        else:
            mode_score = 0.15

    if not locations:
        return mode_score
    geo_score = 1.0 if any(loc in posting_location for loc in locations) else 0.2
    if posting.remote == "remote":
        geo_score = max(geo_score, 0.85)  # remote roles clear most geo constraints
    return 0.5 * mode_score + 0.5 * geo_score


def _salary_match(posting: Posting, prefs: dict[str, Any]) -> float:
    target = prefs.get("target_salary")
    if not target:
        return 0.5
    target = float(target)
    top = posting.salary_max or posting.salary_min
    if top is None:
        return 0.4  # undisclosed salary is a mild negative, not a rejection
    ratio = top / target if target else 1.0
    if ratio >= 1.0:
        return min(1.0, 0.85 + 0.15 * min(ratio - 1.0, 1.0))
    return max(0.0, ratio ** 2)


def _recency(posting: Posting) -> float:
    age = days_since(posting.posted_at)
    if age is None:
        return 0.5
    # Half-life of ~21 days; a 60-day-old posting is often already filled.
    return max(0.0, min(1.0, 0.5 ** (max(age, 0) / 21.0)))


def _list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [v.strip() for v in re.split(r"[,\n;]", value) if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]
