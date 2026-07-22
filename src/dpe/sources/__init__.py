"""Epic data sources. Pick one via [jira] source or the --source flag."""

from __future__ import annotations

from ..config import Config
from ..jira import Epic, JiraError, RawIssue, normalize_all
from . import api_source, csv_source

SOURCES = {"csv": csv_source, "api": api_source}


def _pick(cfg: Config, override: str | None):
    name = (override or cfg.jira.source).lower()
    if name not in SOURCES:
        raise JiraError(f"unknown source {name!r} — use one of: {sorted(SOURCES)}")
    return name, SOURCES[name]


def fetch_raw(cfg: Config, source: str | None = None) -> list[RawIssue]:
    _, module = _pick(cfg, source)
    return module.fetch(cfg)


def load_epics(cfg: Config, source: str | None = None) -> list[Epic]:
    return normalize_all(cfg, fetch_raw(cfg, source))


def describe(cfg: Config, source: str | None = None) -> str:
    _, module = _pick(cfg, source)
    return module.describe(cfg)


__all__ = ["fetch_raw", "load_epics", "describe", "SOURCES"]
