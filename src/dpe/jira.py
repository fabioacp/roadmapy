"""Epic model and the normalization shared across the data sources.

Each source (CSV, API) produces `RawIssue` — a raw issue, still all strings, with
no business rules applied. `normalize()` is the single place that applies the
rules: done-status filter, labels -> skills, points -> senior-days. That way CSV
and API cannot drift apart in behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .config import Config


class JiraError(Exception):
    """Problem in the Jira data — the message is shown straight to the user."""


@dataclass
class RawIssue:
    """Issue exactly as the source gave it, before any business rule."""

    key: str
    summary: str = ""
    status: str = ""
    assignee: str | None = None
    priority: str = ""
    estimate: str | None = None
    labels: list[str] = field(default_factory=list)
    due: str | None = None
    origin: str = ""  # "row 4 of epics.csv" — used in the error messages


@dataclass
class Owner:
    """One person declared on an epic via an owner: label, with their FTE."""

    person: str  # resolved roster name
    fte: float   # 0..1, share of that person's day on this epic; default 1.0


@dataclass
class Epic:
    key: str
    summary: str
    status: str
    assignee: str | None
    priority: str
    estimate_days: float
    estimate_missing: bool
    skills: set[str]
    min_seniority: str | None
    due: date | None
    owners: list[Owner] = field(default_factory=list)  # empty = open backlog
    labels: list[str] = field(default_factory=list)
    priority_forced: bool = False   # came from [priority] in the config, not Jira
    jira_priority: str = ""         # what Jira said, before the override

    @property
    def label(self) -> str:
        return f"{self.key} — {self.summary}"

    @property
    def owned(self) -> bool:
        return bool(self.owners)


_DATE_FORMATS = ("%Y-%m-%d", "%d/%b/%y", "%d/%b/%y %I:%M %p", "%d/%m/%Y", "%m/%d/%Y")


def parse_jira_date(value: str) -> date | None:
    """Jira returns dates in different formats depending on the account locale."""
    text = value.strip()
    if not text:
        return None
    # The API returns a full ISO datetime in some fields.
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def normalize(cfg: Config, raw: RawIssue) -> Epic | None:
    """Applies the business rules. Returns None if the issue should be skipped."""
    spec = cfg.jira
    where = f"{raw.key} ({raw.origin})" if raw.origin else raw.key

    if raw.status.lower() in spec.done_statuses:
        return None

    skills = {
        lb[len(spec.skill_label_prefix):].strip().lower()
        for lb in raw.labels
        if lb.lower().startswith(spec.skill_label_prefix.lower())
    }
    skills.discard("")

    min_seniority = None
    for lb in raw.labels:
        if lb.lower().startswith(spec.min_seniority_label_prefix.lower()):
            candidate = lb[len(spec.min_seniority_label_prefix):].strip().lower()
            if candidate not in cfg.throughput:
                raise JiraError(
                    f"{where}: label {lb!r} asks for seniority {candidate!r}, "
                    f"which does not exist in [throughput]"
                )
            min_seniority = candidate

    estimate_missing = False
    if raw.estimate is not None and str(raw.estimate).strip():
        text = str(raw.estimate).strip().replace(",", ".")
        try:
            value = float(text)
        except ValueError:
            raise JiraError(f"{where}: estimate {raw.estimate!r} is not a number") from None
        if value <= 0:
            estimate_missing = True
            days = spec.default_estimate_days
        else:
            days = value * spec.points_to_days if spec.estimate_unit == "points" else value
    else:
        estimate_missing = True
        days = spec.default_estimate_days

    # Config priority beats Jira's — you are the one who decides the team's order.
    priority = raw.priority
    forced = cfg.priority_for(raw.key)
    if forced is not None and forced != priority:
        priority = forced

    owners = _parse_owners(cfg, raw, where)

    assignee = raw.assignee
    if assignee and cfg.person(assignee) is None:
        assignee = None  # person outside the roster: treat as unassigned

    return Epic(
        key=raw.key,
        summary=raw.summary,
        status=raw.status,
        assignee=assignee,
        priority=priority,
        priority_forced=forced is not None,
        jira_priority=raw.priority,
        estimate_days=days,
        estimate_missing=estimate_missing,
        skills=skills,
        min_seniority=min_seniority,
        owners=owners,
        due=parse_jira_date(raw.due) if raw.due else None,
        labels=list(raw.labels),
    )


def _parse_owners(cfg: Config, raw: RawIssue, where: str) -> list[Owner]:
    """Read owner:<alias>[:<pct>] labels into a de-duplicated list of Owners.

    An epic with one or more owner labels is a fact — the tool schedules exactly
    those people. Multiple labels mean shared ownership. The trailing :pct is the
    FTE (owner:ana-souza:60 = 60%); no pct means 100%.
    """
    prefix = cfg.jira.owner_label_prefix
    owners: list[Owner] = []
    seen: set[str] = set()
    for lb in raw.labels:
        if not lb.lower().startswith(prefix.lower()):
            continue
        body = lb[len(prefix):].strip()
        parts = body.rsplit(":", 1)
        if len(parts) == 2 and parts[1].isdigit():
            alias, pct = parts[0].strip().lower(), int(parts[1])
        else:
            alias, pct = body.lower(), 100
        if not 0 < pct <= 100:
            raise JiraError(f"{where}: label {lb!r} has FTE {pct} — must be 1..100")
        person = cfg.person_by_alias(alias)
        if person is None:
            known = ", ".join(sorted(p.alias for p in cfg.people))
            raise JiraError(
                f"{where}: owner label {lb!r} points at {alias!r}, which matches "
                f"no one in [[people]] (known aliases: {known})"
            )
        if person.name in seen:
            raise JiraError(
                f"{where}: {person.name} appears twice in owner labels — "
                f"one owner label per person per epic"
            )
        seen.add(person.name)
        owners.append(Owner(person=person.name, fte=pct / 100.0))
    return owners


def normalize_all(cfg: Config, raws: list[RawIssue]) -> list[Epic]:
    epics = [e for e in (normalize(cfg, r) for r in raws) if e is not None]
    if not epics:
        raise JiraError(
            f"no open epic among the {len(raws)} issues received — "
            f"did they all fall into done_statuses ({sorted(cfg.jira.done_statuses)})?"
        )
    return epics


def unknown_skills(cfg: Config, epics: list[Epic]) -> dict[str, list[str]]:
    """Skills asked for by epics that nobody in the roster has."""
    have = cfg.all_skills()
    gaps: dict[str, list[str]] = {}
    for epic in epics:
        for skill in sorted(epic.skills - have):
            gaps.setdefault(skill, []).append(epic.key)
    return gaps
