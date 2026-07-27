"""Command-line interface for the capacity planner."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from . import clean as clean_mod
from . import report, sources
from .config import Config, ConfigError, load
from .jira import Epic, JiraError, normalize_all, unknown_skills
from .scheduler import (Plan, availability_for, build_plan, candidates,
                        eligible, is_active, owner_warning)
from .sources import csv_source

# Default: the project's own config, so `dpe` works from any directory.
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent.parent / "config" / "config.toml"


# --------------------------------------------------------------------------- #
# output helpers
# --------------------------------------------------------------------------- #

def table(headers: list[str], rows: list[list[str]], right: set[int] = frozenset()) -> str:
    if not rows:
        return "  (empty)"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells: list[str]) -> str:
        out = []
        for i, cell in enumerate(cells):
            out.append(cell.rjust(widths[i]) if i in right else cell.ljust(widths[i]))
        return "  " + "  ".join(out).rstrip()

    lines = [fmt(headers), "  " + "  ".join("─" * w for w in widths)]
    lines.extend(fmt(r) for r in rows)
    return "\n".join(lines)


def parse_day(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date {value!r} — use YYYY-MM-DD") from None


def load_all(args) -> tuple[Config, list[Epic]]:
    cfg = load(Path(args.config))
    return cfg, sources.load_epics(cfg, getattr(args, "source", None))


def load_plan(args) -> tuple[Config, list[Epic], date, Plan]:
    cfg, epics = load_all(args)
    today = args.date or date.today()
    return cfg, epics, today, build_plan(cfg, epics, today)


def _who(work_rows) -> str:
    """Owner names + FTE for a shared epic."""
    parts = []
    for aw in work_rows:
        name = aw.person.name.split()[0] if aw.person else "(unassigned)"
        fte = next((o.fte for o in aw.epic.owners if aw.person and o.person == aw.person.name), 1.0)
        parts.append(name + (f" {fte:.0%}" if fte < 0.999 else ""))
    return ", ".join(parts)


def quarter_of(cfg: Config, day: date) -> str:
    return cfg.fiscal_quarter(day)


def _prio_cell(epic) -> str:
    return (epic.priority or "—") + ("*" if epic.priority_forced else "")


# --------------------------------------------------------------------------- #
# roadmap views  (active work + queue)
# --------------------------------------------------------------------------- #

def _active_by_epic(plan: Plan):
    """Group active work rows back to one entry per epic."""
    groups, order = {}, []
    for aw in plan.active:
        if aw.epic.key not in groups:
            groups[aw.epic.key] = []
            order.append(aw.epic.key)
        groups[aw.epic.key].append(aw)
    return [(k, groups[k]) for k in order]


def _queue_rows(cfg, plan, today):
    rows = []
    for q in plan.queue:
        if q.person is None:
            rows.append([q.epic.key, q.epic.summary[:34], _prio_cell(q.epic),
                         "— nobody eligible —", "—", "—"])
            continue
        wait = q.wait_days(today)
        rows.append([
            q.epic.key, q.epic.summary[:34], _prio_cell(q.epic),
            q.person.name.split()[0] + f" ({q.person.seniority})",
            f"{q.available_on:%d/%m/%y}",
            "now" if wait == 0 else f"{wait}d",
        ])
    return rows


def view_person(cfg, plan, today) -> str:
    out = []
    for person in cfg.people:
        av = plan.availability[person.name]
        status = "free now" if not av.busy or av.free_from <= today else \
            f"free from {av.free_from:%d/%m/%Y}"
        out.append(f"\n  {person.name}  ({person.seniority})  —  {status}"
                   f"  ·  skills: {', '.join(sorted(person.skills)) or '—'}")
        if not av.active:
            out.append("      (no active epic)")
        for aw in sorted(av.active, key=lambda x: x.start):
            shared = f"  +{len(aw.co_owners)} shared" if aw.co_owners else ""
            warn = "  ⚠" if aw.warning else ""
            out.append(f"      ▪ {aw.epic.key:<9} {aw.epic.summary[:36]:<38} "
                       f"{aw.start:%d/%m/%y} → {aw.end:%d/%m/%y}{shared}{warn}")
        # Queued epics this person would pick up next, from their free date.
        qs = sorted((q for q in plan.queue if q.person is person and q.available_on),
                    key=lambda x: x.available_on)
        for q in qs:
            wait = q.wait_days(today)
            when = "now" if wait == 0 else f"{q.available_on:%d/%m/%y} ({wait}d)"
            out.append(f"      ◇ {q.epic.key:<9} {q.epic.summary[:36]:<38} "
                       f"earliest start {when}")
    out.append("\n      ▪ active (start:/end:)      ◇ queued (earliest start when free)")
    return "\n".join(out)


def view_epic(cfg, plan, today) -> str:
    rows = []
    for _, works in sorted(_active_by_epic(plan), key=lambda kv: kv[1][0].start):
        e = works[0].epic
        deadline = ""
        if e.due:
            deadline = f"{(works[0].end - e.due).days}d late" if works[0].end > e.due else "on time"
        rows.append([e.key, e.summary[:36], _prio_cell(e), _who(works),
                     f"{works[0].start:%d/%m/%y}", f"{works[0].end:%d/%m/%y}", deadline])
    return table(["EPIC", "SUMMARY", "PRIOR.", "OWNER(S)", "START", "END", "DEADLINE"], rows)


def view_quarter(cfg, plan, today) -> str:
    buckets: dict[str, list] = {}
    for key, works in _active_by_epic(plan):
        buckets.setdefault(quarter_of(cfg, works[0].end), []).append(works)
    out = []
    for q, items in sorted(buckets.items()):
        late = sum(1 for w in items if w[0].epic.due and w[0].end > w[0].epic.due)
        alert = f"  ⚠ {late} past deadline" if late else ""
        out.append(f"\n  {q}  —  {len(items)} epic(s){alert}")
        for works in items:
            e = works[0].epic
            out.append(f"      ▪ {e.key:<9} {e.summary[:38]:<40} "
                       f"{_who(works)[:18]:<18} {works[0].start:%d/%m} → {works[0].end:%d/%m}")
    return "\n".join(out)


VIEWS = {
    "person": ("each person's schedule and availability", view_person),
    "epic": ("the active epics and their periods", view_epic),
    "quarter": ("active epics grouped by fiscal quarter", view_quarter),
}


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_clean(args) -> int:
    cfg = load(Path(args.config))
    raw_path, clean_path, stats = clean_mod.clean(cfg, args.raw)
    print(f"\n  Raw:    {raw_path}  (exported {stats['raw_mtime']:%d/%m/%Y %H:%M})")
    print(f"  Clean:  {clean_path}")
    print(f"\n✓ kept {stats['kept_rows']} epic(s), {stats['kept_columns']} columns "
          f"(dropped {stats['raw_columns'] - stats['kept_columns']} unused columns)")
    print(f"\n  Now run:  ./dpe roadmap --html out/roadmap.html")
    return 0


def cmd_validate(args) -> int:
    cfg = load(Path(args.config))
    print(f"✓ config OK — {cfg.team_name}, {len(cfg.people)} people")
    for p in cfg.people:
        print(f"    {p.name:<16} {p.seniority:<10} owner:{p.alias:<14} "
              f"skills: {', '.join(sorted(p.skills)) or '—'}")

    source = (args.source or cfg.jira.source).lower()
    print(f"\n  Source: {sources.describe(cfg, source)}")
    if source == "csv":
        if clean_mod.is_stale(cfg):
            print("  ⚠ the raw export is newer than the cleaned CSV — run `./dpe clean`")
        header, rows = csv_source.read_rows(cfg.resolve(cfg.jira.csv))
        print(f"✓ CSV read — {len(rows)} rows, {len(set(header))} distinct columns")
        missing = csv_source.missing_columns(cfg, header)
        if missing:
            print("\n✗ [jira.columns] mappings missing from the CSV:")
            for m in missing:
                print(f"    {m}")
            return 1
    elif cfg.jira.api.transport == "stub":
        print(f"  ⓘ transport = \"stub\" — nothing goes to the network.")

    raws = sources.fetch_raw(cfg, source)
    epics = normalize_all(cfg, raws)
    active = [e for e in epics if is_active(e)]
    queued = [e for e in epics if not is_active(e)]
    print(f"✓ {len(raws)} issues received · {len(epics)} open · "
          f"{len(active)} active (owner + start:/end:), {len(queued)} in the queue")

    gaps = unknown_skills(cfg, epics)
    if gaps:
        print("\n⚠ skills required that nobody on the roster has:")
        for skill, keys in sorted(gaps.items()):
            print(f"    {skill:<18} required by {', '.join(keys)}")

    # owner labels that don't cover the epic
    for e in active + queued:
        if e.owners:
            warn = owner_warning(cfg, e)
            if warn:
                who = ", ".join(o.person for o in e.owners)
                print(f"\n⚠ {e.key} owned by {who}, but {warn} — honoured, but check.")

    # start:/end: on an epic with no owner — dates ignored, sent to the queue
    ghosts = [e for e in queued if e.scheduled]
    if ghosts:
        print(f"\n  ⓘ {len(ghosts)} epic(s) have start:/end: but no owner: label — "
              f"treated as queued (their dates are ignored, plotted from availability):")
        for e in ghosts:
            print(f"    {e.key:<10} {e.summary[:40]}")

    # nobody can work two epics at once: flag overlapping active periods per person
    by_owner: dict[str, list] = {}
    for e in active:
        for o in e.owners:
            by_owner.setdefault(o.person, []).append(e)
    for name, owned in sorted(by_owner.items()):
        chain = sorted(owned, key=lambda x: x.planned_start)
        clashes = [(a.key, b.key) for a, b in zip(chain, chain[1:])
                   if b.planned_start <= a.planned_end]
        if clashes:
            print(f"\n⚠ {name} has overlapping active epics — one person can't do two "
                  f"at once. Adjust the start:/end: labels so they run back to back:")
            for a, b in clashes:
                print(f"    {a} overlaps {b}")

    # planned end past the Jira deadline
    late = [e for e in active if e.due and e.planned_end > e.due]
    if late:
        print(f"\n⚠ {len(late)} epic(s) planned to finish after their Due date:")
        for e in late:
            print(f"    {e.key:<10} planned {e.planned_end:%d/%m/%y} > due {e.due:%d/%m/%y}")

    if cfg.priority_overrides:
        by_key = {e.key.upper() for e in epics}
        orphans = [k for k in cfg.priority_overrides if k not in by_key]
        if orphans:
            print(f"\n⚠ [priority] keys not in the backlog: {', '.join(sorted(orphans))}")
    return 0


def cmd_roadmap(args) -> int:
    cfg, epics, today, plan = load_plan(args)
    view_label, render_view = VIEWS[args.view]

    if clean_mod.is_stale(cfg) and (args.source or cfg.jira.source) == "csv":
        print("⚠ the raw export is newer than the cleaned CSV — run `./dpe clean` first")

    print(f"\nRoadmap — {cfg.team_name}   (as of {today:%d/%m/%Y})")
    print(f"View: {args.view} ({view_label})\n")
    print(render_view(cfg, plan, today))

    # The queue is always shown — it is the point.
    print(f"\n\nQueue — {len(plan.queue)} epic(s) waiting, by priority")
    print("How long until someone with the right skills frees up.\n")
    print(table(["EPIC", "SUMMARY", "PRIOR.", "NEXT FREE", "AVAILABLE", "WAIT"],
                _queue_rows(cfg, plan, today), right={5}))

    blocked = [q for q in plan.queue if q.person is None]
    if blocked:
        print(f"\n⚠ {len(blocked)} epic(s) nobody on the team can take:")
        for q in blocked:
            need = ", ".join(sorted(q.epic.skills)) or "—"
            if q.epic.min_seniority:
                need += f" (min: {q.epic.min_seniority})"
            print(f"    {q.epic.key:<10} needs {need}")

    forced = [e for e in epics if e.priority_forced]
    if forced:
        print(f"\n  * {len(forced)} priority(ies) forced in [priority]: "
              + ", ".join(f"{e.key}={e.priority}" for e in forced))

    if args.html:
        out = Path(args.html)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report.render(cfg, epics, plan, today), encoding="utf-8")
        print(f"\n  HTML: {out.resolve()}")
    return 0


def cmd_queue(args) -> int:
    """The waiting epics, by priority, with who frees up and when."""
    cfg, epics, today, plan = load_plan(args)
    if not plan.queue:
        print("\n✓ Nothing in the queue — every epic has a planned period.")
        return 0
    print(f"\nQueue — {cfg.team_name}   ({len(plan.queue)} epic(s), by priority, "
          f"as of {today:%d/%m/%Y})\n")
    print(table(["EPIC", "SUMMARY", "PRIOR.", "NEXT FREE", "AVAILABLE", "WAIT"],
                _queue_rows(cfg, plan, today), right={5}))

    startable = [q for q in plan.queue if q.person and q.wait_days(today) == 0]
    if startable:
        print(f"\n  {len(startable)} can start NOW: "
              + ", ".join(f"{q.epic.key} ({q.person.name.split()[0]})" for q in startable))
    blocked = [q for q in plan.queue if q.person is None]
    if blocked:
        print(f"\n⚠ {len(blocked)} nobody can take (skill gap): "
              + ", ".join(q.epic.key for q in blocked))
    return 0


def cmd_availability(args) -> int:
    """Who is free when — the answer to 'who can take the next epic?'."""
    cfg, epics, today, plan = load_plan(args)
    print(f"\nAvailability — {cfg.team_name}   (as of {today:%d/%m/%Y})\n")
    rows = []
    for person in cfg.people:
        av = plan.availability[person.name]
        if not av.busy or av.free_from <= today:
            when = "free now"
        else:
            on = ", ".join(sorted(a.epic.key for a in av.active))
            when = f"{av.free_from:%d/%m/%y} ({av.wait_days(today)}d) — on {on}"
        rows.append([person.name, person.seniority, when,
                     ", ".join(sorted(person.skills)) or "—"])
    print(table(["PERSON", "LEVEL", "AVAILABLE", "SKILLS"], rows))
    return 0


def cmd_estimate(args) -> int:
    """For a new request, who could take it and when they free up."""
    cfg, epics, today, plan = load_plan(args)
    skills = {s.strip().lower() for s in args.skills.split(",") if s.strip()} if args.skills else set()
    if args.min_seniority and args.min_seniority not in cfg.throughput:
        print(f"✗ seniority {args.min_seniority!r} does not exist. "
              f"Options: {sorted(cfg.throughput)}")
        return 1

    ranked, why_not = availability_for(cfg, plan, skills, args.min_seniority)
    print(f"\nNew request"
          + (f" · skills: {', '.join(sorted(skills))}" if skills else " · no skill requirement")
          + (f" · minimum {args.min_seniority}" if args.min_seniority else ""))
    print(f"Who could take it, earliest first (as of {today:%d/%m/%Y})\n")

    if not ranked:
        print("✗ Nobody on the team matches:")
        for reason in why_not:
            print(f"    {reason}")
        print("\n  → this is hiring, training, or cutting scope. Not a queue.")
        return 1

    rows = []
    for person, free_from in ranked:
        wait = max(0, (free_from - today).days)
        rows.append([person.name, person.seniority,
                     "now" if wait == 0 else f"{free_from:%d/%m/%y}",
                     "0" if wait == 0 else str(wait)])
    print(table(["PERSON", "LEVEL", "AVAILABLE", "WAIT(d)"], rows, right={3}))

    best = ranked[0]
    print(f"\n  → Earliest: {best[0].name} ({best[0].seniority}), "
          f"{'free now' if best[1] <= today else f'free {best[1]:%d/%m/%Y}'}.")
    seniors = [r for r in ranked
               if cfg.seniority_rank(r[0].seniority) >= cfg.seniority_rank("senior")]
    others = [r for r in ranked
              if cfg.seniority_rank(r[0].seniority) < cfg.seniority_rank("senior")]
    if seniors:
        s = seniors[0]
        print(f"    Earliest senior:     {s[0].name}, "
              f"{'now' if s[1] <= today else f'{s[1]:%d/%m/%Y}'}.")
    else:
        print("    Earliest senior:     no senior has these skills.")
    if others:
        o = others[0]
        print(f"    Earliest non-senior: {o[0].name} ({o[0].seniority}), "
              f"{'now' if o[1] <= today else f'{o[1]:%d/%m/%Y}'}.")
    return 0


def cmd_skills(args) -> int:
    cfg, epics = load_all(args)
    demanded = {s for e in epics for s in e.skills}
    rows = []
    for skill in sorted(cfg.all_skills() | demanded):
        who = [p for p in cfg.people if skill in p.skills]
        seniors = [p for p in who if cfg.seniority_rank(p.seniority) >= cfg.seniority_rank("senior")]
        if not who:
            risk = "NO COVERAGE"
        elif len(who) == 1:
            risk = "bus factor 1"
        elif not seniors:
            risk = "no senior"
        else:
            risk = "ok"
        rows.append([skill, str(len(who)), str(len(seniors)),
                     ", ".join(p.name for p in who) or "—", risk])
    print("\nSkill coverage vs. backlog demand\n")
    print(table(["SKILL", "PEOPLE", "SENIORS", "WHO", "RISK"], rows, right={1, 2}))
    return 0


def cmd_source(args) -> int:
    """Shows what the source returns, raw — for debugging mapping and JQL."""
    cfg = load(Path(args.config))
    source = (args.source or cfg.jira.source).lower()
    print(f"\nSource: {sources.describe(cfg, source)}\n")
    raws = sources.fetch_raw(cfg, source)
    rows = []
    for r in raws:
        rows.append([r.key, r.summary[:30], r.status, r.assignee or "—",
                     r.priority or "—", ",".join(r.labels)[:40] or "—", r.due or "—"])
    print(table(["KEY", "SUMMARY", "STATUS", "ASSIGNEE", "PRIOR.", "LABELS", "DUE"], rows))
    epics = normalize_all(cfg, raws)
    print(f"\n  {len(raws)} issues → {len(epics)} open epics after normalisation")
    return 0


# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dpe", description="Date-driven roadmap and availability planner for Jira Epics.")
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG),
                        help="path to the config (default: the project's config/config.toml)")
    parser.add_argument("--date", type=parse_day, default=None,
                        help="treat this as today (default: today)")
    parser.add_argument("--source", choices=sorted(sources.SOURCES), default=None,
                        help="override [jira] source (csv or api)")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--date", type=parse_day, default=argparse.SUPPRESS,
                        help="treat this as today (default: today)")
    common.add_argument("--source", choices=sorted(sources.SOURCES),
                        default=argparse.SUPPRESS, help="override [jira] source")

    sub = parser.add_subparsers(dest="command", required=True,
                                parser_class=lambda **kw: argparse.ArgumentParser(
                                    parents=[common], **kw))

    p = sub.add_parser("clean", help="tidy a raw Jira export into the CSV the tool reads")
    p.add_argument("raw", nargs="?", default=None,
                   help="raw export file or folder (default: [jira] raw_csv)")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("validate", help="check config + source before anything else")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("roadmap", help="timeline of active epics + the waiting queue")
    p.add_argument("--view", choices=sorted(VIEWS), default="person",
                   help="person, epic, or quarter. Default: person")
    p.add_argument("--html", metavar="FILE", help="also write the HTML report")
    p.set_defaults(func=cmd_roadmap)

    p = sub.add_parser("queue", help="waiting epics by priority + when someone frees up")
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("availability", help="who is free, and when")
    p.set_defaults(func=cmd_availability)

    p = sub.add_parser("estimate", help="for a new request: who could take it and when")
    p.add_argument("--skills", default="", help="comma-separated list, e.g. terraform,aws")
    p.add_argument("--min-seniority", dest="min_seniority", default=None,
                   help="require this level or above, e.g. senior")
    p.set_defaults(func=cmd_estimate)

    p = sub.add_parser("skills", help="skill coverage and bus factor")
    p.set_defaults(func=cmd_skills)

    p = sub.add_parser("source", help="show the raw issues the source returns")
    p.set_defaults(func=cmd_source)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, JiraError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
