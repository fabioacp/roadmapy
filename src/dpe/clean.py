"""Turn a raw Jira CSV export into the tidy file the tool actually reads.

The raw "Export Excel CSV (all fields)" dump has dozens of columns and repeats
the Labels column once per label. `clean` is the single place that deals with
that mess: it keeps only the columns mapped in [jira.columns], folds the repeated
Labels into one space-joined cell, and writes a small CSV with stable headers.
Everything downstream then reads that clean file and never sees Jira's chaos.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from .config import Config
from .jira import JiraError
from .sources.csv_source import missing_columns, read_rows

# The order of columns in the cleaned output. Keys are [jira.columns] roles.
CLEAN_ORDER = ["key", "summary", "status", "assignee", "priority", "estimate", "labels", "due"]


def resolve_raw(cfg: Config, override: str | None = None) -> Path:
    """The raw export to read: an explicit path, else [jira] raw_csv.

    If it points at a folder, the most recently modified *.csv inside wins — so a
    folder of dated exports just works, newest export first.
    """
    target = cfg.resolve(override) if override else cfg.resolve(cfg.jira.raw_csv)
    if target.is_dir():
        exports = sorted(target.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not exports:
            raise JiraError(f"no *.csv found in {target} — put a Jira export there first")
        return exports[0]
    if not target.exists():
        raise JiraError(
            f"raw export not found: {target}\n"
            f"  Point [jira] raw_csv at your export file or folder, or pass a path."
        )
    return target


def clean(cfg: Config, override: str | None = None) -> tuple[Path, Path, dict]:
    """Read the raw export, project + tidy, write the clean CSV.

    Returns (raw_path, clean_path, stats) so the caller can report what happened.
    """
    raw_path = resolve_raw(cfg, override)
    header, rows = read_rows(raw_path)

    missing = missing_columns(cfg, header)
    if missing:
        raise JiraError(
            f"columns mapped in [jira.columns] that are not in {raw_path.name}:\n  "
            + "\n  ".join(missing)
            + "\n\nHeader found in the export:\n  "
            + "\n  ".join(sorted(set(header)))
        )

    col = cfg.jira.columns
    out_headers = [col[role] for role in CLEAN_ORDER if role in col]
    labels_col = col.get("labels")

    out_rows: list[list[str]] = []
    kept = 0
    for row in rows:
        if not row.get(col.get("key", "")):
            continue  # a row with no issue key is not an epic
        kept += 1
        cells: list[str] = []
        for role in CLEAN_ORDER:
            if role not in col:
                continue
            column = col[role]
            if column == labels_col:
                cells.append(" ".join(row.get(column, [])))  # fold repeats into one cell
            else:
                values = row.get(column, [])
                cells.append(values[0] if values else "")
        out_rows.append(cells)

    clean_path = cfg.resolve(cfg.jira.csv)
    clean_path.parent.mkdir(parents=True, exist_ok=True)
    with clean_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(out_headers)
        writer.writerows(out_rows)

    stats = {
        "raw_columns": len(set(header)),
        "kept_columns": len(out_headers),
        "raw_rows": len(rows),
        "kept_rows": kept,
        "raw_mtime": datetime.fromtimestamp(raw_path.stat().st_mtime),
    }
    return raw_path, clean_path, stats


def is_stale(cfg: Config) -> bool:
    """True when the raw export is newer than the cleaned file (clean not re-run)."""
    clean_path = cfg.resolve(cfg.jira.csv)
    if not clean_path.exists():
        return False
    try:
        raw_path = resolve_raw(cfg)
    except JiraError:
        return False
    return raw_path.stat().st_mtime > clean_path.stat().st_mtime
