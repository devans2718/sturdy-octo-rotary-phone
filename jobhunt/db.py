"""SQLite persistence. One file, no ORM, safe to delete and recreate."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import Posting, Profile, ProfileItem, Score, utcnow

DEFAULT_DB = os.environ.get("JOBHUNT_DB", "data/jobhunt.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    headline TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    location TEXT DEFAULT '',
    preferences TEXT DEFAULT '{}',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS profile_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    title TEXT DEFAULT '',
    org TEXT DEFAULT '',
    location TEXT DEFAULT '',
    start_date TEXT DEFAULT '',
    end_date TEXT DEFAULT '',
    description TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    highlights TEXT DEFAULT '[]',
    sort_order INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_profile ON profile_items(profile_id, kind);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    kind TEXT NOT NULL,
    config TEXT DEFAULT '{}',
    enabled INTEGER DEFAULT 1,
    last_run TEXT DEFAULT '',
    last_status TEXT DEFAULT '',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS postings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    source TEXT, source_kind TEXT, external_id TEXT,
    title TEXT, company TEXT, location TEXT, remote TEXT,
    employment_type TEXT,
    salary_min REAL, salary_max REAL, salary_currency TEXT,
    url TEXT, description TEXT,
    posted_at TEXT, fetched_at TEXT,
    raw TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_postings_company ON postings(company);

CREATE TABLE IF NOT EXISTS scores (
    posting_id INTEGER NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    deterministic REAL DEFAULT 0,
    breakdown TEXT DEFAULT '{}',
    ai_score REAL,
    ai TEXT DEFAULT '{}',
    final REAL DEFAULT 0,
    created_at TEXT,
    PRIMARY KEY (posting_id, profile_id)
);

CREATE TABLE IF NOT EXISTS tracker (
    posting_id INTEGER NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    status TEXT DEFAULT 'new',
    notes TEXT DEFAULT '',
    updated_at TEXT,
    PRIMARY KEY (posting_id, profile_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


class Database:
    def __init__(self, path: str = DEFAULT_DB) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------------------------------------------------------------- profiles
    def list_profiles(self) -> list[Profile]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM profiles ORDER BY name").fetchall()
        return [self._profile_from_row(r) for r in rows]

    def get_profile(self, profile_id: int, with_items: bool = True) -> Profile | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
            if row is None:
                return None
            profile = self._profile_from_row(row)
            if with_items:
                items = conn.execute(
                    "SELECT * FROM profile_items WHERE profile_id=? ORDER BY kind, sort_order, id",
                    (profile_id,),
                ).fetchall()
                profile.items = [self._item_from_row(i) for i in items]
        return profile

    @staticmethod
    def _profile_from_row(row: sqlite3.Row) -> Profile:
        return Profile(
            id=row["id"],
            name=row["name"],
            headline=row["headline"],
            summary=row["summary"],
            location=row["location"],
            preferences=_loads(row["preferences"], {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _item_from_row(row: sqlite3.Row) -> ProfileItem:
        return ProfileItem(
            id=row["id"],
            profile_id=row["profile_id"],
            kind=row["kind"],
            title=row["title"],
            org=row["org"],
            location=row["location"],
            start_date=row["start_date"],
            end_date=row["end_date"],
            description=row["description"],
            tags=_loads(row["tags"], []),
            highlights=_loads(row["highlights"], []),
            sort_order=row["sort_order"],
        )

    def create_profile(self, profile: Profile) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO profiles (name, headline, summary, location, preferences, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    profile.name,
                    profile.headline,
                    profile.summary,
                    profile.location,
                    json.dumps(profile.preferences),
                    utcnow(),
                    utcnow(),
                ),
            )
            return int(cur.lastrowid)

    def update_profile(self, profile: Profile) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE profiles SET name=?, headline=?, summary=?, location=?,
                   preferences=?, updated_at=? WHERE id=?""",
                (
                    profile.name,
                    profile.headline,
                    profile.summary,
                    profile.location,
                    json.dumps(profile.preferences),
                    utcnow(),
                    profile.id,
                ),
            )

    def delete_profile(self, profile_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM profiles WHERE id=?", (profile_id,))

    def duplicate_profile(self, profile_id: int, new_name: str) -> int:
        source = self.get_profile(profile_id)
        if source is None:
            raise ValueError(f"profile {profile_id} not found")
        source.name = new_name
        new_id = self.create_profile(source)
        for item in source.items:
            item.id, item.profile_id = None, new_id
            self.upsert_item(item)
        return new_id

    # ------------------------------------------------------------ profile items
    def upsert_item(self, item: ProfileItem) -> int:
        payload = (
            item.profile_id,
            item.kind,
            item.title,
            item.org,
            item.location,
            item.start_date,
            item.end_date,
            item.description,
            json.dumps(item.tags),
            json.dumps(item.highlights),
            item.sort_order,
        )
        with self.connect() as conn:
            if item.id:
                conn.execute(
                    """UPDATE profile_items SET profile_id=?, kind=?, title=?, org=?, location=?,
                       start_date=?, end_date=?, description=?, tags=?, highlights=?, sort_order=?
                       WHERE id=?""",
                    payload + (item.id,),
                )
                return item.id
            cur = conn.execute(
                """INSERT INTO profile_items
                   (profile_id, kind, title, org, location, start_date, end_date,
                    description, tags, highlights, sort_order)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                payload,
            )
            return int(cur.lastrowid)

    def delete_item(self, item_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM profile_items WHERE id=?", (item_id,))

    # ----------------------------------------------------------------- sources
    def list_sources(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sources" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY kind, label"
        with self.connect() as conn:
            rows = conn.execute(sql).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["config"] = _loads(d["config"], {})
            out.append(d)
        return out

    def add_source(self, label: str, kind: str, config: dict[str, Any], enabled: bool = True) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO sources (label, kind, config, enabled, created_at) VALUES (?,?,?,?,?)",
                (label, kind, json.dumps(config), int(enabled), utcnow()),
            )
            return int(cur.lastrowid)

    def update_source(self, source_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "config" in fields:
            fields["config"] = json.dumps(fields["config"])
        if "enabled" in fields:
            fields["enabled"] = int(fields["enabled"])
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE sources SET {cols} WHERE id=?", (*fields.values(), source_id))

    def delete_source(self, source_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sources WHERE id=?", (source_id,))

    # ---------------------------------------------------------------- postings
    def upsert_posting(self, posting: Posting) -> int:
        """Insert, or refresh an existing row with the same fingerprint.

        Returns the posting id. Deduplication happens here, so the same job
        harvested from an aggregator and the company's own board collapses.
        """
        if not posting.fingerprint:
            posting.fingerprint = posting.compute_fingerprint()
        row = posting.to_row()
        row.pop("id", None)
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        updates = ", ".join(f"{k}=excluded.{k}" for k in row if k != "fingerprint")
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO postings ({cols}) VALUES ({marks}) "
                f"ON CONFLICT(fingerprint) DO UPDATE SET {updates}",
                tuple(row.values()),
            )
            return int(
                conn.execute("SELECT id FROM postings WHERE fingerprint=?", (posting.fingerprint,)).fetchone()[0]
            )

    def get_posting(self, posting_id: int) -> Posting | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM postings WHERE id=?", (posting_id,)).fetchone()
        return self._posting_from_row(row) if row else None

    @staticmethod
    def _posting_from_row(row: sqlite3.Row) -> Posting:
        d = dict(row)
        d["raw"] = _loads(d.get("raw"), {})
        return Posting(**d)

    def list_postings(self, limit: int = 1000) -> list[Posting]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM postings ORDER BY fetched_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._posting_from_row(r) for r in rows]

    def delete_posting(self, posting_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM postings WHERE id=?", (posting_id,))

    def clear_postings(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM postings")

    # ------------------------------------------------------------------ scores
    def save_score(self, score: Score) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO scores (posting_id, profile_id, deterministic, breakdown,
                                       ai_score, ai, final, created_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(posting_id, profile_id) DO UPDATE SET
                     deterministic=excluded.deterministic, breakdown=excluded.breakdown,
                     ai_score=COALESCE(excluded.ai_score, scores.ai_score),
                     ai=CASE WHEN excluded.ai='{}' THEN scores.ai ELSE excluded.ai END,
                     final=excluded.final, created_at=excluded.created_at""",
                (
                    score.posting_id,
                    score.profile_id,
                    score.deterministic,
                    json.dumps(score.breakdown),
                    score.ai_score,
                    json.dumps(score.ai),
                    score.final,
                    utcnow(),
                ),
            )

    def scored_rows(self, profile_id: int) -> list[dict[str, Any]]:
        """Join postings + scores + tracker for the Matches table."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT p.*, s.deterministic, s.breakdown, s.ai_score, s.ai, s.final,
                          COALESCE(t.status, 'new') AS status, COALESCE(t.notes, '') AS notes
                   FROM postings p
                   LEFT JOIN scores s ON s.posting_id = p.id AND s.profile_id = ?
                   LEFT JOIN tracker t ON t.posting_id = p.id AND t.profile_id = ?
                   ORDER BY COALESCE(s.final, -1) DESC, p.fetched_at DESC""",
                (profile_id, profile_id),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["raw"] = _loads(d.get("raw"), {})
            d["breakdown"] = _loads(d.get("breakdown"), {})
            d["ai"] = _loads(d.get("ai"), {})
            out.append(d)
        return out

    # ----------------------------------------------------------------- tracker
    def set_status(self, posting_id: int, profile_id: int, status: str, notes: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO tracker (posting_id, profile_id, status, notes, updated_at)
                   VALUES (?,?,?,COALESCE(?,''),?)
                   ON CONFLICT(posting_id, profile_id) DO UPDATE SET
                     status=excluded.status,
                     notes=COALESCE(?, tracker.notes),
                     updated_at=excluded.updated_at""",
                (posting_id, profile_id, status, notes, utcnow(), notes),
            )

    # ---------------------------------------------------------------- settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return _loads(row["value"], default) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
