"""Generates the HTML report: a date-driven roadmap and the waiting queue.

No dependencies — the HTML is assembled by concatenation and is self-contained
(opens with a double click, works offline, respects the system light/dark theme).
"""

from __future__ import annotations

import html
from datetime import date, timedelta

from .config import Config
from .jira import Epic, unknown_skills
from .scheduler import ActiveWork, Plan

# Validated categorical palette (dataviz): fixed order, never recycled.
SERIES_LIGHT = ["#2a78d6", "#008300", "#e87ba4", "#eda100", "#1baf7a", "#eb6834", "#4a3aa7", "#e34948"]
SERIES_DARK = ["#3987e5", "#008300", "#d55181", "#c98500", "#199e70", "#d95926", "#9085e9", "#e66767"]

E = html.escape


def _css(n_series: int) -> str:
    def series_vars(colors: list[str]) -> str:
        return "".join(f"    --series-{i + 1}: {c};\n" for i, c in enumerate(colors[:n_series]))

    dark_block = f"""
    color-scheme: dark;
    --surface-1: #1a1a19;
    --plane: #0d0d0d;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --muted: #898781;
    --grid: #2c2c2a;
    --baseline: #383835;
    --hairline: rgba(255,255,255,0.10);
{series_vars(SERIES_DARK)}"""

    return f""":root {{
    color-scheme: light;
    --surface-1: #fcfcfb;
    --plane: #f9f9f7;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --muted: #898781;
    --grid: #e1e0d9;
    --baseline: #c3c2b7;
    --hairline: rgba(11,11,11,0.10);
    --good: #0ca30c;
    --warning: #fab219;
    --critical: #d03b3b;
{series_vars(SERIES_LIGHT)}}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) {{{dark_block}  }}
}}
:root[data-theme="dark"] {{{dark_block}}}

* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 32px 24px 80px;
  background: var(--plane); color: var(--text-primary);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}}
.wrap {{ max-width: 1240px; margin: 0 auto; }}
h1 {{ font-size: 24px; margin: 0 0 4px; letter-spacing: -0.01em; }}
h2 {{ font-size: 16px; margin: 0 0 6px; letter-spacing: -0.005em; }}
.sub {{ color: var(--text-secondary); margin: 0 0 28px; }}
section {{ background: var(--surface-1); border: 1px solid var(--hairline);
  border-radius: 12px; padding: 20px; margin-bottom: 20px; }}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 20px; }}
.tile {{ background: var(--surface-1); border: 1px solid var(--hairline); border-radius: 12px; padding: 16px 18px; }}
.tile .k {{ font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }}
.tile .v {{ font-size: 28px; font-weight: 600; letter-spacing: -0.02em; }}
.tile .n {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}

/* --- Gantt --------------------------------------------------------------- */
.gantt {{ overflow-x: auto; }}
.gantt-inner {{ min-width: 900px; }}
.grow {{ display: grid; grid-template-columns: 200px 1fr; align-items: stretch; }}
.gname {{ padding: 8px 12px 8px 0; border-right: 1px solid var(--grid);
  display: flex; flex-direction: column; justify-content: center; }}
.gname b {{ font-weight: 600; }}
.gname span {{ font-size: 11px; color: var(--muted); }}
.gtrack {{ position: relative; padding: 6px 0; }}
.gline {{ position: absolute; top: 0; bottom: 0; width: 1px; background: var(--grid); }}
.lane {{ position: relative; height: 26px; margin-bottom: 4px; }}
.bar {{ position: absolute; top: 2px; height: 22px; border-radius: 4px;
  box-shadow: 0 0 0 2px var(--surface-1); display: flex; align-items: center;
  padding: 0 7px; overflow: hidden; font-size: 11px; font-weight: 600; color: #fff;
  white-space: nowrap; cursor: default; }}
.bar.ink-dark {{ color: #0b0b0b; }}
/* Queue wait: hatched, thinner, dashed — the span from today until start. */
.bar.wait {{ height: 16px; top: 5px; opacity: .9;
  background-image: repeating-linear-gradient(135deg,
    rgba(255,255,255,.35) 0 5px, rgba(255,255,255,0) 5px 10px);
  outline: 1px dashed var(--surface-1); outline-offset: -3px; }}
.bar.blocked {{ background: var(--critical) !important; }}
.axis {{ display: grid; grid-template-columns: 200px 1fr; margin-top: 4px; }}
.axis-months {{ position: relative; height: 20px; border-top: 1px solid var(--baseline); }}
.axis-months span {{ position: absolute; top: 3px; font-size: 11px; color: var(--muted);
  font-variant-numeric: tabular-nums; }}
.today {{ position: absolute; top: 0; bottom: 0; width: 2px; background: var(--critical); opacity: .8; }}
.today-tag {{ position: absolute; top: -18px; transform: translateX(-50%); font-size: 10px; color: var(--critical); font-weight: 600; }}

/* --- Quarter cards -------------------------------------------------------- */
.quarters {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 14px; }}
.qcard {{ border: 1px solid var(--hairline); border-radius: 10px; padding: 14px; }}
.qhead {{ display: flex; flex-direction: column; margin-bottom: 10px; padding-bottom: 8px; border-bottom: 1px solid var(--grid); }}
.qhead b {{ font-size: 15px; letter-spacing: -0.01em; }}
.qhead span {{ font-size: 11px; color: var(--muted); }}
.qitem {{ display: flex; align-items: baseline; gap: 6px; font-size: 12px; padding: 4px 0; cursor: default; }}
.qkey {{ font-weight: 600; flex: none; }}
.qsum {{ color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.qwhen {{ margin-left: auto; color: var(--muted); flex: none; font-variant-numeric: tabular-nums; }}

/* --- Legend --------------------------------------------------------------- */
.legend {{ display: flex; flex-wrap: wrap; gap: 14px; margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--grid); }}
.legend div {{ display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-secondary); }}
.swatch {{ width: 11px; height: 11px; border-radius: 3px; flex: none; }}

/* --- Tables --------------------------------------------------------------- */
.tw {{ overflow-x: auto; }}
.tw table {{ min-width: 720px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th {{ text-align: left; font-weight: 600; color: var(--text-secondary); font-size: 12px; padding: 6px 10px; border-bottom: 1px solid var(--baseline); }}
td {{ padding: 7px 10px; border-bottom: 1px solid var(--grid); }}
td.num {{ font-variant-numeric: tabular-nums; text-align: right; }}
tr:last-child td {{ border-bottom: none; }}
.dot {{ display: inline-block; width: 9px; height: 9px; border-radius: 3px; margin-right: 7px; vertical-align: -1px; }}
.pill {{ display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11px; border: 1px solid var(--hairline); color: var(--text-secondary); }}
.warn {{ color: var(--warning); font-weight: 600; }}
.crit {{ color: var(--critical); font-weight: 600; }}
.ok {{ color: var(--good); font-weight: 600; }}

/* --- Tooltip ------------------------------------------------------------- */
#tip {{ position: fixed; z-index: 50; pointer-events: none; opacity: 0; transition: opacity .1s;
  background: var(--surface-1); color: var(--text-primary); border: 1px solid var(--hairline);
  border-radius: 8px; padding: 8px 10px; font-size: 12px; line-height: 1.45;
  box-shadow: 0 6px 20px rgba(0,0,0,.18); max-width: 300px; }}
#tip b {{ display: block; margin-bottom: 3px; }}
"""


def _js() -> str:
    return """
const tip = document.getElementById('tip');
document.addEventListener('mouseover', e => {
  const el = e.target.closest('[data-tip]'); if (!el) return;
  tip.innerHTML = el.dataset.tip; tip.style.opacity = 1;
});
document.addEventListener('mousemove', e => {
  if (tip.style.opacity !== '1') return;
  const pad = 14, r = tip.getBoundingClientRect();
  let x = e.clientX + pad, y = e.clientY + pad;
  if (x + r.width > innerWidth - 8) x = e.clientX - r.width - pad;
  if (y + r.height > innerHeight - 8) y = e.clientY - r.height - pad;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
});
document.addEventListener('mouseout', e => {
  if (e.target.closest('[data-tip]')) tip.style.opacity = 0;
});
"""


def _lanes(items: list[ActiveWork]) -> list[list[ActiveWork]]:
    """Stacks overlapping epics into sub-lanes so that none blocks another."""
    lanes: list[list[ActiveWork]] = []
    for item in sorted(items, key=lambda a: (a.start, a.end)):
        for lane in lanes:
            if lane[-1].end < item.start:
                lane.append(item)
                break
        else:
            lanes.append([item])
    return lanes


def _month_starts(start: date, end: date) -> list[date]:
    out, cursor = [], date(start.year, start.month, 1)
    while cursor <= end:
        out.append(cursor)
        cursor = date(cursor.year + (cursor.month == 12), (cursor.month % 12) + 1, 1)
    return out


DARK_INK_SLOTS = {3, 4, 5}  # light slots below 3:1 — carry a direct label
QUEUE_NOMINAL_DAYS = 28     # indicative width for a queued bar (no real duration)


def render(cfg: Config, epics: list[Epic], plan: Plan, today: date) -> str:
    color_of = {p.name: f"var(--series-{(i % 8) + 1})" for i, p in enumerate(cfg.people)}
    slot_of = {p.name: (i % 8) + 1 for i, p in enumerate(cfg.people)}
    active = plan.active
    queue = plan.queue

    ends = ([aw.end for aw in active]
            + [q.available_on + timedelta(days=QUEUE_NOMINAL_DAYS)
               for q in queue if q.available_on])
    starts = [aw.start for aw in active]
    span_start = date(min([today] + starts).year, min([today] + starts).month, 1)
    span_end = max([today + timedelta(days=30)] + ends)
    total_days = max(1, (span_end - span_start).days + 1)

    def pct(day: date) -> float:
        return (day - span_start).days / total_days * 100

    parts: list[str] = []
    add = parts.append

    free_now = sum(1 for p in cfg.people if plan.availability[p.name].free_from <= today)
    blocked = [q for q in queue if q.person is None]

    add(f"<h1>{E(cfg.team_name)} — Roadmap &amp; Availability</h1>")
    add(f'<p class="sub">As of {today:%d/%m/%Y} · {len(cfg.people)} people · '
        f'periods come from start:/end: labels in Jira</p>')

    add('<div class="tiles">')
    add(f'<div class="tile"><div class="k">Active epics</div>'
        f'<div class="v">{len(plan.scheduled_epics)}</div>'
        f'<div class="n">planned period on the timeline</div></div>')
    add(f'<div class="tile"><div class="k">In the queue</div><div class="v">{len(queue)}</div>'
        f'<div class="n">waiting, by priority</div></div>')
    add(f'<div class="tile"><div class="k">Free now</div><div class="v">{free_now}</div>'
        f'<div class="n">of {len(cfg.people)} people</div></div>')
    tone = "crit" if blocked else "ok"
    add(f'<div class="tile"><div class="k">Blocked</div><div class="v {tone}">{len(blocked)}</div>'
        f'<div class="n">{"skill gap on the team" if blocked else "every queued epic has a taker"}'
        f'</div></div>')
    add("</div>")

    month_starts = _month_starts(span_start, span_end)
    grid = "".join(f'<div class="gline" style="left:{pct(m):.3f}%"></div>' for m in month_starts)

    def bar(aw: ActiveWork, person) -> str:
        left, width = pct(aw.start), max(0.6, (aw.end - aw.start).days + 1) / total_days * 100
        ink = " ink-dark" if slot_of[person.name] in DARK_INK_SLOTS else ""
        shared = ""
        if aw.co_owners:
            shared = "<br>shared with " + E(", ".join(aw.co_owners))
        due = ""
        if aw.epic.due:
            late = aw.end > aw.epic.due
            due = f"<br>Due {aw.epic.due:%d/%m/%Y} — {'LATE' if late else 'on time'}"
        warn = f"<br>⚠ {E(aw.warning)}" if aw.warning else ""
        tip = (f"<b>{E(aw.epic.key)}</b>{E(aw.epic.summary)}<br>{E(person.name)} "
               f"({E(person.seniority)}){shared}<br>{aw.start:%d/%m/%Y} → {aw.end:%d/%m/%Y}{due}{warn}")
        mark = '<span class="warn-mark">⚠</span>' if aw.warning else ""
        return (f'<div class="bar{ink}" style="left:{left:.3f}%;width:{width:.3f}%;'
                f'background-color:{color_of[person.name]}" data-tip="{E(tip)}">'
                f'{E(aw.epic.key)}{mark}</div>')

    # ---- Gantt: active work per person, plus queued epics from availability ----
    add('<section><h2>Roadmap by person</h2>')
    add('<p class="sub" style="margin-bottom:14px">Solid bars are active epics '
        '(their start:/end: period). Hatched bars are queued epics, plotted from '
        'the date this person frees up to take them — the earliest they could start '
        '(end still to be planned).</p>')
    add(f'<div class="gantt"><div class="gantt-inner">')
    for person in cfg.people:
        mine = [aw for aw in active if aw.person is person]
        waits = sorted((q for q in queue if q.person is person and q.available_on),
                       key=lambda x: x.available_on)
        lanes = _lanes(mine)
        height = (max(1, len(lanes)) + len(waits)) * 30 + 12
        av = plan.availability[person.name]
        free = "free now" if av.free_from <= today else f"free {av.free_from:%d/%m/%y}"
        add('<div class="grow">')
        add(f'<div class="gname" style="min-height:{height}px"><b>{E(person.name)}</b>'
            f'<span>{E(person.seniority)} · {free}</span></div>')
        add('<div class="gtrack">')
        add(grid)
        add(f'<div class="today" style="left:{pct(today):.3f}%"></div>')
        if not lanes and not waits:
            add('<div class="lane"></div>')
        for lane in lanes:
            add('<div class="lane">')
            for aw in lane:
                add(bar(aw, person))
            add("</div>")
        # Each queued epic on its own sub-lane, starting when the person frees up.
        for q in waits:
            add('<div class="lane">')
            left = pct(q.available_on)
            width = max(1.5, QUEUE_NOMINAL_DAYS / total_days * 100)
            wait = q.wait_days(today)
            when = "now" if wait == 0 else f"{q.available_on:%d/%m/%Y} ({wait}d wait)"
            tip = (f"<b>{E(q.epic.key)}</b>{E(q.epic.summary)}<br>"
                   f"queued · {E(q.epic.priority)} priority<br>"
                   f"earliest start {when} — when {E(person.name)} frees up<br>"
                   f"end still to be planned")
            add(f'<div class="bar wait" style="left:{left:.3f}%;width:{width:.3f}%;'
                f'background-color:{color_of[person.name]}" data-tip="{E(tip)}">'
                f'{E(q.epic.key)} ▸</div>')
            add("</div>")
        add("</div></div>")

    add('<div class="axis"><div></div><div class="axis-months">')
    add(f'<div class="today" style="left:{pct(today):.3f}%"><div class="today-tag">today</div></div>')
    for ms in month_starts:
        add(f'<span style="left:{pct(ms):.3f}%">&nbsp;{ms:%b/%y}</span>')
    add("</div></div>")
    add('<div class="legend">')
    for person in cfg.people:
        add(f'<div><span class="swatch" style="background:{color_of[person.name]}"></span>'
            f'{E(person.name)}</div>')
    add('<div style="margin-left:auto"><span class="swatch" style="background:var(--muted)">'
        '</span>solid = active</div>')
    add('<div><span class="swatch" style="background-color:var(--muted);'
        'background-image:repeating-linear-gradient(135deg,rgba(255,255,255,.5) 0 3px,'
        'rgba(255,255,255,0) 3px 6px)"></span>hatched = queued (earliest start)</div>')
    add("</div></div></div></section>")

    # ---- By quarter (active epics) ----
    seen: dict[str, ActiveWork] = {}
    for aw in active:
        seen.setdefault(aw.epic.key, aw)
    buckets: dict[str, list[ActiveWork]] = {}
    for aw in sorted(seen.values(), key=lambda x: x.end):
        buckets.setdefault(cfg.fiscal_quarter(aw.end), []).append(aw)
    if buckets:
        add('<section><h2>By quarter</h2>')
        add('<p class="sub" style="margin-bottom:16px">Active epics grouped by the '
            'quarter they finish — the view for a planning conversation.</p>')
        add('<div class="quarters">')
        for q, items in sorted(buckets.items()):
            add(f'<div class="qcard"><div class="qhead"><b>{E(q)}</b>'
                f'<span>{len(items)} epic(s)</span></div>')
            for aw in items:
                owners = ", ".join(o.person for o in aw.epic.owners) or "unassigned"
                tip = f"<b>{E(aw.epic.key)}</b>{E(aw.epic.summary)}<br>{E(owners)} · ends {aw.end:%d/%m/%Y}"
                add(f'<div class="qitem" data-tip="{E(tip)}">'
                    f'<span class="qkey">▪ {E(aw.epic.key)}</span>'
                    f'<span class="qsum">{E(aw.epic.summary[:44])}</span>'
                    f'<span class="qwhen">{aw.end:%d/%m}</span></div>')
            add("</div>")
        add("</div></section>")

    # ---- Queue table ----
    add('<section><h2>Queue — waiting by priority</h2>')
    add('<p class="sub" style="margin-bottom:14px">For each waiting epic, the first '
        'person with the right skills to free up, and how long the wait is.</p>')
    add('<div class="tw"><table><thead><tr><th>Epic</th><th>Summary</th><th>Priority</th>'
        "<th>Next free</th><th>Available</th><th class='num'>Wait</th><th>Skills</th>"
        "</tr></thead><tbody>")
    for q in queue:
        prio = E(q.epic.priority or "—") + ("*" if q.epic.priority_forced else "")
        skills = " ".join(f'<span class="pill">{E(s)}</span>' for s in sorted(q.epic.skills)) or "—"
        if q.person is None:
            add(f"<tr><td>{E(q.epic.key)}</td><td>{E(q.epic.summary)}</td><td>{prio}</td>"
                f'<td class="crit">nobody eligible</td><td>—</td><td class="num">—</td>'
                f"<td>{skills}</td></tr>")
            continue
        wait = q.wait_days(today)
        tone = "ok" if wait == 0 else ("warn" if wait <= 30 else "crit")
        label = "now" if wait == 0 else f"{wait}d"
        add(f"<tr><td>{E(q.epic.key)}</td><td>{E(q.epic.summary)}</td><td>{prio}</td>"
            f'<td><span class="dot" style="background:{color_of[q.person.name]}"></span>'
            f'{E(q.person.name)} <span class="pill">{E(q.person.seniority)}</span></td>'
            f"<td>{q.available_on:%d/%m/%Y}</td>"
            f'<td class="num"><span class="{tone}">{label}</span></td><td>{skills}</td></tr>')
    add("</tbody></table></div></section>")

    # ---- Availability ----
    add('<section><h2>Availability</h2>')
    add('<div class="tw"><table><thead><tr><th>Person</th><th>Level</th><th>Available</th>'
        "<th>On now</th><th>Skills</th></tr></thead><tbody>")
    for person in cfg.people:
        av = plan.availability[person.name]
        if av.free_from <= today:
            when = '<span class="ok">free now</span>'
        else:
            when = f'{av.free_from:%d/%m/%Y} <span class="pill">{av.wait_days(today)}d</span>'
        on = ", ".join(sorted(a.epic.key for a in av.active)) or "—"
        skills = " ".join(f'<span class="pill">{E(s)}</span>' for s in sorted(person.skills)) or "—"
        add(f"<tr><td>{E(person.name)}</td><td>{E(person.seniority)}</td><td>{when}</td>"
            f"<td style='color:var(--text-secondary)'>{E(on)}</td><td>{skills}</td></tr>")
    add("</tbody></table></div></section>")

    # ---- Skill coverage ----
    add('<section><h2>Skill coverage</h2>')
    add('<div class="tw"><table><thead><tr><th>Skill</th><th class="num">People</th><th>Who</th>'
        "<th>Risk</th></tr></thead><tbody>")
    demanded = {s for e in epics for s in e.skills}
    for skill in sorted(cfg.all_skills() | demanded):
        who = [p for p in cfg.people if skill in p.skills]
        seniors = [p for p in who if cfg.seniority_rank(p.seniority) >= cfg.seniority_rank("senior")]
        if not who:
            risk = '<span class="crit">■ no coverage</span>'
        elif len(who) == 1:
            risk = '<span class="crit">■ bus factor 1</span>'
        elif not seniors:
            risk = '<span class="warn">■ no senior</span>'
        else:
            risk = '<span class="ok">■ ok</span>'
        names = ", ".join(f"{p.name} ({p.seniority})" for p in who) or "—"
        add(f"<tr><td>{E(skill)}</td><td class='num'>{len(who)}</td>"
            f"<td style='color:var(--text-secondary)'>{E(names)}</td><td>{risk}</td></tr>")
    add("</tbody></table></div>")
    gaps = unknown_skills(cfg, epics)
    if gaps:
        add('<p class="sub" style="margin:14px 0 0">Skills requested by epics that nobody has: '
            + ", ".join(f"<b>{E(s)}</b> ({', '.join(k)})" for s, k in sorted(gaps.items())) + "</p>")
    add("</section>")

    body = "\n".join(parts)
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{E(cfg.team_name)} — Roadmap &amp; Availability</title>"
        f"<style>{_css(len(cfg.people))}</style></head><body>"
        f'<div class="wrap">{body}</div><div id="tip"></div>'
        f"<script>{_js()}</script></body></html>"
    )
