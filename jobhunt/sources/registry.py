"""Adapter registry: one dict the UI and the harvester both read."""

from __future__ import annotations

from typing import Any

from .aggregators import AdzunaSource, ArbeitnowSource, RemoteOKSource, RemotiveSource
from .ats_boards import AshbySource, GreenhouseSource, LeverSource, WorkableSource
from .base import JobSource, SourceSpec
from .web import GenericCareersPageSource, RSSSource

SOURCE_CLASSES: list[type[JobSource]] = [
    GreenhouseSource,
    LeverSource,
    AshbySource,
    WorkableSource,
    RemotiveSource,
    RemoteOKSource,
    ArbeitnowSource,
    AdzunaSource,
    RSSSource,
    GenericCareersPageSource,
]

SOURCES: dict[str, type[JobSource]] = {cls.spec.kind: cls for cls in SOURCE_CLASSES}


def get_spec(kind: str) -> SourceSpec:
    return SOURCES[kind].spec


def build_source(kind: str, config: dict[str, Any], llm: Any = None) -> JobSource:
    cls = SOURCES.get(kind)
    if cls is None:
        raise ValueError(f"Unknown source kind {kind!r}. Known: {', '.join(SOURCES)}")
    return cls(config, llm=llm)


# A handful of working sources so a fresh install has something to run.
STARTER_SOURCES: list[dict[str, Any]] = [
    {"label": "Remotive (remote roles)", "kind": "remotive", "config": {}},
    {"label": "Arbeitnow (EU board)", "kind": "arbeitnow", "config": {"pages": 2}},
    {"label": "RemoteOK", "kind": "remoteok", "config": {}},
]
