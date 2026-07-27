"""Builds the roadmap from planned dates, and computes availability.

The model is date-driven, not effort-driven:

  ACTIVE — an epic with start: and end: labels. Its period is given, not
           computed; the tool just plots it, in each owner's lane.

  QUEUE  — an epic with no planned period. Ordered by priority. For each, the
           tool reports the first skill-matched person to free up, and how long
           the wait is.

A person is "free from" the latest end date among the epics they are actively
on (or today, if they hold none). The queue is INDEPENDENT: every queued epic is
measured against that same availability — taking one does not make its owner
busy for the next. That keeps each answer a clean "earliest possible start".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .config import Config, Person
from .jira import Epic


# --------------------------------------------------------------------------- #
# Eligibility — who may work an epic, by skill and seniority
# --------------------------------------------------------------------------- #


def eligible(cfg: Config, person: Person, epic: Epic) -> tuple[bool, str]:
    missing = epic.skills - person.skills
    if missing:
        return False, f"missing skills: {', '.join(sorted(missing))}"
    if epic.min_seniority:
        if cfg.seniority_rank(person.seniority) < cfg.seniority_rank(epic.min_seniority):
            return False, f"requires {epic.min_seniority} or above"
    return True, ""


def candidates(cfg: Config, epic: Epic) -> tuple[list[Person], list[str]]:
    """People who could take the epic. If it carries owner: labels, those people
    are the candidates; otherwise everyone who has the skills and seniority."""
    if epic.owners:
        return [cfg.person(o.person) for o in epic.owners], []
    ok: list[Person] = []
    why: list[str] = []
    for person in cfg.people:
        fits, reason = eligible(cfg, person, epic)
        (ok if fits else why).append(person if fits else f"{person.name}: {reason}")
    return ok, why


def owner_warning(cfg: Config, epic: Epic) -> str:
    """Flag owner labels that, as a group, don't cover the epic's requirements."""
    people = [cfg.person(o.person) for o in epic.owners]
    union = set().union(*(p.skills for p in people)) if people else set()
    notes = []
    missing = epic.skills - union
    if missing:
        notes.append(f"team lacks skills: {', '.join(sorted(missing))}")
    if epic.min_seniority and not any(
        cfg.seniority_rank(p.seniority) >= cfg.seniority_rank(epic.min_seniority)
        for p in people
    ):
        notes.append(f"no owner is {epic.min_seniority}+")
    return "; ".join(notes)


def _priority_key(cfg: Config, epic: Epic) -> tuple:
    return (cfg.jira.priority_rank(epic.priority), epic.due or date.max, epic.key)


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #


@dataclass
class ActiveWork:
    """One person on one scheduled epic, over its planned period."""

    epic: Epic
    person: Person | None       # None = scheduled period with no owner label
    start: date
    end: date
    co_owners: list[str] = field(default_factory=list)  # other owners' names

    @property
    def warning(self) -> str:
        return self._warning

    _warning: str = ""


@dataclass
class Availability:
    """When a person frees up, and what they are on now."""

    person: Person
    free_from: date               # today if free now
    active: list[ActiveWork] = field(default_factory=list)

    @property
    def busy(self) -> bool:
        return bool(self.active)

    def wait_days(self, today: date) -> int:
        return max(0, (self.free_from - today).days)


@dataclass
class QueueItem:
    """A waiting epic and the earliest skill-matched person to free up."""

    epic: Epic
    person: Person | None         # earliest-available candidate; None if nobody
    available_on: date | None     # when that person frees
    candidates: list[Person] = field(default_factory=list)
    reason: str = ""              # why nobody is eligible

    def wait_days(self, today: date) -> int | None:
        if self.available_on is None:
            return None
        return max(0, (self.available_on - today).days)


@dataclass
class Plan:
    active: list[ActiveWork]
    availability: dict[str, Availability]   # keyed by person name
    queue: list[QueueItem]

    @property
    def scheduled_epics(self) -> list[Epic]:
        seen, out = set(), []
        for aw in self.active:
            if aw.epic.key not in seen:
                seen.add(aw.epic.key)
                out.append(aw.epic)
        return out


def is_active(epic: Epic) -> bool:
    """Active = a planned period AND an owner. An epic with no owner has no real
    start (nobody is committed to it), so any start:/end: labels are ignored and
    it goes to the queue, plotted from when a skilled person frees up."""
    return epic.scheduled and bool(epic.owners)


def build_plan(cfg: Config, epics: list[Epic], today: date) -> Plan:
    # 1. Active work: owned epics with a planned period, one row per owner.
    active: list[ActiveWork] = []
    for epic in epics:
        if not is_active(epic):
            continue
        warn = owner_warning(cfg, epic)
        for o in epic.owners:
            others = [x.person for x in epic.owners if x.person != o.person]
            active.append(ActiveWork(
                epic, cfg.person(o.person), epic.planned_start, epic.planned_end,
                co_owners=others, _warning=warn,
            ))

    # 2. Availability: each person is free from the latest end of their active work.
    availability: dict[str, Availability] = {}
    for person in cfg.people:
        mine = [aw for aw in active if aw.person is person]
        free_from = max([today] + [aw.end for aw in mine])
        availability[person.name] = Availability(person, free_from, mine)

    # 3. Queue: everything not active, priority order, each vs. the same availability.
    queue: list[QueueItem] = []
    for epic in sorted((e for e in epics if not is_active(e)),
                       key=lambda e: _priority_key(cfg, e)):
        pool, why_not = candidates(cfg, epic)
        pool = [p for p in pool if p is not None]
        if not pool:
            queue.append(QueueItem(
                epic, None, None, reason="; ".join(why_not) or "no eligible person"))
            continue
        best = min(pool, key=lambda p: (availability[p.name].free_from, p.name))
        queue.append(QueueItem(
            epic, best, availability[best.name].free_from, candidates=pool))

    return Plan(active=active, availability=availability, queue=queue)


def availability_for(
    cfg: Config, plan: Plan, skills: set[str], min_seniority: str | None
) -> tuple[list[tuple[Person, date]], list[str]]:
    """Who could take a new request with these skills, and when they free up.

    Sorted earliest-available first. Also returns why the others were ruled out.
    """
    probe = Epic(
        key="NEW", summary="", status="", assignee=None, priority="",
        estimate_days=0, estimate_missing=False, skills=skills,
        min_seniority=min_seniority, due=None,
    )
    pool, why_not = candidates(cfg, probe)
    ranked = sorted(
        ((p, plan.availability[p.name].free_from) for p in pool if p is not None),
        key=lambda pair: (pair[1], pair[0].name),
    )
    return ranked, why_not
