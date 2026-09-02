"""Plain dataclasses shared across the app.

These mirror the SQLite schema in `db.py` but stay free of storage concerns so
the sources / scoring layers can be unit tested without a database.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

# The kinds of entries a master profile ("experience bank") can hold.
ITEM_KINDS = [
    "experience",
    "education",
    "project",
    "achievement",
    "skill",
    "certification",
    "publication",
    "other",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ProfileItem:
    """One entry in the experience bank."""

    id: int | None = None
    profile_id: int | None = None
    kind: str = "experience"
    title: str = ""
    org: str = ""
    location: str = ""
    start_date: str = ""  # free text: "2021-03", "Mar 2021", ...
    end_date: str = ""  # "" or "Present"
    description: str = ""
    tags: list[str] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)
    sort_order: int = 0

    def as_text(self) -> str:
        """Flatten into the text blob used for keyword / similarity scoring."""
        parts = [self.title, self.org, self.kind, self.description]
        parts.extend(self.highlights)
        parts.extend(self.tags)
        return "\n".join(p for p in parts if p)


@dataclass
class Profile:
    id: int | None = None
    name: str = "Default"
    headline: str = ""
    summary: str = ""
    location: str = ""
    # Search preferences double as deterministic scoring inputs.
    preferences: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)
    items: list[ProfileItem] = field(default_factory=list)

    def skills(self) -> list[str]:
        out: list[str] = []
        for item in self.items:
            if item.kind == "skill":
                out.extend(s.strip() for s in re.split(r"[,;\n]", item.title) if s.strip())
            out.extend(t for t in item.tags if t)
        seen, uniq = set(), []
        for s in out:
            k = s.lower().strip()
            if k and k not in seen:
                seen.add(k)
                uniq.append(s.strip())
        return uniq

    def as_text(self) -> str:
        head = "\n".join(p for p in [self.headline, self.summary, self.location] if p)
        return head + "\n" + "\n".join(i.as_text() for i in self.items)

    def as_markdown(self) -> str:
        """Compact master-CV rendering; this is what gets sent to the LLM."""
        lines = [f"# {self.name}"]
        if self.headline:
            lines.append(f"**{self.headline}**")
        if self.location:
            lines.append(f"Location: {self.location}")
        if self.summary:
            lines.append(f"\n{self.summary}")
        for kind in ITEM_KINDS:
            group = [i for i in self.items if i.kind == kind]
            if not group:
                continue
            lines.append(f"\n## {kind.title()}")
            for i in group:
                dates = " – ".join(x for x in [i.start_date, i.end_date] if x)
                header = " | ".join(x for x in [i.title, i.org, i.location, dates] if x)
                lines.append(f"- {header}")
                if i.description:
                    lines.append(f"  {i.description}")
                for h in i.highlights:
                    lines.append(f"  * {h}")
                if i.tags:
                    lines.append(f"  _tags: {', '.join(i.tags)}_")
        return "\n".join(lines)


@dataclass
class Posting:
    """A normalized job posting, regardless of where it came from."""

    id: int | None = None
    fingerprint: str = ""
    source: str = ""  # human label, e.g. "Greenhouse: stripe"
    source_kind: str = ""  # adapter id, e.g. "greenhouse"
    external_id: str = ""
    title: str = ""
    company: str = ""
    location: str = ""
    remote: str = ""  # remote | hybrid | onsite | ""
    employment_type: str = ""
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str = ""
    url: str = ""
    description: str = ""
    posted_at: str = ""
    fetched_at: str = field(default_factory=utcnow)
    raw: dict[str, Any] = field(default_factory=dict)

    def compute_fingerprint(self) -> str:
        """Stable identity so the same job from two boards collapses to one row."""
        norm = lambda s: re.sub(r"[^a-z0-9]+", "", (s or "").lower())
        basis = f"{norm(self.company)}|{norm(self.title)}|{norm(self.location)[:24]}"
        if not norm(self.company) and self.url:
            basis = self.url
        return hashlib.sha1(basis.encode()).hexdigest()[:20]

    def as_text(self) -> str:
        return "\n".join(
            p for p in [self.title, self.company, self.location, self.employment_type, self.description] if p
        )

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["raw"] = json.dumps(d["raw"], default=str)
        return d


@dataclass
class Score:
    posting_id: int = 0
    profile_id: int = 0
    deterministic: float = 0.0
    breakdown: dict[str, float] = field(default_factory=dict)
    ai_score: float | None = None
    ai: dict[str, Any] = field(default_factory=dict)
    final: float = 0.0
    created_at: str = field(default_factory=utcnow)


# Application tracker states, in pipeline order.
STATUSES = ["new", "shortlist", "applied", "interviewing", "offer", "rejected", "archived"]
