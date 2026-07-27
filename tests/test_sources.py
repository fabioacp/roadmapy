"""Test suite. Stdlib only: python3 -m unittest discover tests

The test that matters most is `test_csv_and_api_produce_the_same_plan` — it is
the guarantee that switching the transport from stub to http does not change the
result through some parsing accident.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dpe import clean as clean_mod  # noqa: E402
from dpe import sources  # noqa: E402
from dpe.config import load  # noqa: E402
from dpe.jira import JiraError, Owner, RawIssue, normalize, normalize_all  # noqa: E402
from dpe.scheduler import availability_for, build_plan  # noqa: E402
from dpe.sources import api_source  # noqa: E402

CONFIG = ROOT / "config" / "config.toml"
TODAY = date(2026, 7, 25)


def fresh_config():
    return load(CONFIG)


def epic(cfg, key, source="csv"):
    return next(e for e in sources.load_epics(cfg, source) if e.key == key)


def mk(cfg, key, *, skills=(), owners=(), start=None, end=None,
       priority="Medium", min_sen=None, status="To Do"):
    """Build an epic synthetically, so tests don't depend on the sample data.
    owners: list of (alias, pct-or-None)."""
    labels = [f"skill:{s}" for s in skills]
    for alias, pct in owners:
        labels.append(f"owner:{alias}" + (f":{pct}" if pct else ""))
    if start:
        labels.append(f"start:{start}")
    if end:
        labels.append(f"end:{end}")
    if min_sen:
        labels.append(f"min:{min_sen}")
    return normalize(cfg, RawIssue(key=key, status=status, priority=priority, labels=labels))


class TestSourceParity(unittest.TestCase):
    def test_csv_and_api_produce_the_same_plan(self):
        cfg = fresh_config()
        results = {}
        for source in ("csv", "api"):
            plan = build_plan(cfg, sources.load_epics(cfg, source), TODAY)
            results[source] = (
                sorted((aw.epic.key, aw.person.name if aw.person else None,
                        aw.start, aw.end) for aw in plan.active),
                sorted((q.epic.key, q.person.name if q.person else None,
                        q.available_on) for q in plan.queue),
                {n: a.free_from for n, a in plan.availability.items()},
            )
        self.assertEqual(results["csv"], results["api"])

    def test_done_epics_are_filtered_out(self):
        cfg = fresh_config()
        raws = sources.fetch_raw(cfg, "csv")
        done = {r.key for r in raws if r.status.lower() in cfg.jira.done_statuses}
        self.assertTrue(done, "sample should include at least one Done epic")
        live = {e.key for e in sources.load_epics(cfg, "csv")}
        self.assertFalse(done & live, "a Done epic leaked into the plan")


class TestApiPagination(unittest.TestCase):
    def test_pages_without_losing_or_duplicating(self):
        cfg = fresh_config()
        total = len(sources.fetch_raw(cfg, "api"))
        cfg.jira.api.page_size = 5
        transport = api_source.StubTransport(cfg.resolve(cfg.jira.api.stub_file), 5)
        raws = api_source.fetch(cfg, transport)
        self.assertEqual(len({r.key for r in raws}), total)
        self.assertEqual(transport.calls, (total + 4) // 5)  # ceil(total/5)

    def test_transport_that_never_ends_is_interrupted(self):
        class Runaway:
            def get(self, url, headers, timeout):
                return {"issues": [{"key": "X-1", "fields": {}}],
                        "isLast": False, "nextPageToken": "1"}

        with self.assertRaises(JiraError) as ctx:
            api_source.fetch(fresh_config(), Runaway())
        self.assertIn("pagination", str(ctx.exception))

    def test_response_without_issues_fails_usefully(self):
        class Broken:
            def get(self, url, headers, timeout):
                return {"warningMessages": ["bad jql"]}

        with self.assertRaises(JiraError) as ctx:
            api_source.fetch(fresh_config(), Broken())
        self.assertIn("issues", str(ctx.exception))


class TestCredentials(unittest.TestCase):
    def test_http_without_token_fails_before_any_network(self):
        cfg = fresh_config()
        cfg.jira.api.transport = "http"
        cfg.jira.api.token_env = "MISSING_ENV_VAR_DPE"
        with self.assertRaises(JiraError) as ctx:
            api_source.build_transport(cfg)
        self.assertIn("MISSING_ENV_VAR_DPE", str(ctx.exception))

    def test_missing_fixture_gives_a_clear_error(self):
        with self.assertRaises(JiraError) as ctx:
            api_source.StubTransport(Path("/nope/missing.json"), 100)
        self.assertIn("not found", str(ctx.exception))


class TestNormalisation(unittest.TestCase):
    def test_labels_become_skills_and_min_seniority(self):
        cfg = fresh_config()
        e = normalize(cfg, RawIssue(key="T-1", status="To Do",
                      labels=["skill:go", "skill:AWS", "min:senior", "tech-debt"]))
        self.assertEqual(e.skills, {"go", "aws"})
        self.assertEqual(e.min_seniority, "senior")

    def test_done_status_disappears(self):
        cfg = fresh_config()
        self.assertIsNone(normalize(cfg, RawIssue(key="T-1", status="Done")))

    def test_unknown_seniority_fails(self):
        cfg = fresh_config()
        with self.assertRaises(JiraError):
            normalize(cfg, RawIssue(key="T-1", status="To Do", labels=["min:staff"]))


class TestOwnerLabels(unittest.TestCase):
    def test_single_owner_defaults_to_full_fte(self):
        cfg = fresh_config()
        e = normalize(cfg, RawIssue(key="T-1", status="To Do", labels=["owner:alex"]))
        self.assertEqual(e.owners, [Owner("Alex", 1.0)])

    def test_owner_with_percentage(self):
        cfg = fresh_config()
        e = normalize(cfg, RawIssue(key="T-1", status="To Do", labels=["owner:daniell:60"]))
        self.assertEqual(e.owners, [Owner("Daniell", 0.6)])

    def test_shared_ownership(self):
        cfg = fresh_config()
        e = normalize(cfg, RawIssue(key="T-1", status="To Do",
                      labels=["owner:alex:50", "owner:daniell:50"]))
        self.assertEqual({o.person for o in e.owners}, {"Alex", "Daniell"})

    def test_unknown_alias_fails_with_known_list(self):
        cfg = fresh_config()
        with self.assertRaises(JiraError) as ctx:
            normalize(cfg, RawIssue(key="T-1", status="To Do", labels=["owner:nobody"]))
        self.assertIn("alex", str(ctx.exception))

    def test_alias_defaults_to_slug_of_name(self):
        cfg = fresh_config()
        self.assertEqual(cfg.person("Alex").alias, "alex")


class TestPeriodLabels(unittest.TestCase):
    def test_start_and_end_labels_make_an_epic_active(self):
        cfg = fresh_config()
        e = normalize(cfg, RawIssue(key="T-1", status="To Do",
                      labels=["owner:alex", "start:2026-07-01", "end:2026-09-11"]))
        self.assertTrue(e.scheduled)
        self.assertEqual(e.planned_start, date(2026, 7, 1))
        self.assertEqual(e.planned_end, date(2026, 9, 11))

    def test_missing_one_label_leaves_epic_queued(self):
        cfg = fresh_config()
        e = normalize(cfg, RawIssue(key="T-1", status="To Do", labels=["start:2026-07-01"]))
        self.assertFalse(e.scheduled)

    def test_end_before_start_fails(self):
        cfg = fresh_config()
        with self.assertRaises(JiraError) as ctx:
            normalize(cfg, RawIssue(key="T-1", status="To Do",
                      labels=["start:2026-09-01", "end:2026-07-01"]))
        self.assertIn("before", str(ctx.exception))

    def test_bad_date_in_label_fails(self):
        cfg = fresh_config()
        with self.assertRaises(JiraError):
            normalize(cfg, RawIssue(key="T-1", status="To Do", labels=["start:nope"]))


class TestActivePlan(unittest.TestCase):
    def test_active_epics_use_their_planned_periods(self):
        cfg = fresh_config()
        e = mk(cfg, "A-1", skills=["terraform"], owners=[("alex", None)],
               start="2026-07-01", end="2026-09-11")
        plan = build_plan(cfg, [e], TODAY)
        aw = plan.active[0]
        self.assertEqual((aw.start, aw.end), (date(2026, 7, 1), date(2026, 9, 11)))

    def test_shared_epic_appears_once_per_owner_same_period(self):
        cfg = fresh_config()
        e = mk(cfg, "A-1", skills=["ci-cd"], owners=[("alex", 50), ("brad", 50)],
               start="2026-08-01", end="2026-10-20")
        plan = build_plan(cfg, [e], TODAY)
        self.assertEqual({aw.person.name for aw in plan.active}, {"Alex", "Brad"})
        self.assertEqual({aw.end for aw in plan.active}, {date(2026, 10, 20)})


class TestAvailability(unittest.TestCase):
    def test_free_from_is_the_latest_active_end(self):
        cfg = fresh_config()
        epics = [
            mk(cfg, "A-1", owners=[("alex", None)], start="2026-07-01", end="2026-09-11"),
            mk(cfg, "A-2", owners=[("alex", None)], start="2026-08-01", end="2026-10-20"),
        ]
        plan = build_plan(cfg, epics, TODAY)
        self.assertEqual(plan.availability["Alex"].free_from, date(2026, 10, 20))

    def test_person_with_no_active_work_is_free_today(self):
        cfg = fresh_config()
        plan = build_plan(cfg, [mk(cfg, "A-1", owners=[("alex", None)],
                                   start="2026-07-01", end="2026-09-11")], TODAY)
        self.assertEqual(plan.availability["Brad"].free_from, TODAY)
        self.assertFalse(plan.availability["Brad"].busy)


class TestQueue(unittest.TestCase):
    def _sample_plan(self, cfg):
        epics = [
            mk(cfg, "A-1", owners=[("alex", None)], start="2026-07-01", end="2026-09-30"),
            mk(cfg, "Q-hi", skills=["ci-cd"], priority="High"),
            mk(cfg, "Q-lo", skills=["ci-cd"], priority="Low"),
            mk(cfg, "Q-tf", skills=["terraform"], priority="Medium"),
            mk(cfg, "Q-none", skills=["cobol"], priority="Medium"),
        ]
        return build_plan(cfg, epics, TODAY)

    def test_queue_is_priority_ordered(self):
        cfg = fresh_config()
        plan = self._sample_plan(cfg)
        ranks = [cfg.jira.priority_rank(q.epic.priority) for q in plan.queue]
        self.assertEqual(ranks, sorted(ranks))

    def test_queue_picks_the_earliest_free_eligible_person(self):
        cfg = fresh_config()
        plan = self._sample_plan(cfg)
        # Q-hi needs ci-cd; Brad/Karthi have it and are free now — Alex (busy) loses.
        item = next(q for q in plan.queue if q.epic.key == "Q-hi")
        self.assertIsNotNone(item.person)
        self.assertEqual(item.wait_days(TODAY), 0)

    def test_queue_is_independent_no_cascade(self):
        cfg = fresh_config()
        plan = self._sample_plan(cfg)
        hi = next(q for q in plan.queue if q.epic.key == "Q-hi")
        lo = next(q for q in plan.queue if q.epic.key == "Q-lo")
        # both need ci-cd; the same earliest person, same date — no cascade
        self.assertEqual(hi.person.name, lo.person.name)
        self.assertEqual(hi.available_on, lo.available_on)

    def test_epic_with_no_team_skill_is_blocked(self):
        cfg = fresh_config()
        plan = self._sample_plan(cfg)
        none = next(q for q in plan.queue if q.epic.key == "Q-none")
        self.assertIsNone(none.person)
        self.assertIn("cobol", none.reason)

    def test_queued_epic_wait_reflects_owner_availability(self):
        # If the only person with the skill is busy, the wait is their free date.
        cfg = fresh_config()
        busy = mk(cfg, "A-1", owners=[("karthi", None)], start="2026-07-01", end="2026-09-15")
        queued = mk(cfg, "Q-1", skills=["artifactory"], priority="High")
        plan = build_plan(cfg, [busy, queued], TODAY)
        item = plan.queue[0]
        self.assertEqual(item.person.name, "Karthi")
        self.assertEqual(item.available_on, date(2026, 9, 15))


class TestEstimate(unittest.TestCase):
    def test_availability_for_ranks_earliest_first(self):
        cfg = fresh_config()
        plan = build_plan(cfg, sources.load_epics(cfg, "csv"), TODAY)
        ranked, _ = availability_for(cfg, plan, {"ci-cd"}, None)
        dates = [free for _, free in ranked]
        self.assertEqual(dates, sorted(dates))
        self.assertTrue(ranked)

    def test_availability_for_reports_nobody_when_skill_absent(self):
        cfg = fresh_config()
        plan = build_plan(cfg, sources.load_epics(cfg, "csv"), TODAY)
        ranked, why = availability_for(cfg, plan, {"cobol"}, None)
        self.assertEqual(ranked, [])
        self.assertTrue(why)


class TestPriority(unittest.TestCase):
    def test_config_override_beats_jira(self):
        cfg = fresh_config()
        cfg.priority_overrides = {"DPE-211": "Highest"}
        e = epic(cfg, "DPE-211")
        self.assertEqual(e.priority, "Highest")
        self.assertEqual(e.jira_priority, "Medium")
        self.assertTrue(e.priority_forced)

    def test_invalid_priority_is_rejected_at_load(self):
        from dpe.config import ConfigError
        text = CONFIG.read_text().replace('"DPE-210" = "High"', '"DPE-210" = "Urgent!!"')
        alt = ROOT / "config" / "_test_priority.toml"
        alt.write_text(text)
        try:
            with self.assertRaises(ConfigError) as ctx:
                load(alt)
            self.assertIn("Urgent!!", str(ctx.exception))
        finally:
            alt.unlink()


class TestViews(unittest.TestCase):
    def test_all_three_views_render_without_error(self):
        from dpe.cli import VIEWS
        cfg = fresh_config()
        plan = build_plan(cfg, sources.load_epics(cfg, "csv"), TODAY)
        for name, (_, render) in VIEWS.items():
            out = render(cfg, plan, TODAY)
            self.assertTrue(out.strip(), f"view {name} came back empty")


class TestClean(unittest.TestCase):
    def test_clean_drops_columns_and_reads_back(self):
        cfg = fresh_config()
        raw_rows = len(sources.fetch_raw(cfg, "csv"))
        _, clean_path, stats = clean_mod.clean(cfg)
        self.assertTrue(clean_path.exists())
        self.assertLess(stats["kept_columns"], stats["raw_columns"])
        self.assertEqual(stats["kept_rows"], raw_rows)
        # an active epic still reads back as scheduled with its owner
        active = next(e for e in sources.load_epics(cfg, "csv") if e.scheduled)
        self.assertTrue(active.owners)


if __name__ == "__main__":
    unittest.main()
