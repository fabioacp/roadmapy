# DPE Capacity

Capacity and roadmap planner for the **Developer Platform Engineering** team.

Reads Epics from Jira — via CSV export or the API — cross-references who is
working on what, and answers the two questions that matter:

> *"Which epics have nobody on them, who could take each one, and when would
> that person be free?"* → `./dpe suggest`
>
> *"A request came in needing Terraform and AWS, 8 points, and it needs a senior.
> When do we start and when do we deliver?"* → `./dpe estimate`

**The tool separates fact from proposal.** An epic with an `owner:` label in Jira
is fact — the tool only computes its dates. Every epic without one is open
backlog, and only then does it propose an owner. The roadmap marks the two
distinctly, so you never present a suggestion as if it were a commitment.

Pure Python 3.11+. **Zero dependencies** — no `pip install`.

---

## Getting started

```bash
cd ~/Desktop/facp-claude

./dpe clean                           # tidy the raw Jira export into data/epics.csv
./dpe validate                        # checks config + data
./dpe suggest                         # unassigned epics + who would take them
./dpe roadmap --html out/roadmap.html # the visual report
```

`./dpe --help` lists every command; `./dpe <command> --help` details each one.
The tool works from any directory — add an alias if you like:

```bash
echo "alias dpe='~/Desktop/facp-claude/dpe'" >> ~/.zshrc && source ~/.zshrc
```

`validate` is the first command to run whenever the source changes — when a
column mapping does not match, it prints the CSV's real header so you can fix
`config.toml` without guessing.

---

## Data sources

Epics come from one of two interchangeable sources. Pick one in `[jira] source`,
or override it with `--source csv|api` on any command.

Both run through the **same normalisation** (`src/dpe/jira.py`): done-status
filtering, labels → skills, points → senior-days. A test guarantees they produce
an identical roadmap, so switching sources cannot change the result.

```bash
./dpe roadmap --source csv
./dpe roadmap --source api
./dpe source --source api    # raw issues, for debugging the mapping and JQL
```

### `csv` — manual export, cleaned by the tool

In the Jira issue search, filter by `type = Epic AND project = DPE`, then
**Export → Export Excel CSV (all fields)**. Drop that raw file (dozens of
columns, repeated `Labels`) into the folder `[jira] raw_csv` points at — a folder
of dated exports is fine, the newest wins.

Then let the tool tidy it:

```bash
./dpe clean          # reads the raw export, writes data/epics.csv
```

`clean` keeps only the columns mapped in `[jira.columns]`, folds the repeated
`Labels` into one cell, and writes a small stable CSV. Everything downstream
reads that clean file and never sees Jira's chaos. If the raw export is newer
than the cleaned file, `validate` and `roadmap` remind you to re-run `clean`.

Column names vary per instance — that is why `[jira.columns]` exists.

### `api` — Jira Cloud REST v3 *(currently stubbed)*

The client in `src/dpe/sources/api_source.py` is real and complete: it builds the
URL, paginates, handles 400/401/403/429/5xx with retry and backoff, and maps the
response. **Only the transport is stubbed** — the layer that speaks HTTP.

That is deliberate: the parsing and pagination paths already run for real today,
against a fixture in the exact shape the API returns (`data/jira_api_stub.json`,
with pagination simulated via `nextPageToken`). Going live means swapping the
transport, not rewriting the client.

**Two steps to go live:**

```toml
# config/config.toml
[jira]
source = "api"

[jira.api]
transport = "http"                              # was "stub"
base_url = "https://yourcompany.atlassian.net"
email = "you@yourcompany.com"
```

```bash
export JIRA_API_TOKEN='your-token'   # id.atlassian.com/manage-profile/security/api-tokens
./dpe validate --source api          # confirms the credential before anything else
```

The token **never** goes in the config file — only the environment variable name,
in `token_env`.

`[jira.api.fields] estimate` points at the Story Points custom field, whose ID
differs per instance. Find yours:

```bash
curl -u you@yourcompany.com:$JIRA_API_TOKEN \
  https://yourcompany.atlassian.net/rest/api/3/field | grep -i "story point"
```

Endpoint used: `GET /rest/api/3/search/jql`. The classic `/rest/api/3/search` was
deprecated by Atlassian; the new one paginates by `nextPageToken`, not `startAt`.

---

## Who is working on what — via Jira labels

Ownership lives entirely in Jira, as `owner:` labels on the epic, next to the
`skill:` labels the tool already reads. There is no assignment table in the
config to keep in sync.

| Label on the epic | Meaning |
|---|---|
| `owner:alex` | Alex owns it, 100% dedicated |
| `owner:daniell:60` | Daniell, 60% of his day (40% free for other work) |
| `owner:alex:50` `owner:daniell:50` | **shared** — both own it, co-scheduled |

The slug after `owner:` must match a person's `alias` in `[[people]]`, which
defaults to a slug of their name (`Ana Souza` → `ana-souza`); set an explicit
`alias` if you need something else.

**Shared ownership is co-scheduled.** When two or three people own one epic, they
burn its effort together — each contributing their free capacity × FTE per day —
and finish on the same date. The roadmap shows the epic once, listing all owners.

`fte` is fine control: `owner:x:50` spends half that person's day here, leaving
the rest for another epic. Add up a person's FTE across all their epics — past
100% they are overcommitted and `validate` warns you (the work **stretches out
over time**, it does not happen in parallel).

**An owner label is always honoured**, even if that person (or the group) lacks a
required skill or the minimum seniority — it is your call. But it gets flagged
with ⚠ in the roadmap and in `validate`.

An epic with **no** `owner:` label is open backlog: the tool suggests an owner
(`./dpe suggest`). Jira's `Assignee` field schedules nothing, but `validate` uses
it as a hint — if an epic has an Assignee and no `owner:` label, it prints the
label to add.

---

## Priority

By default the order comes from Jira's Priority field. To decide it yourself,
override in `config/config.toml` using the same scale:

```toml
[priority]
"DPE-1141" = "Highest"   # the catalogue became a leadership commitment
"DPE-1051" = "Low"       # can wait, nobody is blocked
```

Anything not listed keeps its Jira priority. `roadmap` marks forced ones with `*`,
and `validate` shows the before → after and warns if a key is not in the backlog.

**Ties:** within the same level, the order is deadline → epic key. If you need an
exact order between two epics at the same level, give them Due dates in Jira.

### Labels the tool reads

| Label | Effect |
|---|---|
| `skill:terraform` | the epic requires the `terraform` skill |
| `min:senior` | only people at senior level or above can take it |

An epic with no `skill:*` label can be taken by anyone. An epic with no estimate
gets `default_estimate_days` and is flagged in the report — it does not vanish.

---

## Commands

### `./dpe clean [PATH]`
Reads the raw Jira export (from `[jira] raw_csv`, or the path you pass), keeps
only the columns you use, folds the repeated `Labels` into one cell, and writes
the tidy `data/epics.csv` the rest of the tool reads. Point `raw_csv` at a folder
and the newest `*.csv` inside wins.

### `./dpe validate [--source csv|api]`
Checks config + data source, lists each person's daily capacity, and warns about
epics with no estimate, epics with no skill, and skills nobody on the team has.
With `--source api` and `transport = "http"`, it also confirms the token is in the
environment **before** any network call.

### `./dpe suggest`
**The epics nobody is touching.** For each one the tool proposes who would take
it — based on skill, seniority, and first free window — and says how many days
of waiting until that person can start, plus the alternatives.

```
EPIC      SUMMARY                    SUGGESTION           WAIT  STARTS    DELIVERS
DPE-1041  Self-service S3 buckets    Oscar (mid)            0d  22/07/26  25/08/26
DPE-1071  Cost dashboard per squad   Oscar (mid)           34d  25/08/26  15/09/26
DPE-1141  Service catalogue          Karthi (mid)          56d  16/09/26  18/12/26
```

Suggestions compete with each other for the capacity left over after committed
work — which is why the plan is coherent, not a list of conflicting optimistic
dates. At the end it prints the `owner:` label to add in Jira.

### `./dpe roadmap [--view V] [--strategy S] [--html FILE]`
Allocates every open epic and prints the roadmap.

**`--view`** picks the cut:

| View | For what |
|---|---|
| `person` *(default)* | each person's schedule — who is busy with what |
| `epic` | the team's delivery sequence, one row per epic |
| `quarter` | grouped by fiscal quarter — the view for planning conversations |

**`--strategy`** picks the order of the open backlog (`priority` is the default;
see `compare` below).

With `--html`, it generates a self-contained report with **all** the views: Gantt
by person, quarter cards, utilisation heatmap, suggestions, detailed table, and
skill coverage. Opens on double-click, works offline, follows the system
light/dark theme.

### `./dpe compare [--detail]`
Runs the three strategies side by side and shows what each choice costs:

| Strategy | Orders by |
|---|---|
| `priority` | the priority you defined |
| `deadline` | tightest deadline first |
| `quick-wins` | smallest epics first — drains the queue faster |

Compares end date, late epics, days late, average wait, and first delivery. The
owned work is identical in all three — only the open backlog order changes.

**When all three end on the same date**, the command says so and points at the
critical path. It means reordering will not help: the bottleneck is capacity or
skill, not priority. Real output:

```
ⓘ All three strategies end on the SAME date. Reordering the backlog
  will not help — the bottleneck is capacity, not order.

  Critical path: DPE-1021 (Golden path for Go services in Backstage),
  which can only go to Karthi (60% FTE).
  Skill with no backup: backstage — only Karthi has it.
```

`--detail` shows the full order for each strategy.

### `./dpe capacity [--months N]`
Monthly utilisation per person and — the line that matters in a negotiation —
**free senior-days per month**, with a team total.

### `./dpe estimate --skills ... [--days N | --points N] [--min-seniority ...]`
The simulator. Runs a hypothetical request against the already-committed backlog,
without altering it, and reports per eligible person: when they can start, when
they deliver, and how many days of waiting. It always calls out **the earliest
senior** and the **earliest non-senior** separately.

```bash
./dpe estimate --skills terraform,aws --points 8 --min-seniority senior
./dpe estimate --skills python --days 15 --verbose   # -v shows who was ruled out and why
```

If nobody is eligible, the output says so explicitly — the problem is hiring,
training, or scope, not a queue.

### `./dpe source [--source csv|api]`
Prints the **raw** issues the source returned, before normalisation. This is the
command for "why doesn't this epic show up?" and "what is the JQL actually
picking up?".

### `./dpe skills`
Skill coverage vs. backlog demand, with bus factor. This is where "`backstage`:
1 person, 0 seniors, 51 days of demand" shows up before it becomes a crisis.

### Global flags
- `--date YYYY-MM-DD` — simulate from another date. Useful for "what if we only
  start in October?".
- `--source csv|api` — override `[jira] source`.

Both work before or after the subcommand.

---

## Tests

```bash
python3 -m unittest discover tests
```

43 tests, stdlib only. They cover CSV↔API parity, API pagination, credential
errors, label/estimate normalisation, `owner:` label parsing (single, partial
FTE, shared, unknown alias), shared co-scheduling (all owners finish together,
effort split not duplicated), `[priority]` overrides, the three strategies (none
loses or invents an epic, none changes ownership), the three views, the `clean`
command, and the scheduler's invariants (nobody works during time off; a
suggestion never ignores skill or seniority; daily capacity is never exceeded).

---

## The model behind the numbers

**Senior-day** is the unit: one day of effective work by a senior engineer.

```
daily_capacity(person) = fte × (1 − overhead_pct) × throughput[seniority]
```

- **`overhead_pct`** (default 0.30) is the time that does not go to epics:
  meetings, on-call, support, interrupts. This is the number that wrecks roadmaps
  most often — be honest.
- **`throughput`** converts seniority into output. A `mid` at 0.7 takes 1.43 days
  to deliver 1 senior-day. It is not a value judgement, it is calibration.
- **`fte` on an owner label** is how much of a person goes into each epic. It is
  what controls parallelism: two epics at `0.5` run together; one at `1.0` is full
  focus. It is also what shared ownership splits across co-owners.
- **`fiscal_year_start_month`** (default 7 = July) sets where Q1 begins. The FY is
  named after the year it ends in, so August 2026 falls in Q1 FY27. Set it to 1
  for calendar quarters.

The calculation has two phases. First the **owned** work: each epic with `owner:`
labels consumes the declared people's capacity at the declared FTE; shared epics
are co-scheduled so their owners finish together. Then the **suggestions**: the
remaining epics, in whatever order the chosen strategy dictates, compete for what
is left; each goes to the eligible person who **finishes earliest**. Time off and
holidays drop out of the affected person's calendar.

The strategy reorders the backlog and can shift an owned epic's **dates** (when
one person owns several, something has to run first) — but it never changes
**who** owns what.

### Limits you should know about

- **There are no dependencies between epics.** If DPE-102 needs DPE-101 finished,
  the model does not know. That makes the roadmap optimistic for chained backlogs.
- **Greedy is not optimal.** High priority takes the best person first, even when
  swapping would reduce the total makespan. That is why `compare` exists: instead
  of promising the optimum, it shows the cost of each order and leaves the choice
  to you.
- **Bad estimates in, bad roadmap out.** `validate` lists epics with no estimate
  precisely so you do not trust them unknowingly.
- **In-flight epics are replanned from scratch.** An `In Progress` epic is
  scheduled for its full effort starting today — work already done is not
  deducted, because the tool does not read time logs. Every started epic is
  overestimated.

---

## Layout

```
config/config.toml             roster (with aliases), throughput, holidays, priority
data/raw/                      drop your raw Jira exports here
data/epics.csv                 the cleaned file (written by `dpe clean`)
data/jira_api_stub.json        API fixture, in the exact shape of the real endpoint
src/dpe/config.py              loads and validates the config
src/dpe/jira.py                Epic/Owner model + shared normalisation
src/dpe/clean.py               raw export -> tidy CSV
src/dpe/sources/__init__.py    source factory
src/dpe/sources/csv_source.py  CSV reader (repeated or folded Labels)
src/dpe/sources/api_source.py  REST v3 client + StubTransport/HttpTransport
src/dpe/scheduler.py           capacity ledger, co-scheduling, simulation
src/dpe/report.py              HTML report
src/dpe/cli.py                 commands
tests/test_sources.py          43 tests
./dpe                          shortcut to run without installing
```
