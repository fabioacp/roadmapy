"""Loads and validates config/config.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


class ConfigError(Exception):
    """Invalid config — the message is shown straight to the user."""


def _parse_date(value: str, where: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"{where}: invalid date {value!r} (use YYYY-MM-DD)") from exc


@dataclass
class Absence:
    start: date
    end: date

    def covers(self, day: date) -> bool:
        return self.start <= day <= self.end


def slug(text: str) -> str:
    """Turn 'Ana Souza' into 'ana-souza' — the shape an owner: label needs."""
    return "-".join(text.lower().split())


@dataclass
class Person:
    name: str
    seniority: str
    skills: set[str]
    fte: float
    alias: str = ""  # matches owner:<alias> in Jira; defaults to slug(name)
    pto: list[Absence] = field(default_factory=list)

    def available_on(self, day: date) -> bool:
        return not any(a.covers(day) for a in self.pto)


@dataclass
class JiraApiSpec:
    transport: str  # "stub" | "http"
    base_url: str
    email: str
    token_env: str
    jql: str
    page_size: int
    timeout_seconds: float
    stub_file: str
    fields: dict[str, str]


@dataclass
class JiraSpec:
    source: str  # "csv" | "api"
    api: JiraApiSpec
    csv: str
    raw_csv: str  # the raw Jira export that `dpe clean` reads (file or folder)
    columns: dict[str, str]
    estimate_unit: str
    points_to_days: float
    default_estimate_days: float
    skill_label_prefix: str
    min_seniority_label_prefix: str
    owner_label_prefix: str
    start_label_prefix: str
    end_label_prefix: str
    done_statuses: set[str]
    priority_order: list[str]

    def priority_rank(self, priority: str) -> int:
        """Lower = more urgent. Unknown priority falls to the end."""
        try:
            return self.priority_order.index(priority)
        except ValueError:
            return len(self.priority_order)


@dataclass
class Config:
    team_name: str
    priority_overrides: dict[str, str]  # DPE-101 -> "Highest"
    hours_per_week: float
    overhead_pct: float
    default_assignment_fte: float
    fiscal_year_start_month: int
    throughput: dict[str, float]
    seniority_order: list[str]
    holidays: set[date]
    people: list[Person]
    jira: JiraSpec
    root: Path

    def seniority_rank(self, seniority: str) -> int:
        try:
            return self.seniority_order.index(seniority)
        except ValueError:
            return -1

    def person(self, name: str) -> Person | None:
        lowered = name.strip().lower()
        for p in self.people:
            if p.name.lower() == lowered:
                return p
        return None

    def person_by_alias(self, alias: str) -> Person | None:
        """Resolve an owner: label slug to a roster person."""
        wanted = alias.strip().lower()
        for p in self.people:
            if p.alias == wanted:
                return p
        return None

    def daily_capacity(self, person: Person) -> float:
        """Senior-days delivered per working day worked."""
        mult = self.throughput.get(person.seniority)
        if mult is None:
            raise ConfigError(
                f"{person.name}: seniority {person.seniority!r} does not exist in [throughput]"
            )
        return person.fte * (1.0 - self.overhead_pct) * mult

    def all_skills(self) -> set[str]:
        return {s for p in self.people for s in p.skills}

    def resolve(self, relative: str) -> Path:
        """Resolve a data path from the config.

        Absolute wins. Relative is looked up first from the project root
        (config.toml's grandparent), then from the current directory — so that
        `-c /somewhere/else/config.toml` still finds the data.
        """
        candidate = Path(relative)
        if candidate.is_absolute():
            return candidate
        from_root = self.root / candidate
        if from_root.exists():
            return from_root
        from_cwd = Path.cwd() / candidate
        if from_cwd.exists():
            return from_cwd
        return from_root  # nowhere to be found: fail pointing at the root

    def fiscal_quarter(self, day: date) -> str:
        """Fiscal quarter label for a date.

        Q1 starts in `fiscal_year_start_month`. The FY is named after the
        calendar year it ENDS in — with a July start, 2026-08-14 is Q1 FY27.
        A start month of 1 degrades to plain calendar quarters.
        """
        offset = (day.month - self.fiscal_year_start_month) % 12
        quarter = offset // 3 + 1
        year = day.year + (1 if day.month >= self.fiscal_year_start_month else 0)
        if self.fiscal_year_start_month == 1:
            year = day.year
        return f"Q{quarter} FY{year % 100:02d}"

    def priority_for(self, epic_key: str) -> str | None:
        """Priority you forced in the config, if any."""
        return self.priority_overrides.get(epic_key.strip().upper())


def load(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    team = raw.get("team", {})
    overhead = float(team.get("overhead_pct", 0.0))
    if not 0.0 <= overhead < 1.0:
        raise ConfigError("[team] overhead_pct must be between 0.0 and 0.99")

    throughput = {k: float(v) for k, v in raw.get("throughput", {}).items()}
    if not throughput:
        raise ConfigError("[throughput] cannot be empty")

    seniority_order = list(raw.get("seniority", {}).get("order", []))
    if not seniority_order:
        seniority_order = sorted(throughput, key=lambda k: throughput[k])
    unknown = set(throughput) - set(seniority_order)
    if unknown:
        raise ConfigError(
            f"seniorities in [throughput] missing from [seniority].order: {sorted(unknown)}"
        )

    holidays = {
        _parse_date(d, "[calendar].holidays")
        for d in raw.get("calendar", {}).get("holidays", [])
    }

    people: list[Person] = []
    for idx, entry in enumerate(raw.get("people", [])):
        name = str(entry.get("name", "")).strip()
        if not name:
            raise ConfigError(f"[[people]] #{idx + 1}: field 'name' is required")
        seniority = str(entry.get("seniority", "")).strip()
        if seniority not in throughput:
            raise ConfigError(
                f"{name}: seniority {seniority!r} does not exist in [throughput] "
                f"(options: {sorted(throughput)})"
            )
        fte = float(entry.get("fte", 1.0))
        if not 0.0 < fte <= 1.0:
            raise ConfigError(f"{name}: fte must be between 0.01 and 1.0 (got {fte})")
        pto = []
        for slot in entry.get("pto", []):
            start = _parse_date(slot["start"], f"{name} pto.start")
            end = _parse_date(slot["end"], f"{name} pto.end")
            if end < start:
                raise ConfigError(f"{name}: pto ends ({end}) before it starts ({start})")
            pto.append(Absence(start, end))
        skills = {str(s).strip().lower() for s in entry.get("skills", []) if str(s).strip()}
        alias = str(entry.get("alias", "")).strip().lower() or slug(name)
        people.append(Person(name, seniority, skills, fte, alias, pto))

    if not people:
        raise ConfigError("no people in [[people]] — the roadmap would be empty")

    names = [p.name.lower() for p in people]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"duplicate names in [[people]]: {sorted(dupes)}")

    aliases = [p.alias for p in people]
    alias_dupes = {a for a in aliases if aliases.count(a) > 1}
    if alias_dupes:
        raise ConfigError(
            f"duplicate owner aliases in [[people]]: {sorted(alias_dupes)} — "
            f"set a unique 'alias' on the clashing people"
        )

    default_fte = float(team.get("default_assignment_fte", 1.0))
    if not 0.0 < default_fte <= 1.0:
        raise ConfigError("[team] default_assignment_fte must be between 0.01 and 1.0")

    fy_start = int(team.get("fiscal_year_start_month", 7))
    if not 1 <= fy_start <= 12:
        raise ConfigError("[team] fiscal_year_start_month must be between 1 and 12")

    jira_raw = raw.get("jira", {})
    unit = str(jira_raw.get("estimate_unit", "points")).lower()
    if unit not in ("points", "days"):
        raise ConfigError("[jira] estimate_unit must be 'points' or 'days'")
    columns = jira_raw.get("columns", {})
    for required in ("key", "summary"):
        if required not in columns:
            raise ConfigError(f"[jira.columns] missing the required mapping '{required}'")

    source = str(jira_raw.get("source", "csv")).lower()
    if source not in ("csv", "api"):
        raise ConfigError("[jira] source must be 'csv' or 'api'")

    api_raw = jira_raw.get("api", {})
    transport = str(api_raw.get("transport", "stub")).lower()
    if transport not in ("stub", "http"):
        raise ConfigError("[jira.api] transport must be 'stub' or 'http'")
    api = JiraApiSpec(
        transport=transport,
        base_url=str(api_raw.get("base_url", "")).strip(),
        email=str(api_raw.get("email", "")).strip(),
        token_env=str(api_raw.get("token_env", "JIRA_API_TOKEN")),
        jql=str(api_raw.get("jql", "type = Epic")),
        page_size=max(1, int(api_raw.get("page_size", 100))),
        timeout_seconds=float(api_raw.get("timeout_seconds", 30)),
        stub_file=str(api_raw.get("stub_file", "data/jira_api_stub.json")),
        fields={k: str(v) for k, v in api_raw.get("fields", {}).items()},
    )

    jira = JiraSpec(
        source=source,
        api=api,
        csv=str(jira_raw.get("csv", "data/epics.csv")),
        raw_csv=str(jira_raw.get("raw_csv", "data/raw")),
        columns={k: str(v) for k, v in columns.items()},
        estimate_unit=unit,
        points_to_days=float(jira_raw.get("points_to_days", 1.0)),
        default_estimate_days=float(jira_raw.get("default_estimate_days", 10)),
        skill_label_prefix=str(jira_raw.get("skill_label_prefix", "skill:")),
        min_seniority_label_prefix=str(jira_raw.get("min_seniority_label_prefix", "min:")),
        owner_label_prefix=str(jira_raw.get("owner_label_prefix", "owner:")),
        start_label_prefix=str(jira_raw.get("start_label_prefix", "start:")),
        end_label_prefix=str(jira_raw.get("end_label_prefix", "end:")),
        done_statuses={str(s).lower() for s in jira_raw.get("done_statuses", [])},
        priority_order=[str(p) for p in jira_raw.get("priority_order", [])],
    )

    overrides: dict[str, str] = {}
    valid = jira.priority_order
    for key, value in raw.get("priority", {}).items():
        level = str(value).strip()
        if valid and level not in valid:
            raise ConfigError(
                f"[priority] {key}: {level!r} is not a valid priority. "
                f"Use one of: {valid}"
            )
        overrides[str(key).strip().upper()] = level

    return Config(
        team_name=str(team.get("name", "Team")),
        priority_overrides=overrides,
        hours_per_week=float(team.get("hours_per_week", 40)),
        overhead_pct=overhead,
        default_assignment_fte=default_fte,
        fiscal_year_start_month=fy_start,
        throughput=throughput,
        seniority_order=seniority_order,
        holidays=holidays,
        people=people,
        jira=jira,
        root=path.parent.parent,
    )
