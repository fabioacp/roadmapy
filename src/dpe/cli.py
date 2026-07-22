"""Command-line interface for the capacity planner."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from . import report, sources
from .config import Config, ConfigError, load
from .jira import Epic, JiraError, normalize_all, unknown_skills
from .scheduler import (COMMITTED, DEFAULT_STRATEGY, STRATEGIES, measure,
                        monthly_utilization, schedule, simulate)
from .sources import csv_source

# Default: the project's own config, so `dpe` works from any directory.
# A relative path passed via -c is still resolved from the
# current directory, which is what you expect when typing it by hand.
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


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def quarter_of(cfg: Config, day: date) -> str:
    """Fiscal quarter — Q1 starts in [team] fiscal_year_start_month (July by default)."""
    return cfg.fiscal_quarter(day)


def _prio_cell(epic) -> str:
    """Flags with * any priority you forced in [priority]."""
    return (epic.priority or "—") + ("*" if epic.priority_forced else "")


def view_person(cfg, assignments, today) -> str:
    """Each person's schedule — who is busy with what."""
    out = []
    for person in cfg.people:
        mine = sorted((a for a in assignments if a.person is person),
                      key=lambda x: (x.start or date.max))
        load_days = sum(a.epic.estimate_days for a in mine)
        out.append(f"\n  {person.name}  ({person.seniority}, {person.fte:.0%} FTE)"
                   f"  —  {len(mine)} epic(s), {load_days:.0f} senior-days")
        if not mine:
            out.append("      (free)")
        for a in mine:
            marker = "▪" if a.committed else "◇"
            late_note = ""
            if a.epic.due and a.end and a.end > a.epic.due:
                late_note = f"  {(a.end - a.epic.due).days}d LATE"
            out.append(f"      {marker} {a.epic.key:<9} {a.epic.summary[:38]:<40} "
                       f"{a.start:%d/%m} → {a.end:%d/%m/%y}  {a.fte:.0%}{late_note}")
    out.append("\n      ▪ declared in [[assignments]]      ◇ tool suggestion")
    return "\n".join(out)


def view_epic(cfg, assignments, today) -> str:
    """The team's delivery sequence — one row per epic, in delivery order."""
    staffed = sorted((a for a in assignments if a.staffed and a.end),
                     key=lambda x: (x.end, x.start))
    rows = []
    for a in staffed:
        late_note = ""
        if a.epic.due:
            late_note = (f"{(a.end - a.epic.due).days}d late" if a.end > a.epic.due
                      else "on time")
        rows.append([
            a.epic.key,
            a.epic.summary[:38],
            _prio_cell(a.epic),
            "fact" if a.committed else "suggestion",
            a.person.name.split()[0],
            f"{a.start:%d/%m/%y}",
            f"{a.end:%d/%m/%y}",
            f"{a.epic.estimate_days:.1f}",
            late_note,
        ])
    return table(["EPIC", "SUMMARY", "PRIOR.", "ORIGIN", "WHO", "START", "DELIVERS",
                  "DAYS", "DEADLINE"], rows, right={7})


def view_quarter(cfg, assignments, today) -> str:
    """Groups by delivery quarter — the view to bring to leadership."""
    staffed = [a for a in assignments if a.staffed and a.end]
    buckets: dict[str, list] = {}
    for a in sorted(staffed, key=lambda x: x.end):
        buckets.setdefault(quarter_of(cfg, a.end), []).append(a)

    out = []
    for q, items in buckets.items():
        effort = sum(a.epic.estimate_days for a in items)
        late_count = sum(1 for a in items if a.epic.due and a.end > a.epic.due)
        alert = f"  ⚠ {late_count} past deadline" if late_count else ""
        out.append(f"\n  {q}  —  {len(items)} epic(s), {effort:.0f} senior-days{alert}")
        for a in items:
            marker = "▪" if a.committed else "◇"
            out.append(f"      {marker} {a.epic.key:<9} {a.epic.summary[:40]:<42} "
                       f"{a.person.name.split()[0]:<8} delivers {a.end:%d/%m}")

    no_date = [a for a in assignments if not a.staffed]
    if no_date:
        out.append(f"\n  NO DATE  —  {len(no_date)} epic(s) nobody can take")
        for a in no_date:
            out.append(f"      ✗ {a.epic.key:<9} {a.epic.summary[:40]}")
    out.append("\n      ▪ declared in [[assignments]]      ◇ tool suggestion")
    return "\n".join(out)


VIEWS = {
    "person": ("each person's schedule", view_person),
    "epic": ("the team's delivery sequence", view_epic),
    "quarter": ("grouped by fiscal quarter", view_quarter),
}


def cmd_validate(args) -> int:
    cfg = load(Path(args.config))
    print(f"✓ config OK — {cfg.team_name}, {len(cfg.people)} people")
    for p in cfg.people:
        print(
            f"    {p.name:<18} {p.seniority:<10} {p.fte:.0%} FTE  "
            f"{cfg.daily_capacity(p):.2f} senior-days/working day  "
            f"skills: {', '.join(sorted(p.skills)) or '—'}"
        )

    source = (args.source or cfg.jira.source).lower()
    print(f"\n  Source: {sources.describe(cfg, source)}")

    if source == "csv":
        path = cfg.resolve(cfg.jira.csv)
        header, rows = csv_source.read_rows(path)
        print(f"✓ CSV read — {len(rows)} rows, {len(set(header))} distinct columns")
        missing = csv_source.missing_columns(cfg, header)
        if missing:
            print("\n✗ [jira.columns] mappings missing from the CSV:")
            for m in missing:
                print(f"    {m}")
            print("\n  Columns available in the CSV:")
            for col in sorted(set(header)):
                print(f"    {col!r}")
            return 1
    else:
        api = cfg.jira.api
        if api.transport == "stub":
            print(f"  ⓘ transport = \"stub\" — nothing goes to the network. "
                  f"Fixture: {api.stub_file}")
        else:
            import os
            token = os.environ.get(api.token_env, "").strip()
            print(f"  transport = \"http\" · {api.base_url} · {api.email}")
            print(f"  {api.token_env}: {'set' if token else '✗ EMPTY'}")

    raws = sources.fetch_raw(cfg, source)
    epics = normalize_all(cfg, raws)
    print(f"✓ {len(raws)} issues received · {len(epics)} open epics "
          f"(done statuses filtered out)")

    missing_estimate = [e.key for e in epics if e.estimate_missing]
    if missing_estimate:
        print(
            f"\n⚠ {len(missing_estimate)} epic(s) with no estimate, assuming "
            f"{cfg.jira.default_estimate_days:g} senior-days: {', '.join(missing_estimate)}"
        )
    missing_skill = [e.key for e in epics if not e.skills]
    if missing_skill:
        print(
            f"\n⚠ {len(missing_skill)} epic(s) with no {cfg.jira.skill_label_prefix}* label — "
            f"anyone can take them: {', '.join(missing_skill)}"
        )
    gaps = unknown_skills(cfg, epics)
    if gaps:
        print("\n⚠ skills required that nobody on the roster has:")
        for skill, keys in sorted(gaps.items()):
            print(f"    {skill:<18} required by {', '.join(keys)}")

    if cfg.priority_overrides:
        by_key = {e.key.upper() for e in epics}
        unknown_keys = [k for k in cfg.priority_overrides if k not in by_key]
        print(f"\n  [priority] — {len(cfg.priority_overrides)} forced priority(ies)")
        for e in epics:
            if e.priority_forced:
                before = e.jira_priority or "(empty)"
                print(f"    {e.key:<10} {before:<10} -> {e.priority}")
        if unknown_keys:
            print(f"\n⚠ {len(unknown_keys)} key(s) in [priority] that are not in the backlog: "
                  f"{', '.join(sorted(unknown_keys))}")

    _check_assignments(cfg, epics)
    return 0


def _check_assignments(cfg: Config, epics: list[Epic]) -> None:
    """Checks [[assignments]] against the backlog and against real capacity."""
    from .scheduler import eligible

    by_key = {e.key.upper(): e for e in epics}
    print(f"\n  [[assignments]] — {len(cfg.commitments)} epic(s) with a declared owner")

    orphans = [c for c in cfg.commitments if c.epic.upper() not in by_key]
    if orphans:
        print(f"\n⚠ {len(orphans)} assignment(s) pointing at an epic not in the backlog "
              f"(already done, or outside the JQL/CSV):")
        for c in orphans:
            print(f"    {c.epic:<10} -> {c.person}  — safe to remove from config.toml")

    for c in cfg.commitments:
        epic = by_key.get(c.epic.upper())
        person = cfg.person(c.person)
        if epic is None or person is None:
            continue
        fits, why = eligible(cfg, person, epic)
        if not fits:
            print(f"\n⚠ {c.epic} is assigned to {c.person}, but {why}.")
            print(f"    Honoured anyway — it is your call. "
                  f"Fix the skills in [[people]] if that data is stale.")

    overloaded = [
        (p.name, cfg.committed_fte(p.name))
        for p in cfg.people
        if cfg.committed_fte(p.name) > 1.0 + 1e-9
    ]
    if overloaded:
        print("\n⚠ people committed above 100% — the work will stretch out over")
        print("  time, not happen in parallel:")
        for name, total in overloaded:
            their_epics = [c.epic for c in cfg.commitments if c.person == name]
            print(f"    {name:<18} {total:.0%}  ({', '.join(their_epics)})")

    # Jira's Assignee schedules nothing — but it is a useful hint of what to declare.
    hints = [
        e for e in epics
        if e.assignee and cfg.commitment_for(e.key) is None
    ]
    if hints:
        print(f"\n  ⓘ {len(hints)} epic(s) have a Jira Assignee but are not in")
        print("    [[assignments]]. Jira schedules nothing here — if it is real, declare it:")
        for e in hints:
            print(f"\n      [[assignments]]")
            print(f"      epic = \"{e.key}\"")
            print(f"      person = \"{e.assignee}\"")
            print(f"      fte = 1.0")


def cmd_source(args) -> int:
    """Shows what the source returns, raw — for debugging mapping and JQL."""
    cfg = load(Path(args.config))
    source = (args.source or cfg.jira.source).lower()
    print(f"\nSource: {sources.describe(cfg, source)}\n")

    raws = sources.fetch_raw(cfg, source)
    rows = []
    for r in raws:
        rows.append([
            r.key,
            r.summary[:34],
            r.status,
            r.assignee or "—",
            r.priority or "—",
            str(r.estimate) if r.estimate is not None else "—",
            ",".join(r.labels) or "—",
            r.due or "—",
        ])
    print(table(
        ["KEY", "SUMMARY", "STATUS", "ASSIGNEE", "PRIOR.", "EST.", "LABELS", "DUE"],
        rows, right={5},
    ))
    epics = normalize_all(cfg, raws)
    print(f"\n  {len(raws)} issues → {len(epics)} open epics after normalisation")
    return 0


def cmd_roadmap(args) -> int:
    cfg, epics = load_all(args)
    today = args.date or date.today()
    assignments, ledgers = schedule(cfg, epics, today, strategy=args.strategy)

    staffed = [a for a in assignments if a.staffed]
    unstaffed = [a for a in assignments if not a.staffed]
    view_label, render_view = VIEWS[args.view]

    print(f"\nRoadmap — {cfg.team_name}   (from {today:%d/%m/%Y})")
    print(f"View: {args.view} ({view_label}) · order: {STRATEGIES[args.strategy]}\n")
    print(render_view(cfg, assignments, today))

    if unstaffed and args.view != "quarter":
        print(f"\n⚠ {len(unstaffed)} epic(s) with nobody eligible:")
        for a in unstaffed:
            print(f"    {a.epic.key:<10} {a.epic.summary[:40]:<42} {a.reason[:60]}")

    if staffed:
        m = measure(assignments, today, args.strategy)
        n_fact = sum(1 for a in staffed if a.committed)
        total = sum(a.epic.estimate_days for a in staffed)
        print(f"\n  {len(staffed)} epics · {total:.0f} senior-days · "
              f"backlog clear on {m.makespan:%d/%m/%Y}")
        print(f"  {n_fact} from [[assignments]] (fact) · {len(staffed) - n_fact} suggested")
        if m.late_epics:
            print(f"  ⚠ {m.late_epics} epic(s) miss their deadline, {m.late_days} days late "
                  f"in total — see `./dpe compare`")

    forced = [e for e in epics if e.priority_forced]
    if forced:
        print(f"\n  * {len(forced)} priority(ies) come from [priority] in the config, "
              f"not from Jira: " + ", ".join(f"{e.key}={e.priority}" for e in forced))

    if args.html:
        out = Path(args.html)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            report.render(cfg, epics, assignments, ledgers, today, args.months),
            encoding="utf-8",
        )
        print(f"\n  HTML: {out.resolve()}")
    return 0


def cmd_compare(args) -> int:
    """Runs the strategies side by side and shows what each choice costs."""
    cfg, epics = load_all(args)
    today = args.date or date.today()

    results = []
    for name in STRATEGIES:
        assignments, _ = schedule(cfg, epics, today, strategy=name)
        results.append((name, assignments, measure(assignments, today, name)))

    print(f"\nStrategy comparison — {cfg.team_name} (from {today:%d/%m/%Y})")
    print("The [[assignments]] work is identical in all three; only the open backlog order changes.\n")

    rows = []
    for name, _, m in results:
        rows.append([
            name,
            STRATEGIES[name][:34],
            f"{m.makespan:%d/%m/%y}" if m.makespan else "—",
            str(m.late_epics),
            str(m.late_days),
            f"{m.avg_wait:.0f}",
            f"{m.first_delivery:%d/%m}" if m.first_delivery else "—",
        ])
    print(table(
        ["STRATEGY", "ORDERS BY", "ENDS", "LATE", "DAYS LATE", "AVG WAIT",
         "1ST DELIVERY"],
        rows, right={3, 4, 5},
    ))

    least_late = min(results, key=lambda r: (r[2].late_days, r[2].late_epics))
    finishes_first = min(results, key=lambda r: (r[2].makespan or date.max))
    shortest_wait = min(results, key=lambda r: r[2].avg_wait)
    print(f"\n  Least delay:    {least_late[0]}  "
          f"({least_late[2].late_epics} epics, {least_late[2].late_days} days)")
    print(f"  Finishes first: {finishes_first[0]}  ({finishes_first[2].makespan:%d/%m/%Y})")
    print(f"  Shortest wait:  {shortest_wait[0]}  ({shortest_wait[2].avg_wait:.0f} days)")

    # If reordering does not move the end date, the bottleneck is capacity or
    # skill — not priority.
    end_dates = {r[2].makespan for r in results}
    if len(end_dates) == 1:
        _, assignments, m = results[0]
        critical = next((a for a in assignments if a.staffed and a.end == m.makespan), None)
        print("\n  ⓘ All three strategies end on the SAME date. Reordering the backlog")
        print("    will not help — the bottleneck is capacity, not order.")
        if critical:
            who = critical.person
            rare_skills = sorted(
                sk for sk in critical.epic.skills
                if sum(1 for q in cfg.people if sk in q.skills) == 1
            )
            print(f"\n    Critical path: {critical.epic.key} ({critical.epic.summary[:40]}),")
            print(f"    which can only go to {who.name} ({who.fte:.0%} FTE).")
            if rare_skills:
                print(f"    Skill with no backup: {', '.join(rare_skills)} — only {who.name} has it.")
            print("\n    What moves the date: raise that person's FTE, train someone in that")
            print("    skill, hire, or cut the epic from scope.")

    if args.detail:
        for name, assignments, _ in results:
            print(f"\n{'─' * 78}\n  {name.upper()} — {STRATEGIES[name]}")
            print(view_epic(cfg, assignments, today))
    else:
        print("\n  Use --detail to see the full order for each strategy.")
        print(f"  To adopt one:  ./dpe roadmap --strategy deadline")
    return 0


def cmd_capacity(args) -> int:
    cfg, epics = load_all(args)
    today = args.date or date.today()
    _, ledgers = schedule(cfg, epics, today)
    labels, util = monthly_utilization(cfg, ledgers, today, args.months)

    print(f"\nMonthly utilisation — % of effective capacity already committed\n")
    rows = []
    for p in cfg.people:
        row = [p.name]
        for label in labels:
            used, cap = util[p.name][label]
            row.append(f"{used / cap * 100:.0f}%" if cap > 1e-9 else "—")
        rows.append(row)
    print(table(["PERSON"] + [l[5:] + "/" + l[2:4] for l in labels], rows,
                right=set(range(1, len(labels) + 1))))

    print("\nFREE senior-days per month\n")
    rows = []
    totals = {label: 0.0 for label in labels}
    for p in cfg.people:
        row = [p.name]
        for label in labels:
            used, cap = util[p.name][label]
            free = max(0.0, cap - used)
            totals[label] += free
            row.append(f"{free:.1f}")
        rows.append(row)
    rows.append(["TOTAL"] + [f"{totals[l]:.1f}" for l in labels])
    print(table(["PERSON"] + [l[5:] + "/" + l[2:4] for l in labels], rows,
                right=set(range(1, len(labels) + 1))))
    return 0


def cmd_estimate(args) -> int:
    cfg, epics = load_all(args)
    today = args.date or date.today()
    _, ledgers = schedule(cfg, epics, today)

    skills = {s.strip().lower() for s in args.skills.split(",") if s.strip()} if args.skills else set()
    if args.min_seniority and args.min_seniority not in cfg.throughput:
        print(f"✗ seniority {args.min_seniority!r} does not exist. Options: {sorted(cfg.throughput)}")
        return 1

    days = args.days
    if args.points is not None:
        days = args.points * cfg.jira.points_to_days

    options, why_not = simulate(cfg, ledgers, skills, days, args.min_seniority, today)

    print(f"\nNew request — {days:.1f} senior-days"
          + (f" · skills: {', '.join(sorted(skills))}" if skills else " · no skill requirement")
          + (f" · minimum {args.min_seniority}" if args.min_seniority else ""))
    print(f"Simulated against the current backlog ({len(epics)} epics), from {today:%d/%m/%Y}\n")

    if not options:
        print("✗ Nobody on the team can take this work:")
        for reason in why_not:
            print(f"    {reason}")
        print("\n  → this is hiring, training, or cutting scope. It is not a queue.")
        return 1

    rows = []
    for opt in options:
        rows.append([
            opt.person.name,
            opt.person.seniority,
            f"{opt.start:%d/%m/%Y}",
            f"{opt.end:%d/%m/%Y}",
            str(opt.slack_days),
            f"{(opt.end - opt.start).days + 1}",
        ])
    print(table(
        ["PERSON", "LEVEL", "CAN START", "DELIVERS", "WAIT(d)", "DURATION(d)"],
        rows,
        right={4, 5},
    ))

    best = options[0]
    print(f"\n  → Earliest delivery: {best.person.name} ({best.person.seniority}), "
          f"starts {best.start:%d/%m/%Y}, delivers {best.end:%d/%m/%Y}.")
    print(f"    {best.slack_days} days of waiting until capacity frees up.")

    seniors = [o for o in options
               if cfg.seniority_rank(o.person.seniority) >= cfg.seniority_rank("senior")]
    others = [o for o in options
              if cfg.seniority_rank(o.person.seniority) < cfg.seniority_rank("senior")]
    if seniors:
        s = seniors[0]
        print(f"    Earliest senior:     {s.person.name}, free on {s.start:%d/%m/%Y}, "
              f"delivers {s.end:%d/%m/%Y}.")
    else:
        print("    Earliest senior:     no senior has these skills.")
    if others:
        o = others[0]
        print(f"    Earliest non-senior: {o.person.name} ({o.person.seniority}), "
              f"free on {o.start:%d/%m/%Y}, delivers {o.end:%d/%m/%Y}.")

    if why_not and args.verbose:
        print("\n  Who was ruled out:")
        for reason in why_not:
            print(f"    {reason}")
    return 0


def cmd_suggest(args) -> int:
    """Epics with nobody working on them: who would take them and when."""
    cfg, epics = load_all(args)
    today = args.date or date.today()
    assignments, _ = schedule(cfg, epics, today)

    open_items = [a for a in assignments if a.kind != COMMITTED]
    if not open_items:
        print("\n✓ Every epic in the backlog is in [[assignments]] — nothing to suggest.")
        return 0

    print(f"\nEpics with nobody working on them — {len(open_items)} of {len(assignments)}")
    print(f"Suggestion based on skill + seniority + first free window, "
          f"from {today:%d/%m/%Y}.\n")

    rows = []
    for a in sorted(open_items, key=lambda x: (x.start or date.max, x.epic.key)):
        if not a.staffed:
            rows.append([a.epic.key, a.epic.summary[:32], "— nobody eligible —",
                         "—", "—", "—", a.reason[:38]])
            continue
        wait_days = (a.start - today).days
        alt = ", ".join(f"{o.person.name.split()[0]} ({o.start:%d/%m})"
                        for o in a.alternatives) or "—"
        rows.append([
            a.epic.key,
            a.epic.summary[:32],
            f"{a.person.name} ({a.person.seniority})",
            f"{wait_days}d",
            f"{a.start:%d/%m/%y}",
            f"{a.end:%d/%m/%y}",
            alt,
        ])
    print(table(
        ["EPIC", "SUMMARY", "SUGGESTION", "WAIT", "STARTS", "DELIVERS", "ALTERNATIVES"],
        rows, right={3},
    ))

    unowned = [a for a in open_items if not a.staffed]
    if unowned:
        print(f"\n⚠ {len(unowned)} epic(s) nobody on the team can take:")
        for a in unowned:
            need = ", ".join(sorted(a.epic.skills)) or "—"
            if a.epic.min_seniority:
                need += f" (min: {a.epic.min_seniority})"
            print(f"    {a.epic.key:<10} needs {need}")
        print("\n  → hiring, training, or cutting scope. It is not a queue.")

    ready_now = [a for a in open_items if a.staffed and (a.start - today).days == 0]
    if ready_now:
        print(f"\n  {len(ready_now)} epic(s) can start TODAY: "
              + ", ".join(f"{a.epic.key} ({a.person.name.split()[0]})" for a in ready_now))

    print("\n  To turn a suggestion into a commitment, paste into config/config.toml:\n")
    example = next((a for a in open_items if a.staffed), None)
    if example:
        print(f"      [[assignments]]")
        print(f"      epic = \"{example.epic.key}\"")
        print(f"      person = \"{example.person.name}\"")
        print(f"      fte = 1.0")
    return 0


def cmd_skills(args) -> int:
    cfg, epics = load_all(args)
    demanded = {s for e in epics for s in e.skills}
    rows = []
    for skill in sorted(cfg.all_skills() | demanded):
        who = [p for p in cfg.people if skill in p.skills]
        seniors = [p for p in who if cfg.seniority_rank(p.seniority) >= cfg.seniority_rank("senior")]
        demand = sum(e.estimate_days for e in epics if skill in e.skills)
        if not who:
            risk = "NO COVERAGE"
        elif len(who) == 1:
            risk = "bus factor 1"
        elif not seniors:
            risk = "no senior"
        else:
            risk = "ok"
        rows.append([skill, str(len(who)), str(len(seniors)), f"{demand:.0f}",
                     ", ".join(p.name for p in who) or "—", risk])
    print("\nSkill coverage vs. backlog demand\n")
    print(table(["SKILL", "PEOPLE", "SENIORS", "DEMAND(d)", "WHO", "RISK"], rows, right={1, 2, 3}))
    return 0


# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dpe",
        description="Capacity and roadmap planner driven by Jira Epics.",
    )
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG),
                        help="path to the config (default: the project's config/config.toml)")
    parser.add_argument("--date", type=parse_day, default=None,
                        help="simulate from this date (default: today)")
    parser.add_argument("--source", choices=sorted(sources.SOURCES), default=None,
                        help="override [jira] source (csv or api)")

    # The same flags on the subcommands, so that both `dpe --date X roadmap` and
    # `dpe roadmap --date X` work. SUPPRESS is what stops the subparser from
    # overwriting the global parser's value with None.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)
    common.add_argument("--date", type=parse_day, default=argparse.SUPPRESS,
                        help="simulate from this date (default: today)")
    common.add_argument("--source", choices=sorted(sources.SOURCES),
                        default=argparse.SUPPRESS,
                        help="override [jira] source (csv or api)")

    sub = parser.add_subparsers(dest="command", required=True, parser_class=lambda **kw: argparse.ArgumentParser(parents=[common], **kw))

    p = sub.add_parser("validate", help="check config + source before anything else")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("roadmap", help="allocate the epics and show the roadmap")
    p.add_argument("--view", choices=sorted(VIEWS), default="person",
                   help="person (each schedule), epic (delivery sequence) "
                        "or quarter (by fiscal quarter). Default: person")
    p.add_argument("--strategy", choices=sorted(STRATEGIES), default=DEFAULT_STRATEGY,
                   help=f"order of the open backlog (default: {DEFAULT_STRATEGY})")
    p.add_argument("--html", metavar="ARQUIVO", help="also write the HTML report")
    p.add_argument("--months", type=int, default=9, help="months in the HTML heatmap (default: 9)")
    p.set_defaults(func=cmd_roadmap)

    p = sub.add_parser("compare", help="compare ordering strategies side by side")
    p.add_argument("--detail", action="store_true", help="show the full order for each one")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("capacity", help="utilisation and free days per person/month")
    p.add_argument("--months", type=int, default=9, help="how many months (default: 9)")
    p.set_defaults(func=cmd_capacity)

    p = sub.add_parser("estimate", help="when the team can take on a new request")
    p.add_argument("--skills", default="", help="comma-separated list, e.g. terraform,aws")
    p.add_argument("--days", type=float, default=10.0, help="size in senior-days (default: 10)")
    p.add_argument("--points", type=float, default=None,
                   help="size in story points (converted via points_to_days)")
    p.add_argument("--min-seniority", dest="min_seniority", default=None,
                   help="require this level or above, e.g. senior")
    p.add_argument("-v", "--verbose", action="store_true", help="show who was ruled out and why")
    p.set_defaults(func=cmd_estimate)

    p = sub.add_parser("skills", help="skill coverage and bus factor")
    p.set_defaults(func=cmd_skills)

    p = sub.add_parser("suggest", help="epics with no owner: who would take them and when")
    p.set_defaults(func=cmd_suggest)

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
