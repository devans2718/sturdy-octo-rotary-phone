"""Source adapter contract + shared HTTP plumbing.

Every board is different: Greenhouse hands you clean JSON, Lever nests the
description in HTML fragments, a company careers page may be nothing but a
rendered table. Adapters absorb that difference and all emit `Posting`.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable

import requests

from ..models import Posting

USER_AGENT = "jobhunt-streamlit/0.1 (personal job search; contact via app owner)"

# Be a good citizen: one request per host at a time, spaced out. Public boards
# will rate-limit or ban aggressive scrapers, and company sites deserve care.
_host_locks: dict[str, threading.Lock] = {}
_last_call: dict[str, float] = {}
_registry_lock = threading.Lock()


def polite_get(url: str, *, min_interval: float = 1.0, timeout: int = 30, **kwargs: Any) -> requests.Response:
    host = requests.utils.urlparse(url).netloc
    with _registry_lock:
        lock = _host_locks.setdefault(host, threading.Lock())
    with lock:
        wait = min_interval - (time.monotonic() - _last_call.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9"}
        headers.update(kwargs.pop("headers", {}))
        try:
            response = requests.get(url, headers=headers, timeout=timeout, **kwargs)
        finally:
            _last_call[host] = time.monotonic()
    response.raise_for_status()
    return response


@dataclass
class SourceSpec:
    """Describes an adapter so the UI can render a config form generically."""

    kind: str
    label: str
    help: str
    fields: list[dict[str, Any]] = field(default_factory=list)
    needs_llm: bool = False


class JobSource(ABC):
    """Base class for every adapter.

    Subclasses declare a `spec` (drives the Sources UI) and implement `fetch`.
    """

    spec: SourceSpec

    def __init__(self, config: dict[str, Any] | None = None, llm: Any = None) -> None:
        self.config = config or {}
        self.llm = llm  # only the LLM-assisted adapters use this

    @abstractmethod
    def fetch(self, query: str = "", limit: int = 100) -> Iterable[Posting]:
        """Yield postings. Network errors should raise; empty results are fine."""

    # Convenience used by most adapters
    def _posting(self, **kwargs: Any) -> Posting:
        posting = Posting(source=self.label(), source_kind=self.spec.kind, **kwargs)
        posting.fingerprint = posting.compute_fingerprint()
        return posting

    def label(self) -> str:
        detail = self.config.get("token") or self.config.get("company") or self.config.get("url") or ""
        return f"{self.spec.label}: {detail}" if detail else self.spec.label


def matches_query(posting: Posting, query: str) -> bool:
    """Cheap client-side keyword filter for sources with no server-side search.

    Supports space-separated terms (all must appear) and quoted phrases.
    """
    if not query.strip():
        return True
    blob = posting.as_text().lower()
    terms = [t.strip('"').lower() for t in _split_terms(query) if t.strip('"').strip()]
    return all(term in blob for term in terms)


def _split_terms(query: str) -> list[str]:
    out, current, in_quotes = [], "", False
    for ch in query:
        if ch == '"':
            in_quotes = not in_quotes
            current += ch
        elif ch.isspace() and not in_quotes:
            if current:
                out.append(current)
                current = ""
        else:
            current += ch
    if current:
        out.append(current)
    return out
