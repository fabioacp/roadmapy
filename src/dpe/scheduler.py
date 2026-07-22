"""Assigns epics to people and produces the roadmap.

Two very different things live here, and the distinction is the point:

  COMMITTED  — epics carrying owner:<alias> labels in Jira. Ownership (who, at
               what FTE) is fact; the tool never picks or reassigns, it only
               computes the dates. When one person owns several epics, the
               strategy order decides which of theirs runs first — so an owned
               epic's dates can shift, but never its owners. Several owner
               labels on one epic mean shared ownership.

  SUGGESTED  — every epic with no owner label. The tool proposes who would take
               it, based on skill + seniority + first open window. It is a
               proposal, and the report marks it as such.

Model: each person has a "ledger" — how much capacity (in senior-days) is still
left on each working day. Owned work consumes the ledger first, at the FTE the
label declared. Suggestions compete for whatever is left.

Shared ownership is co-scheduled: all owners burn the epic's effort together,
each contributing their free capacity × FTE per working day, and they finish
together when the total effort is spent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .config import Config, Person
from .jira import Epic, Owner

HORIZON_DAYS = 365 * 4


@dataclass
class Ledger:
    """Remaining capacity per working day, for one person."""

    person: Person
    cfg: Config
    used: dict[date, float] = field(default_factory=dict)

    def copy(self) -> Ledger:
        return Ledger(self.person, self.cfg, dict(self.used))

    def is_workday(self, day: date) -> bool:
        return (
            day.weekday() < 5
            and day not in self.cfg.holidays
            and self.person.available_on(day)
        )

    def capacity_on(self, day: date) -> float:
        return self.cfg.daily_capacity(self.person) if self.is_workday(day) else 0.0

    def remaining_on(self, day: date) -> float:
        return max(0.0, self.capacity_on(day) - self.used.get(day, 0.0))

    def plan(
        self, days_needed: float, not_before: date, share: float = 1.0
    ) -> tuple[date, date, dict[date, float]] | None:
        """Where this work would land, without writing anything to the ledger.

        `share` is the FTE dedicated to this epic: 0.5 means the person spends
        half of their daily capacity here, leaving the other half free to run
        another epic in parallel.
        """
        remaining = days_needed
        cursor = not_before
        cap = self.cfg.daily_capacity(self.person) * share
        spend: dict[date, float] = {}
        start: date | None = None
        limit = not_before + timedelta(days=HORIZON_DAYS)

        while remaining > 1e-9:
            if cursor > limit:
                return None
            free = min(self.remaining_on(cursor), cap)
            if free > 1e-9:
                take = min(free, remaining)
                spend[cursor] = take
                remaining -= take
                if start is None:
                    start = cursor
            cursor += timedelta(days=1)

        assert start is not None
        return start, max(spend), spend

    def commit(self, spend: dict[date, float]) -> None:
        for day, amount in spend.items():
            self.used[day] = self.used.get(day, 0.0) + amount


@dataclass
class Option:
    """A person who could take the epic, and when."""

    person: Person
    start: date
    end: date
    slack_days: int  # days of wait until there is free capacity
    spend: dict[date, float] = field(default_factory=dict)


COMMITTED = "committed"
SUGGESTED = "suggested"
UNSTAFFABLE = "unstaffable"


@dataclass
class Assignment:
    """One (epic, person) row. A shared epic produces one Assignment per owner,
    all sharing the same start/end window; co_owners lists the others."""

    epic: Epic
    person: Person | None
    start: date | None
    end: date | None
    kind: str = SUGGESTED
    fte: float = 1.0
    reason: str = ""       # why nobody took it (kind == UNSTAFFABLE)
    warning: str = ""      # owner labels that violate skill/seniority
    co_owners: list[tuple[str, float]] = field(default_factory=list)  # (name, fte)
    alternatives: list[Option] = field(default_factory=list)

    @property
    def staffed(self) -> bool:
        return self.person is not None

    @property
    def committed(self) -> bool:
        return self.kind == COMMITTED


@dataclass
class EpicPlan:
    """A whole epic, its owners grouped back together (one row per epic)."""

    epic: Epic
    kind: str
    start: date | None
    end: date | None
    owners: list[Assignment]   # per-owner rows; [] when unstaffable
    warning: str = ""
    reason: str = ""
    alternatives: list[Option] = field(default_factory=list)

    @property
    def staffed(self) -> bool:
        return bool(self.owners) and self.start is not None

    @property
    def committed(self) -> bool:
        return self.kind == COMMITTED

    @property
    def owner_names(self) -> list[str]:
        return [a.person.name for a in self.owners if a.person]


def by_epic(assignments: list[Assignment]) -> list[EpicPlan]:
    """Regroup per-owner rows back into one EpicPlan per epic, order preserved."""
    order: list[str] = []
    groups: dict[str, list[Assignment]] = {}
    for a in assignments:
        if a.epic.key not in groups:
            groups[a.epic.key] = []
            order.append(a.epic.key)
        groups[a.epic.key].append(a)

    plans: list[EpicPlan] = []
    for key in order:
        rows = groups[key]
        first = rows[0]
        owners = [r for r in rows if r.person]
        plans.append(EpicPlan(
            epic=first.epic, kind=first.kind, start=first.start, end=first.end,
            owners=owners, warning=first.warning, reason=first.reason,
            alternatives=first.alternatives,
        ))
    return plans


def eligible(cfg: Config, person: Person, epic: Epic) -> tuple[bool, str]:
    missing = epic.skills - person.skills
    if missing:
        return False, f"missing skills: {', '.join(sorted(missing))}"
    if epic.min_seniority:
        if cfg.seniority_rank(person.seniority) < cfg.seniority_rank(epic.min_seniority):
            return False, f"requires {epic.min_seniority} or above"
    return True, ""


def candidates(cfg: Config, epic: Epic) -> tuple[list[Person], list[str]]:
    ok: list[Person] = []
    why: list[str] = []
    for person in cfg.people:
        fits, reason = eligible(cfg, person, epic)
        if fits:
            ok.append(person)
        else:
            why.append(f"{person.name}: {reason}")
    return ok, why


# Backlog ordering strategies. Each one answers a different question,
# and `./dpe compare` shows what each choice costs.
STRATEGIES: dict[str, str] = {
    "priority": "priority first (the order you defined)",
    "deadline": "tightest deadline first",
    "quick-wins": "smaller epics first (drains the queue faster)",
}
DEFAULT_STRATEGY = "priority"


def _sort_key(cfg: Config, epic: Epic, strategy: str = DEFAULT_STRATEGY) -> tuple:
    rank = cfg.jira.priority_rank(epic.priority)
    due = epic.due or date.max
    if strategy == "deadline":
        return (due, rank, epic.key)
    if strategy == "quick-wins":
        return (epic.estimate_days, rank, due, epic.key)
    return (rank, due, epic.key)


def _co_schedule(
    cfg: Config, epic: Epic, ledgers: dict[str, Ledger], start_from: date
) -> tuple[date, date, dict[str, dict[date, float]]] | None:
    """Burn the epic's effort across all its owners at once.

    Each working day, every owner contributes their free capacity capped at
    (daily_capacity × their FTE). Contributions are pooled until the epic's total
    effort is spent, so the owners finish together. Returns (start, end, per-owner
    spend), or None if it never fits within the horizon.
    """
    remaining = epic.estimate_days
    day = start_from
    limit = start_from + timedelta(days=HORIZON_DAYS)
    spends: dict[str, dict[date, float]] = {o.person: {} for o in epic.owners}
    start: date | None = None

    while remaining > 1e-9:
        if day > limit:
            return None
        avail: list[tuple[Owner, float]] = []
        for o in epic.owners:
            ledger = ledgers[o.person]
            rate = cfg.daily_capacity(ledger.person) * o.fte
            avail.append((o, min(ledger.remaining_on(day), rate)))
        total = sum(free for _, free in avail)
        if total > 1e-9:
            take = min(total, remaining)
            for o, free in avail:
                if free > 1e-9:
                    spends[o.person][day] = free / total * take
            remaining -= take
            if start is None:
                start = day
        day += timedelta(days=1)

    assert start is not None
    end = max(max(s) for s in spends.values() if s)
    return start, end, spends


def _owner_warning(cfg: Config, epic: Epic) -> str:
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


def schedule(
    cfg: Config,
    epics: list[Epic],
    start_from: date,
    max_alternatives: int = 3,
    strategy: str = DEFAULT_STRATEGY,
) -> tuple[list[Assignment], dict[str, Ledger]]:
    """Computes dates for owned work, then suggests an owner for the rest."""
    if strategy not in STRATEGIES:
        raise ValueError(f"strategy {strategy!r} — use one of: {sorted(STRATEGIES)}")
    ledgers = {p.name: Ledger(p, cfg) for p in cfg.people}
    ordered = sorted(epics, key=lambda e: _sort_key(cfg, e, strategy))
    assignments: list[Assignment] = []

    # --- 1. Owned: owner: labels are fact, the tool only computes the dates. -
    open_epics: list[Epic] = []
    for epic in ordered:
        if not epic.owned:
            open_epics.append(epic)
            continue

        def co(owner: Owner) -> list[tuple[str, float]]:
            return [(x.person, x.fte) for x in epic.owners if x.person != owner.person]

        planned = _co_schedule(cfg, epic, ledgers, start_from)
        if planned is None:
            for o in epic.owners:
                assignments.append(Assignment(
                    epic, cfg.person(o.person), None, None, kind=COMMITTED,
                    fte=o.fte, co_owners=co(o), reason="no capacity within horizon",
                ))
            continue

        p_start, p_end, spends = planned
        warn = _owner_warning(cfg, epic)  # owners honoured even if they violate it
        for o in epic.owners:
            ledgers[o.person].commit(spends[o.person])
        for o in epic.owners:
            assignments.append(Assignment(
                epic, cfg.person(o.person), p_start, p_end, kind=COMMITTED,
                fte=o.fte, co_owners=co(o), warning=warn,
            ))

    # --- 2. Open: the tool suggests, competing for the capacity left over. ---
    share = cfg.default_assignment_fte
    for epic in open_epics:
        pool, why_not = candidates(cfg, epic)
        if not pool:
            assignments.append(Assignment(
                epic, None, None, None, kind=UNSTAFFABLE,
                reason="; ".join(why_not) or "empty roster",
            ))
            continue

        options: list[Option] = []
        for person in pool:
            planned = ledgers[person.name].plan(epic.estimate_days, start_from, share)
            if planned is None:
                continue
            p_start, p_end, spend = planned
            options.append(Option(person, p_start, p_end, (p_start - start_from).days, spend))

        if not options:
            assignments.append(Assignment(
                epic, None, None, None, kind=UNSTAFFABLE,
                reason="no capacity within horizon",
            ))
            continue

        options.sort(key=lambda o: (o.end, o.start, o.person.name))
        best = options[0]
        ledgers[best.person.name].commit(best.spend)
        assignments.append(Assignment(
            epic, best.person, best.start, best.end, kind=SUGGESTED, fte=share,
            alternatives=options[1:max_alternatives],
        ))

    return assignments, ledgers


# --------------------------------------------------------------------------- #
# New request simulation
# --------------------------------------------------------------------------- #




def simulate(
    cfg: Config,
    ledgers: dict[str, Ledger],
    skills: set[str],
    estimate_days: float,
    min_seniority: str | None,
    start_from: date,
) -> tuple[list[Option], list[str]]:
    """When a new request could start and finish, per eligible person.

    Runs on copies of the ledgers, so it does not alter the committed roadmap.
    """
    probe = Epic(
        key="NEW",
        summary="simulated request",
        status="",
        assignee=None,
        priority="",
        estimate_days=estimate_days,
        estimate_missing=False,
        skills=skills,
        min_seniority=min_seniority,
        due=None,
    )
    pool, why_not = candidates(cfg, probe)

    options: list[Option] = []
    for person in pool:
        planned = ledgers[person.name].copy().plan(
            estimate_days, start_from, cfg.default_assignment_fte
        )
        if planned is None:
            continue
        p_start, p_end, _ = planned
        options.append(Option(person, p_start, p_end, (p_start - start_from).days))

    options.sort(key=lambda o: (o.end, o.start, o.person.name))
    return options, why_not


# --------------------------------------------------------------------------- #
# Utilization
# --------------------------------------------------------------------------- #


def monthly_utilization(
    cfg: Config, ledgers: dict[str, Ledger], start: date, months: int
) -> tuple[list[str], dict[str, dict[str, tuple[float, float]]]]:
    """Returns (month labels, {person: {month: (used, capacity)}}) in senior-days."""
    labels: list[str] = []
    cursor = date(start.year, start.month, 1)
    for _ in range(months):
        labels.append(f"{cursor:%Y-%m}")
        cursor = date(cursor.year + (cursor.month == 12), (cursor.month % 12) + 1, 1)

    table: dict[str, dict[str, tuple[float, float]]] = {}
    for person in cfg.people:
        ledger = ledgers[person.name]
        row: dict[str, tuple[float, float]] = {label: (0.0, 0.0) for label in labels}
        # Starts at `start`, not on the 1st: in the current month only the days
        # still left count, otherwise today's utilization looks artificially low.
        day = start
        horizon_end = cursor
        while day < horizon_end:
            label = f"{day:%Y-%m}"
            if label in row:
                used, cap = row[label]
                row[label] = (used + ledger.used.get(day, 0.0), cap + ledger.capacity_on(day))
            day += timedelta(days=1)
        table[person.name] = row
    return labels, table


# --------------------------------------------------------------------------- #
# Metrics for a plan — used by `./dpe compare`
# --------------------------------------------------------------------------- #


@dataclass
class Metrics:
    strategy: str
    makespan: date | None       # when the last epic finishes
    late_epics: int             # how many pass their due date
    late_days: int              # sum of days late
    avg_wait: float             # average wait before starting (days)
    first_delivery: date | None
    unstaffed: int

    @property
    def horizon_days(self) -> int:
        return 0 if self.makespan is None else (self.makespan - self._origin).days

    _origin: date = date.min


def measure(assignments: list[Assignment], start_from: date, strategy: str) -> Metrics:
    # Group first, so a shared epic counts once, not once per owner.
    plans = by_epic(assignments)
    staffed = [p for p in plans if p.staffed and p.end]
    late = [p for p in staffed if p.epic.due and p.end > p.epic.due]
    waits = [(p.start - start_from).days for p in staffed]
    return Metrics(
        strategy=strategy,
        makespan=max((p.end for p in staffed), default=None),
        late_epics=len(late),
        late_days=sum((p.end - p.epic.due).days for p in late),
        avg_wait=sum(waits) / len(waits) if waits else 0.0,
        first_delivery=min((p.end for p in staffed), default=None),
        unstaffed=sum(1 for p in plans if not p.staffed),
        _origin=start_from,
    )
