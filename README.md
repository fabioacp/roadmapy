# DPE Capacity

A date-driven roadmap and availability planner for the **Developer Platform
Engineering** team, built from Jira Epics.

It reads your epics (via CSV export or the API) and answers three things:

1. **What's the roadmap?** — the epics being worked, plotted over their planned
   period (their `start:`/`end:` labels).
2. **Who is available for the next epic?** — computed from when people finish
   their active work, matched by skill.
3. **What's waiting, and for how long?** — the queue, in priority order, each with
   the first skilled person to free up and the wait until they can start.

Pure Python 3.11+. **Zero dependencies** — no `pip install`.

---

## Getting started

```bash
cd ~/Desktop/facp-claude

./dpe clean                            # tidy the raw Jira export into data/epics.csv
./dpe validate                         # checks config + data
./dpe roadmap --html out/roadmap.html  # timeline + queue, as a visual report
./dpe availability                     # who is free, and when
```

`./dpe --help` lists every command. The tool works from any directory; add an
alias if you like:

```bash
echo "alias dpe='~/Desktop/facp-claude/dpe'" >> ~/.zshrc && source ~/.zshrc
```

---

## The model in one picture

Everything lives in Jira, as labels on the epic.

```
ACTIVE epic   start:2026-07-01  end:2026-09-11  owner:alex
              → plotted on the timeline over that period, in Alex's lane.

SHARED        start:2026-08-01  end:2026-10-20  owner:alex:50  owner:daniell:50
              → both own it; it shows once, in both their lanes.

QUEUED epic   skill:backstage        (no start:/end:)
              → waits, ordered by priority. The tool finds the first person with
                the skill to free up, and reports the wait.
```

A person is **free from** the latest `end:` among the epics they are actively on
(today if they hold none). The queue is **independent**: every waiting epic is
measured against that same availability — taking one does not make its owner busy
for the next, so each answer is a clean "earliest possible start."

Jira has no start-date field, which is why the period comes from labels. The end
can point at a separate `end:` label or you can mirror the Due date into it —
your call.

---

## Data sources

Epics come from one of two interchangeable sources. Pick one in `[jira] source`,
or override with `--source csv|api` on any command. Both run through the **same
normalisation**, and a test guarantees they produce an identical plan.

### `csv` — manual export, cleaned by the tool

In Jira, filter by `type = Epic AND project = DPE`, then **Export → Export Excel
CSV (all fields)**. Drop the raw file into the folder `[jira] raw_csv` points at —
a folder of dated exports is fine, the newest wins. Then:

```bash
./dpe clean          # reads the raw export, writes data/epics.csv
```

`clean` keeps only the columns mapped in `[jira.columns]`, folds the repeated
`Labels` into one cell, and writes a small stable CSV. Everything downstream reads
that clean file. If the raw export is newer than the cleaned one, `validate` and
`roadmap` remind you to re-run `clean`.

### `api` — Jira Cloud REST v3 *(currently stubbed)*

The client in `src/dpe/sources/api_source.py` is real and complete: it builds the
URL, paginates, handles 400/401/403/429/5xx with retry, and maps the response.
**Only the transport is stubbed** — the layer that speaks HTTP — so parsing and
pagination already run for real against a fixture in the API's exact shape. Going
live is swapping the transport, not rewriting the client:

```toml
[jira]
source = "api"
[jira.api]
transport = "http"                              # was "stub"
base_url = "https://yourcompany.atlassian.net"
email = "you@yourcompany.com"
```
```bash
export JIRA_API_TOKEN='your-token'   # id.atlassian.com/manage-profile/security/api-tokens
./dpe validate --source api
```

The token never goes in the config — only the environment variable name.

---

## Labels the tool reads

| Label | Effect |
|---|---|
| `skill:terraform` | the epic requires the `terraform` skill |
| `min:senior` | only senior level or above can take it |
| `owner:alex` | Alex owns it (100%) |
| `owner:daniell:60` | Daniell owns it at 60% |
| `start:2026-07-01` | planned start of the period |
| `end:2026-09-11` | planned end of the period |

An epic with **both** `start:` and `end:` is **active** (plotted). Without them it
is **queued**. The slug after `owner:` matches a person's `alias` in `[[people]]`
(default = a slug of their name, `Ana Souza` → `ana-souza`).

You can also override an epic's priority in `config.toml`:

```toml
[priority]
"DPE-1141" = "Highest"   # forced; roadmap marks it with *
```

---

## Commands

| Command | What it does |
|---|---|
| `./dpe clean [PATH]` | tidy a raw Jira export into `data/epics.csv` |
| `./dpe validate` | check config + data; flag owner/skill/date problems |
| `./dpe roadmap [--view V] [--html FILE]` | timeline of active epics + the queue |
| `./dpe queue` | waiting epics by priority + when someone frees up |
| `./dpe availability` | who is free, and when |
| `./dpe estimate --skills ... [--min-seniority ...]` | for a new request, who could take it and when |
| `./dpe skills` | skill coverage and bus factor |
| `./dpe source` | the raw issues the source returns, for debugging |

`roadmap --view` picks the cut: `person` (each schedule + availability), `epic`
(active epics and their periods), `quarter` (grouped by fiscal quarter). The HTML
report includes all of them plus the queue timeline, availability table, and skill
coverage — opens on double-click, works offline, follows the system theme.

`validate` flags: skills nobody has, owner labels that don't cover the epic,
overlapping active epics for one person, ownerless epics with dates (sent to the
queue), and periods that run past the Jira Due date.

Global flags: `--date YYYY-MM-DD` (treat as today), `--source csv|api`. Both work
before or after the subcommand.

---

## Config knobs

- **`[team] fiscal_year_start_month`** (default 7 = July) sets where Q1 begins. The
  FY is named after the year it ends in, so August 2026 is Q1 FY27. Set to 1 for
  calendar quarters.
- **`[throughput]`** lists the seniority levels that exist. The tool is
  date-driven, so these are not effort multipliers — only the names and the
  `[seniority] order` matter, for `min:senior` and skill coverage.
- **`[[people]]`** — name, seniority, skills, optional `alias`, optional `pto`
  (time off, so someone on holiday isn't offered as "free").

---

## Limits you should know about

- **Active periods are taken as given.** The tool plots the `start:`/`end:` you
  set. One person can't work two epics at once, so their epics should run back to
  back — `validate` warns if two of a person's active periods overlap, but it
  won't reshuffle the dates for you; fix the labels.
- **The queue is independent, not a cascade.** Each waiting epic reports the
  earliest a skilled person *could* start it, ignoring the other queued epics. Two
  epics needing the same person both show that person's free date. It's an
  earliest-possible-start, not a committed sequence.
- **No dependencies between epics.** If DPE-1021 needs DPE-1011 finished, the model
  does not know. (`not-before:` labels or Jira issue links would be the way in.)

---

## Layout

```
config/config.toml             roster (with aliases), seniority, priority, jira mapping
data/raw/                      drop your raw Jira exports here
data/epics.csv                 the cleaned file (written by `dpe clean`)
data/jira_api_stub.json        API fixture, in the exact shape of the real endpoint
src/dpe/config.py              loads and validates the config
src/dpe/jira.py                Epic/Owner model + label normalisation
src/dpe/clean.py               raw export -> tidy CSV
src/dpe/sources/               csv + api sources (StubTransport/HttpTransport)
src/dpe/scheduler.py           active work, availability, queue
src/dpe/report.py              HTML report
src/dpe/cli.py                 commands
tests/test_sources.py          34 tests
./dpe                          shortcut to run without installing
```

```bash
python3 -m unittest discover tests
```
