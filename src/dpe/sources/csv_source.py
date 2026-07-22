"""CSV source — reads Jira's "Export Excel CSV (all fields)" export.

The export repeats the same column name whenever a field has multiple values
(Labels, Components, Fix Version...). csv.DictReader collapses that and keeps
only the last value, so we read it manually and accumulate by column name.
"""

from __future__ import annotations

import csv
from pathlib import Path

from ..config import Config
from ..jira import JiraError, RawIssue


def read_rows(path: Path) -> tuple[list[str], list[dict[str, list[str]]]]:
    """Returns (header, rows) where each cell is a list of values."""
    if not path.exists():
        raise JiraError(f"Jira CSV not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            raise JiraError(f"empty CSV: {path}") from None
        header = [h.strip() for h in header]
        rows: list[dict[str, list[str]]] = []
        for raw in reader:
            if not any(cell.strip() for cell in raw):
                continue
            row: dict[str, list[str]] = {}
            for name, cell in zip(header, raw):
                cell = cell.strip()
                if cell:
                    row.setdefault(name, []).append(cell)
            rows.append(row)
    return header, rows


def _first(row: dict[str, list[str]], column: str | None) -> str | None:
    if not column:
        return None
    values = row.get(column)
    return values[0] if values else None


def missing_columns(cfg: Config, header: list[str]) -> list[str]:
    return [
        f"{name} -> {column!r}"
        for name, column in cfg.jira.columns.items()
        if column not in header
    ]


def fetch(cfg: Config) -> list[RawIssue]:
    path = cfg.resolve(cfg.jira.csv)
    header, rows = read_rows(path)

    missing = missing_columns(cfg, header)
    if missing:
        raise JiraError(
            "columns mapped in [jira.columns] that do not exist in the CSV:\n  "
            + "\n  ".join(missing)
            + "\n\nHeader found in the CSV:\n  "
            + "\n  ".join(sorted(set(header)))
        )

    col = cfg.jira.columns
    issues: list[RawIssue] = []
    for line_no, row in enumerate(rows, start=2):
        key = _first(row, col.get("key"))
        if not key:
            continue
        issues.append(
            RawIssue(
                key=key,
                summary=_first(row, col.get("summary")) or "",
                status=_first(row, col.get("status")) or "",
                assignee=_first(row, col.get("assignee")),
                priority=_first(row, col.get("priority")) or "",
                estimate=_first(row, col.get("estimate")),
                labels=row.get(col.get("labels", ""), []),
                due=_first(row, col.get("due")),
                origin=f"row {line_no} of {path.name}",
            )
        )
    return issues


def describe(cfg: Config) -> str:
    return f"CSV {cfg.resolve(cfg.jira.csv)}"
