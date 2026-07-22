"""Generates the HTML report: Gantt roadmap, monthly utilisation and skill coverage.

No dependencies — the HTML is assembled by concatenation and is self-contained (opens
with a double click, works offline, respects the system light/dark theme).
"""

from __future__ import annotations

import html
from datetime import date, timedelta

from .config import Config
from .jira import Epic, unknown_skills
from .scheduler import COMMITTED, Assignment, Ledger, by_epic, monthly_utilization

# Validated categorical palette (dataviz): fixed order, never recycled.
SERIES_LIGHT = ["#2a78d6", "#008300", "#e87ba4", "#eda100", "#1baf7a", "#eb6834", "#4a3aa7", "#e34948"]
SERIES_DARK = ["#3987e5", "#008300", "#d55181", "#c98500", "#199e70", "#d95926", "#9085e9", "#e66767"]

# Sequential blue ramp for the utilisation heatmap (low -> high).
HEAT_LIGHT = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95"]
HEAT_DARK = ["#0d366b", "#104281", "#1c5cab", "#2a78d6", "#5598e7", "#86b6ef"]
HEAT_INK_LIGHT = ["#0b0b0b", "#0b0b0b", "#0b0b0b", "#ffffff", "#ffffff", "#ffffff"]
HEAT_INK_DARK = ["#c3c2b7", "#c3c2b7", "#ffffff", "#ffffff", "#0b0b0b", "#0b0b0b"]

E = html.escape


def _heat_bucket(pct: float) -> int:
    for idx, ceiling in enumerate((0.001, 25, 50, 75, 90)):
        if pct <= ceiling:
            return idx
    return 5


def _css(n_series: int) -> str:
    def series_vars(colors: list[str]) -> str:
        return "".join(f"    --series-{i + 1}: {c};\n" for i, c in enumerate(colors[:n_series]))

    def heat_vars(fills: list[str], inks: list[str]) -> str:
        return "".join(
            f"    --heat-{i}: {f}; --heat-ink-{i}: {inks[i]};\n" for i, f in enumerate(fills)
        )

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
{series_vars(SERIES_DARK)}{heat_vars(HEAT_DARK, HEAT_INK_DARK)}"""

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
{series_vars(SERIES_LIGHT)}{heat_vars(HEAT_LIGHT, HEAT_INK_LIGHT)}}}
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
h2 {{ font-size: 16px; margin: 0 0 16px; letter-spacing: -0.005em; }}
.sub {{ color: var(--text-secondary); margin: 0 0 28px; }}
section {{
  background: var(--surface-1); border: 1px solid var(--hairline);
  border-radius: 12px; padding: 20px; margin-bottom: 20px;
}}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 20px; }}
.tile {{ background: var(--surface-1); border: 1px solid var(--hairline); border-radius: 12px; padding: 16px 18px; }}
.tile .k {{ font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }}
.tile .v {{ font-size: 28px; font-weight: 600; letter-spacing: -0.02em; }}
.tile .n {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}

/* --- Gantt --------------------------------------------------------------- */
.gantt {{ overflow-x: auto; }}
.gantt-inner {{ min-width: 900px; }}
.grow {{ display: grid; grid-template-columns: 190px 1fr; align-items: stretch; }}
.gname {{
  padding: 8px 12px 8px 0; border-right: 1px solid var(--grid);
  display: flex; flex-direction: column; justify-content: center;
}}
.gname b {{ font-weight: 600; }}
.gname span {{ font-size: 11px; color: var(--muted); }}
.gtrack {{ position: relative; padding: 6px 0; }}
.gline {{ position: absolute; top: 0; bottom: 0; width: 1px; background: var(--grid); }}
.lane {{ position: relative; height: 26px; margin-bottom: 4px; }}
.bar {{
  position: absolute; top: 2px; height: 22px; border-radius: 4px;
  box-shadow: 0 0 0 2px var(--surface-1);
  display: flex; align-items: center; padding: 0 7px; overflow: hidden;
  font-size: 11px; font-weight: 600; color: #fff; white-space: nowrap; cursor: default;
}}
.bar.ink-dark {{ color: #0b0b0b; }}
/* Suggestion: hatching + dashed outline. The texture is the redundant channel —
   the ORIGIN column and the legend say the same thing without relying on colour. */
.bar.suggested {{
  background-image: repeating-linear-gradient(135deg,
    rgba(255,255,255,.34) 0 5px, rgba(255,255,255,0) 5px 10px);
  outline: 1px dashed var(--surface-1); outline-offset: -3px;
}}
.bar .warn-mark {{ margin-left: 5px; }}
.axis {{ display: grid; grid-template-columns: 190px 1fr; margin-top: 4px; }}
.axis-months {{ position: relative; height: 20px; border-top: 1px solid var(--baseline); }}
.axis-months span {{
  position: absolute; top: 3px; font-size: 11px; color: var(--muted);
  font-variant-numeric: tabular-nums;
}}
.today {{ position: absolute; top: 0; bottom: 0; width: 2px; background: var(--critical); opacity: 0.8; }}
.today-tag {{ position: absolute; top: -18px; transform: translateX(-50%); font-size: 10px; color: var(--critical); font-weight: 600; }}

/* --- Quarter cards -------------------------------------------------------- */
.quarters {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 14px; }}
.qcard {{ border: 1px solid var(--hairline); border-radius: 10px; padding: 14px; }}
.qcard.nodate {{ border-style: dashed; }}
.qhead {{ display: flex; flex-direction: column; margin-bottom: 10px;
  padding-bottom: 8px; border-bottom: 1px solid var(--grid); }}
.qhead b {{ font-size: 15px; letter-spacing: -0.01em; }}
.qhead span {{ font-size: 11px; color: var(--muted); }}
.qitem {{ display: flex; align-items: baseline; gap: 6px; font-size: 12px;
  padding: 4px 0; cursor: default; }}
.qkey {{ font-weight: 600; flex: none; }}
.qsum {{ color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }}
.qwhen {{ margin-left: auto; color: var(--muted); flex: none;
  font-variant-numeric: tabular-nums; }}

/* --- Legend --------------------------------------------------------------- */
.legend {{ display: flex; flex-wrap: wrap; gap: 14px; margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--grid); }}
.legend div {{ display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-secondary); }}
.swatch {{ width: 11px; height: 11px; border-radius: 3px; flex: none; }}

/* --- Tables --------------------------------------------------------------- */
/* The table scrolls inside its own container — the body never scrolls sideways. */
.tw {{ overflow-x: auto; }}
.tw table {{ min-width: 820px; }}
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

/* --- Heatmap ------------------------------------------------------------- */
.heat {{ overflow-x: auto; }}
.heat table {{ border-collapse: separate; border-spacing: 2px; min-width: 700px; }}
.heat td.cell {{
  border: none; border-radius: 4px; text-align: center; font-size: 11px;
  font-weight: 600; font-variant-numeric: tabular-nums; padding: 9px 6px; cursor: default;
}}
.heat th {{ border-bottom: none; font-variant-numeric: tabular-nums; }}

/* --- Tooltip ------------------------------------------------------------- */
#tip {{
  position: fixed; z-index: 50; pointer-events: none; opacity: 0;
  transition: opacity .1s; background: var(--surface-1); color: var(--text-primary);
  border: 1px solid var(--hairline); border-radius: 8px; padding: 8px 10px;
  font-size: 12px; line-height: 1.45; box-shadow: 0 6px 20px rgba(0,0,0,.18); max-width: 300px;
}}
#tip b {{ display: block; margin-bottom: 3px; }}
details summary {{ cursor: pointer; color: var(--text-secondary); font-size: 13px; margin-bottom: 12px; }}
"""


def _js() -> str:
    return """
const tip = document.getElementById('tip');
document.addEventListener('mouseover', e => {
  const el = e.target.closest('[data-tip]');
  if (!el) return;
  tip.innerHTML = el.dataset.tip;
  tip.style.opacity = 1;
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


def _lanes(items: list[Assignment]) -> list[list[Assignment]]:
    """Stacks overlapping epics into sub-lanes so that none blocks another."""
    lanes: list[list[Assignment]] = []
    for item in sorted(items, key=lambda a: (a.start, a.end)):
        for lane in lanes:
            if lane[-1].end < item.start:
                lane.append(item)
                break
        else:
            lanes.append([item])
    return lanes


def _month_starts(start: date, end: date) -> list[date]:
    out = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        out.append(cursor)
        cursor = date(cursor.year + (cursor.month == 12), (cursor.month % 12) + 1, 1)
    return out


def swatch_for(color_of: dict, person) -> str:
    return f'<span class="dot" style="background:{color_of[person.name]}"></span>'


def render(
    cfg: Config,
    epics: list[Epic],
    assignments: list[Assignment],
    ledgers: dict[str, Ledger],
    today: date,
    months: int,
) -> str:
    staffed = [a for a in assignments if a.staffed]          # per-owner rows (Gantt)
    plans = by_epic(assignments)                             # one per epic (counts/tables)
    staffed_plans = [p for p in plans if p.staffed]
    unstaffed = [p for p in plans if not p.staffed]
    color_of = {p.name: f"var(--series-{(i % 8) + 1})" for i, p in enumerate(cfg.people)}
    # Slots 3,4,5 (light) fall below 3:1 — that is why every bar carries a direct
    # label and the full table exists below (dataviz relief rule).
    dark_ink_slots = {3, 4, 5}

    span_start = min([today] + [a.start for a in staffed])
    span_end = max([today + timedelta(days=30)] + [a.end for a in staffed])
    span_start = date(span_start.year, span_start.month, 1)
    total_days = max(1, (span_end - span_start).days + 1)

    def pct(day: date) -> float:
        return (day - span_start).days / total_days * 100

    parts: list[str] = []
    add = parts.append

    committed = sum(p.epic.estimate_days for p in staffed_plans)
    no_estimate = [p for p in plans if p.epic.estimate_missing]
    last_end = max((p.end for p in staffed_plans), default=today)
    n_owned = sum(1 for e in epics if e.owned)

    add(f"<h1>{E(cfg.team_name)} — Roadmap &amp; Capacity</h1>")
    add(
        f'<p class="sub">Generated {today:%d/%m/%Y} · {len(cfg.people)} people · '
        f'{cfg.overhead_pct:.0%} overhead · {n_owned} epic(s) with an owner: label in Jira</p>'
    )

    add('<div class="tiles">')
    n_fact = sum(1 for p in staffed_plans if p.committed)
    n_suggested = len(staffed_plans) - n_fact
    add(f'<div class="tile"><div class="k">Declared owner</div><div class="v">{n_fact}</div>'
        f'<div class="n">owner: label — fact</div></div>')
    add(f'<div class="tile"><div class="k">Suggested by the tool</div>'
        f'<div class="v">{n_suggested}</div>'
        f'<div class="n">of {len(epics)} epics in the backlog</div></div>')
    add(f'<div class="tile"><div class="k">Committed effort</div>'
        f'<div class="v">{committed:.0f}</div>'
        f'<div class="n">senior-days</div></div>')
    add(f'<div class="tile"><div class="k">Current backlog ends</div>'
        f'<div class="v">{last_end:%b/%y}</div>'
        f'<div class="n">last epic finishes {last_end:%d/%m/%Y}</div></div>')
    tone = "crit" if unstaffed else "ok"
    add(f'<div class="tile"><div class="k">Unstaffed</div>'
        f'<div class="v {tone}">{len(unstaffed)}</div>'
        f'<div class="n">{"skill gap on the team" if unstaffed else "every epic has an owner"}'
        f'</div></div>')
    if no_estimate:
        add(f'<div class="tile"><div class="k">No estimate</div><div class="v warn">'
            f'{len(no_estimate)}</div>'
            f'<div class="n">assumed {cfg.jira.default_estimate_days:.0f} senior-days</div></div>')
    add("</div>")

    # ---- Gantt ----
    month_starts = _month_starts(span_start, span_end)
    # Month lines positioned on the real date — months have different lengths, so a
    # fixed-step gradient would drift out of alignment with the bars.
    grid = "".join(f'<div class="gline" style="left:{pct(m):.3f}%"></div>' for m in month_starts)
    add('<section><h2>Roadmap by person</h2>')
    add('<div class="gantt"><div class="gantt-inner">')
    for person in cfg.people:
        mine = [a for a in staffed if a.person is person]
        lanes = _lanes(mine)
        height = max(1, len(lanes)) * 30 + 12
        add('<div class="grow">')
        add(f'<div class="gname" style="min-height:{height}px"><b>{E(person.name)}</b>'
            f'<span>{E(person.seniority)} · {person.fte:.0%} FTE · {len(mine)} epic(s)</span></div>')
        add('<div class="gtrack">')
        add(grid)
        add(f'<div class="today" style="left:{pct(today):.3f}%"></div>')
        if not lanes:
            add('<div class="lane"></div>')
        for lane in lanes:
            add('<div class="lane">')
            for a in lane:
                left = pct(a.start)
                width = max(0.6, (a.end - a.start).days + 1) / total_days * 100
                slot = (cfg.people.index(person) % 8) + 1
                ink = " ink-dark" if slot in dark_ink_slots else ""
                due_note = ""
                if a.epic.due:
                    late = a.end > a.epic.due
                    due_note = (
                        f"<br>Due {a.epic.due:%d/%m/%Y} — "
                        f"{str((a.end - a.epic.due).days) + 'd LATE' if late else 'on time'}"
                    )
                origin = ("✓ owner: label in Jira" if a.committed
                          else "◇ tool suggestion — nobody assigned yet")
                warn_note = f"<br>⚠ {E(a.warning)}" if a.warning else ""
                shared_note = ""
                if a.co_owners:
                    others = ", ".join(f"{n} ({f:.0%})" for n, f in a.co_owners)
                    shared_note = f"<br>shared with {E(others)}"
                tip = (
                    f"<b>{E(a.epic.key)}</b>{E(a.epic.summary)}<br>"
                    f"{E(person.name)} ({E(person.seniority)}) · {a.fte:.0%} FTE"
                    f"{shared_note}<br>"
                    f"{a.start:%d/%m/%Y} → {a.end:%d/%m/%Y} · epic total {a.epic.estimate_days:.1f} "
                    f"senior-days{due_note}<br>{origin}{warn_note}"
                )
                classes = ink + ("" if a.committed else " suggested")
                mark = '<span class="warn-mark">⚠</span>' if a.warning else ""
                add(
                    f'<div class="bar{classes}" style="left:{left:.3f}%;width:{width:.3f}%;'
                    f'background-color:{color_of[person.name]}" data-tip="{E(tip)}">'
                    f'{E(a.epic.key)}{mark}</div>'
                )
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
    add('<div style="margin-left:auto"><span class="swatch" '
        'style="background:var(--muted)"></span>solid = owner: label (fact)</div>')
    add('<div><span class="swatch suggested" style="background-color:var(--muted);'
        'background-image:repeating-linear-gradient(135deg,rgba(255,255,255,.5) 0 3px,'
        'rgba(255,255,255,0) 3px 6px)"></span>hatched = suggestion</div>')
    add("</div></div></div></section>")

    def owner_dots(plan) -> str:
        return "".join(
            f'<span class="dot" style="background:{color_of[a.person.name]}"></span>'
            for a in plan.owners
        )

    # ---- Quarter view ----
    buckets: dict[str, list] = {}
    for p in sorted(staffed_plans, key=lambda x: x.end):
        buckets.setdefault(cfg.fiscal_quarter(p.end), []).append(p)

    add('<section><h2>By quarter</h2>')
    add('<p class="sub" style="margin-bottom:16px">The same plan grouped by delivery '
        'quarter — the view for a planning conversation.</p>')
    add('<div class="quarters">')
    for q, items in buckets.items():
        effort = sum(p.epic.estimate_days for p in items)
        late_count = sum(1 for p in items if p.epic.due and p.end > p.epic.due)
        add('<div class="qcard">')
        add(f'<div class="qhead"><b>{E(q)}</b>'
            f'<span>{len(items)} epic(s) · {effort:.0f} senior-days</span></div>')
        if late_count:
            add(f'<div class="crit" style="font-size:11px;margin-bottom:8px">'
                f'⚠ {late_count} past deadline</div>')
        for p in items:
            marker = "▪" if p.committed else "◇"
            who = ", ".join(p.owner_names)
            tip = (f"<b>{E(p.epic.key)}</b>{E(p.epic.summary)}<br>"
                   f"{E(who)} · delivers {p.end:%d/%m/%Y}")
            add(f'<div class="qitem" data-tip="{E(tip)}">'
                f'{owner_dots(p)}'
                f'<span class="qkey">{marker} {E(p.epic.key)}</span>'
                f'<span class="qsum">{E(p.epic.summary[:44])}</span>'
                f'<span class="qwhen">{p.end:%d/%m}</span></div>')
        add("</div>")
    if unstaffed:
        add('<div class="qcard nodate"><div class="qhead"><b>No date</b>'
            f'<span>{len(unstaffed)} epic(s) with nobody eligible</span></div>')
        for p in unstaffed:
            add(f'<div class="qitem"><span class="qkey crit">✗ {E(p.epic.key)}</span>'
                f'<span class="qsum">{E(p.epic.summary[:44])}</span></div>')
        add("</div>")
    add("</div>")
    add('<p class="sub" style="margin:14px 0 0;font-size:12px">'
        '▪ owner: label in Jira &nbsp;·&nbsp; ◇ tool suggestion</p>')
    add("</section>")

    # ---- Utilisation heatmap ----
    labels, table = monthly_utilization(cfg, ledgers, today, months)
    add('<section><h2>Monthly utilisation</h2>')
    add('<p class="sub" style="margin-bottom:14px">Percentage of effective capacity already '
        'committed to the current backlog. Blank space on the right is what you have left '
        'to sell.</p>')
    add('<div class="heat"><table><thead><tr><th>Person</th>')
    for label in labels:
        add(f"<th>{label[5:]}/{label[2:4]}</th>")
    add("</tr></thead><tbody>")
    for person in cfg.people:
        add(f"<tr><td>{E(person.name)}</td>")
        for label in labels:
            used, cap = table[person.name][label]
            share = (used / cap * 100) if cap > 1e-9 else 0.0
            bucket = _heat_bucket(share)
            if cap <= 1e-9:
                add('<td class="cell" style="background:var(--grid);color:var(--muted)" '
                    'data-tip="No working days available (time off / holidays)">—</td>')
            else:
                tip = (
                    f"<b>{E(person.name)} · {label}</b>"
                    f"{used:.1f} of {cap:.1f} senior-days committed<br>"
                    f"free: {max(0.0, cap - used):.1f} senior-days"
                )
                add(
                    f'<td class="cell" style="background:var(--heat-{bucket});'
                    f'color:var(--heat-ink-{bucket})" data-tip="{E(tip)}">{share:.0f}%</td>'
                )
        add("</tr>")
    add("</tbody></table></div></section>")

    # ---- Suggestions: epics with no declared owner ----
    suggested_items = [p for p in staffed_plans if not p.committed]
    if suggested_items:
        add('<section><h2>Unassigned epics — who would take them and when</h2>')
        add('<p class="sub" style="margin-bottom:14px">None of these carry an '
            '<code>owner:</code> label in Jira. The tool proposes the owner by the first '
            'free window among those who have the skill. To make it a commitment, add the '
            'label in Jira.</p>')
        add('<div class="tw"><table><thead><tr><th>Epic</th><th>Summary</th>'
            "<th>Suggestion</th><th class='num'>Wait</th><th>Starts</th><th>Delivers</th>"
            "<th>Alternatives</th></tr></thead><tbody>")
        for p in sorted(suggested_items, key=lambda x: x.start):
            person = p.owners[0].person
            wait_days = (p.start - today).days
            tone = "ok" if wait_days == 0 else ("warn" if wait_days <= 30 else "crit")
            label_text = "today" if wait_days == 0 else f"{wait_days}d"
            alts = ", ".join(f"{o.person.name} ({o.start:%d/%m})"
                             for o in p.alternatives) or "—"
            add(f"<tr><td>{E(p.epic.key)}</td><td>{E(p.epic.summary)}</td>"
                f'<td>{swatch_for(color_of, person)}{E(person.name)} '
                f'<span class="pill">{E(person.seniority)}</span></td>'
                f"<td class='num'><span class=\"{tone}\">{label_text}</span></td>"
                f"<td>{p.start:%d/%m/%Y}</td><td>{p.end:%d/%m/%Y}</td>"
                f"<td style='color:var(--text-secondary)'>{E(alts)}</td></tr>")
        add("</tbody></table></div></section>")

    # ---- Full table (alternative view, required by the relief rule) ----
    add('<section><h2>Detailed allocation</h2>')
    add('<div class="tw"><table><thead><tr><th>Epic</th><th>Summary</th><th>Owner(s)</th>'
        "<th>Priority</th><th>Origin</th><th class='num'>FTE</th>"
        "<th>Start</th><th>End</th><th class='num'>Senior-days</th><th>Skills</th>"
        "<th>Deadline</th></tr></thead><tbody>")
    for p in sorted(staffed_plans, key=lambda x: (x.start, x.end)):
        owner_cell = ", ".join(
            f'<span class="dot" style="background:{color_of[a.person.name]}"></span>'
            f'{E(a.person.name)}' + (f' ({a.fte:.0%})' if a.fte < 0.999 else '')
            for a in p.owners
        )
        fte_cell = f"{sum(a.fte for a in p.owners):.0%}" if len(p.owners) > 1 else \
            f"{p.owners[0].fte:.0%}"
        est = f"{p.epic.estimate_days:.1f}"
        if p.epic.estimate_missing:
            est = f'<span class="warn" title="no estimate in Jira">{est} ?</span>'
        skills = " ".join(f'<span class="pill">{E(s)}</span>' for s in sorted(p.epic.skills)) or "—"
        if p.epic.due is None:
            due_cell = '<span style="color:var(--muted)">—</span>'
        elif p.end > p.epic.due:
            due_cell = f'<span class="crit">{(p.end - p.epic.due).days}d late</span>'
        else:
            due_cell = f'<span class="ok">on time</span>'
        if p.committed:
            origin = '<span class="pill" title="owner: label in Jira">✓ fact</span>'
        else:
            origin = '<span class="pill" title="tool proposal">◇ suggestion</span>'
        if p.warning:
            origin += f' <span class="warn" title="{E(p.warning)}">⚠</span>'
        prio = E(p.epic.priority or "—")
        if p.epic.priority_forced:
            prio = (f'<b title="forced in [priority]; Jira said '
                    f'{E(p.epic.jira_priority or "nothing")}">{prio}*</b>')
        add(
            f"<tr><td>{E(p.epic.key)}</td><td>{E(p.epic.summary)}</td>"
            f"<td>{owner_cell}</td><td>{prio}</td><td>{origin}</td>"
            f"<td class='num'>{fte_cell}</td><td>{p.start:%d/%m/%Y}</td>"
            f"<td>{p.end:%d/%m/%Y}</td><td class='num'>{est}</td><td>{skills}</td>"
            f"<td>{due_cell}</td></tr>"
        )
    add("</tbody></table></div></section>")

    # ---- Unallocated ----
    if unstaffed:
        add('<section><h2><span class="crit">⚠</span> Epics with nobody eligible</h2>')
        add('<p class="sub" style="margin-bottom:14px">Nobody on the roster meets the '
            'requirements. This is hiring, training, or scope renegotiation — not a queue.</p>')
        add('<div class="tw"><table><thead><tr><th>Epic</th><th>Summary</th><th>Needs</th>'
            "<th>Reason</th></tr></thead><tbody>")
        for p in unstaffed:
            need = " ".join(f'<span class="pill">{E(s)}</span>' for s in sorted(p.epic.skills)) or "—"
            if p.epic.min_seniority:
                need += f' <span class="pill">min: {E(p.epic.min_seniority)}</span>'
            add(f"<tr><td>{E(p.epic.key)}</td><td>{E(p.epic.summary)}</td><td>{need}</td>"
                f"<td style='color:var(--text-secondary)'>{E(p.reason)}</td></tr>")
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
            + ", ".join(f"<b>{E(s)}</b> ({', '.join(k)})" for s, k in sorted(gaps.items()))
            + "</p>")
    add("</section>")

    body = "\n".join(parts)
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{E(cfg.team_name)} — Roadmap &amp; Capacity</title>"
        f"<style>{_css(len(cfg.people))}</style></head><body>"
        f'<div class="wrap">{body}</div><div id="tip"></div>'
        f"<script>{_js()}</script></body></html>"
    )
