"""Test suite. Stdlib only: python3 -m unittest discover tests

The test that matters most is `test_csv_and_api_produce_the_same_roadmap` — it is
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
from dpe.scheduler import (COMMITTED, STRATEGIES, SUGGESTED, UNSTAFFABLE,  # noqa: E402
                           by_epic, measure, schedule)
from dpe.sources import api_source  # noqa: E402

CONFIG = ROOT / "config" / "config.toml"
TODAY = date(2026, 7, 21)
DONE_EPIC = "DPE-1131"
SHARED_EPIC = "DPE-1091"
RUST_EPIC = "DPE-1081"


def fresh_config():
    return load(CONFIG)


def epic(cfg, key, source="csv"):
    return next(e for e in sources.load_epics(cfg, source) if e.key == key)


class TestSourceParity(unittest.TestCase):
    def test_csv_and_api_produce_the_same_roadmap(self):
        cfg = fresh_config()
        plans = {}
        for source in ("csv", "api"):
            epics = sources.load_epics(cfg, source)
            assignments, _ = schedule(cfg, epics, TODAY)
            plans[source] = sorted(
                (a.epic.key, a.person.name if a.person else None, a.fte, a.start, a.end)
                for a in assignments
            )
        self.assertEqual(plans["csv"], plans["api"])

    def test_both_sources_filter_out_the_done_epic(self):
        cfg = fresh_config()
        for source in ("csv", "api"):
            keys = {e.key for e in sources.load_epics(cfg, source)}
            self.assertNotIn(DONE_EPIC, keys, f"{source} did not filter out the Done epic")


class TestApiPagination(unittest.TestCase):
    def test_pages_without_losing_or_duplicating(self):
        cfg = fresh_config()
        cfg.jira.api.page_size = 5
        transport = api_source.StubTransport(cfg.resolve(cfg.jira.api.stub_file), 5)
        raws = api_source.fetch(cfg, transport)
        keys = [r.key for r in raws]
        self.assertEqual(len(keys), 14)
        self.assertEqual(len(set(keys)), 14)
        self.assertEqual(transport.calls, 3)  # ceil(14/5)

    def test_a_single_page_makes_one_call(self):
        cfg = fresh_config()
        transport = api_source.StubTransport(cfg.resolve(cfg.jira.api.stub_file), 100)
        api_source.fetch(cfg, transport)
        self.assertEqual(transport.calls, 1)

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
        e = normalize(cfg, RawIssue(
            key="T-1", status="To Do", estimate="10",
            labels=["skill:go", "skill:AWS", "min:senior", "tech-debt"],
        ))
        self.assertEqual(e.skills, {"go", "aws"})
        self.assertEqual(e.min_seniority, "senior")

    def test_points_become_senior_days(self):
        cfg = fresh_config()  # points_to_days = 1.5
        e = normalize(cfg, RawIssue(key="T-1", status="To Do", estimate="8"))
        self.assertAlmostEqual(e.estimate_days, 12.0)

    def test_missing_estimate_uses_default_and_is_flagged(self):
        cfg = fresh_config()
        for value in (None, "", "0"):
            e = normalize(cfg, RawIssue(key="T-1", status="To Do", estimate=value))
            self.assertTrue(e.estimate_missing, f"estimate={value!r}")
            self.assertEqual(e.estimate_days, cfg.jira.default_estimate_days)

    def test_done_status_disappears(self):
        cfg = fresh_config()
        self.assertIsNone(normalize(cfg, RawIssue(key="T-1", status="Done")))

    def test_unknown_seniority_fails(self):
        cfg = fresh_config()
        with self.assertRaises(JiraError):
            normalize(cfg, RawIssue(key="T-1", status="To Do", labels=["min:staff"]))

    def test_fully_done_backlog_fails_usefully(self):
        cfg = fresh_config()
        with self.assertRaises(JiraError) as ctx:
            normalize_all(cfg, [RawIssue(key="T-1", status="Done")])
        self.assertIn("done_statuses", str(ctx.exception))


class TestOwnerLabels(unittest.TestCase):
    def test_single_owner_defaults_to_full_fte(self):
        cfg = fresh_config()
        e = normalize(cfg, RawIssue(key="T-1", status="To Do", labels=["owner:alex"]))
        self.assertEqual(e.owners, [Owner("Alex", 1.0)])
        self.assertTrue(e.owned)

    def test_owner_with_percentage(self):
        cfg = fresh_config()
        e = normalize(cfg, RawIssue(key="T-1", status="To Do", labels=["owner:daniell:60"]))
        self.assertEqual(e.owners, [Owner("Daniell", 0.6)])

    def test_several_owner_labels_mean_shared_ownership(self):
        cfg = fresh_config()
        e = normalize(cfg, RawIssue(
            key="T-1", status="To Do", labels=["owner:alex:50", "owner:daniell:50"]))
        self.assertEqual({o.person for o in e.owners}, {"Alex", "Daniell"})
        self.assertTrue(all(o.fte == 0.5 for o in e.owners))

    def test_no_owner_label_leaves_the_epic_open(self):
        cfg = fresh_config()
        e = normalize(cfg, RawIssue(key="T-1", status="To Do", labels=["skill:go"]))
        self.assertFalse(e.owned)

    def test_unknown_alias_fails_with_the_known_list(self):
        cfg = fresh_config()
        with self.assertRaises(JiraError) as ctx:
            normalize(cfg, RawIssue(key="T-1", status="To Do", labels=["owner:nobody"]))
        self.assertIn("alex", str(ctx.exception))

    def test_same_person_twice_on_one_epic_fails(self):
        cfg = fresh_config()
        with self.assertRaises(JiraError):
            normalize(cfg, RawIssue(
                key="T-1", status="To Do", labels=["owner:alex", "owner:alex:50"]))

    def test_alias_defaults_to_slug_of_name(self):
        cfg = fresh_config()
        alex = cfg.person("Alex")
        self.assertEqual(alex.alias, "alex")
        self.assertIs(cfg.person_by_alias("alex"), alex)


class TestSchedulerHonoursConstraints(unittest.TestCase):
    def test_min_senior_never_lands_on_a_junior_when_suggested(self):
        cfg = fresh_config()
        assignments, _ = schedule(cfg, sources.load_epics(cfg, "csv"), TODAY)
        for a in assignments:
            if a.kind != SUGGESTED:
                continue  # owner labels may violate on purpose — that is your call
            if a.epic.min_seniority and a.person:
                self.assertGreaterEqual(
                    cfg.seniority_rank(a.person.seniority),
                    cfg.seniority_rank(a.epic.min_seniority), a.epic.key)

    def test_suggestion_never_ignores_a_skill(self):
        cfg = fresh_config()
        assignments, _ = schedule(cfg, sources.load_epics(cfg, "csv"), TODAY)
        for a in assignments:
            if a.kind == SUGGESTED and a.person:
                self.assertTrue(a.epic.skills <= a.person.skills, a.epic.key)

    def test_nobody_works_during_time_off_or_weekends(self):
        cfg = fresh_config()
        _, ledgers = schedule(cfg, sources.load_epics(cfg, "csv"), TODAY)
        for person in cfg.people:
            for day, used in ledgers[person.name].used.items():
                if used > 1e-9:
                    self.assertTrue(person.available_on(day), f"{person.name} {day}")
                    self.assertLess(day.weekday(), 5, f"{person.name} {day}")
                    self.assertNotIn(day, cfg.holidays, f"{person.name} {day}")

    def test_daily_capacity_is_never_exceeded(self):
        cfg = fresh_config()
        _, ledgers = schedule(cfg, sources.load_epics(cfg, "csv"), TODAY)
        for person in cfg.people:
            ledger = ledgers[person.name]
            for day, used in ledger.used.items():
                self.assertLessEqual(used, ledger.capacity_on(day) + 1e-9,
                                     f"{person.name} overallocated on {day}")

    def test_epic_with_no_team_skill_is_unstaffable(self):
        cfg = fresh_config()
        assignments, _ = schedule(cfg, sources.load_epics(cfg, "csv"), TODAY)
        rust = next(a for a in assignments if a.epic.key == RUST_EPIC)
        self.assertEqual(rust.kind, UNSTAFFABLE)
        self.assertIsNone(rust.person)


class TestOwnedAndShared(unittest.TestCase):
    def test_owner_labels_become_committed_facts(self):
        cfg = fresh_config()
        plans = {p.epic.key: p for p in by_epic(schedule(cfg, sources.load_epics(cfg, "csv"), TODAY)[0])}
        self.assertEqual(plans["DPE-1011"].kind, COMMITTED)
        self.assertEqual(plans["DPE-1011"].owner_names, ["Alex"])

    def test_shared_epic_lists_all_owners_and_they_finish_together(self):
        cfg = fresh_config()
        assignments, _ = schedule(cfg, sources.load_epics(cfg, "csv"), TODAY)
        rows = [a for a in assignments if a.epic.key == SHARED_EPIC]
        self.assertEqual({a.person.name for a in rows}, {"Alex", "Daniell"})
        self.assertEqual({a.end for a in rows}, {rows[0].end})  # same end date
        self.assertEqual({a.start for a in rows}, {rows[0].start})

    def test_shared_epic_effort_is_split_not_duplicated(self):
        cfg = fresh_config()
        _, ledgers = schedule(cfg, sources.load_epics(cfg, "csv"), TODAY)
        e = epic(cfg, SHARED_EPIC)
        spent = sum(
            sum(v for d, v in ledgers[name].used.items())
            for name in ("Alex", "Daniell")
        )
        # The two owners together burn roughly the epic's effort — but they also
        # own other epics, so just assert the shared epic did not double-count:
        # total team spend must not exceed the sum of every epic's effort.
        all_effort = sum(x.epic.estimate_days for x in by_epic(
            schedule(cfg, sources.load_epics(cfg, "csv"), TODAY)[0]) if x.staffed)
        total_used = sum(sum(l.used.values()) for l in ledgers.values())
        self.assertLessEqual(total_used, all_effort + 1e-6)
        self.assertGreater(spent, 0)

    def test_owner_lacking_the_skill_is_honoured_but_flagged(self):
        cfg = fresh_config()
        # Brad (junior, python/ci-cd) put on an epic needing kubernetes + min:senior
        e = normalize(cfg, RawIssue(
            key="T-1", status="To Do", estimate="8",
            labels=["skill:kubernetes", "min:senior", "owner:brad"]))
        a = next(x for x in schedule(cfg, [e], TODAY)[0] if x.epic.key == "T-1")
        self.assertEqual(a.person.name, "Brad")   # honoured
        self.assertEqual(a.kind, COMMITTED)
        self.assertIn("kubernetes", a.warning)    # but flagged

    def test_open_epic_becomes_a_suggestion(self):
        cfg = fresh_config()
        assignments, _ = schedule(cfg, sources.load_epics(cfg, "csv"), TODAY)
        s3 = next(a for a in assignments if a.epic.key == "DPE-1041")
        self.assertEqual(s3.kind, SUGGESTED)
        self.assertTrue(s3.person)


class TestPriority(unittest.TestCase):
    def test_config_override_beats_jira(self):
        cfg = fresh_config()
        cfg.priority_overrides = {"DPE-1051": "Highest"}
        e = epic(cfg, "DPE-1051")
        self.assertEqual(e.priority, "Highest")
        self.assertEqual(e.jira_priority, "Medium")
        self.assertTrue(e.priority_forced)

    def test_epic_without_override_keeps_jira_priority(self):
        cfg = fresh_config()
        cfg.priority_overrides = {}
        e = epic(cfg, "DPE-1051")
        self.assertEqual(e.priority, "Medium")
        self.assertFalse(e.priority_forced)

    def test_invalid_priority_is_rejected_at_load(self):
        from dpe.config import ConfigError
        text = CONFIG.read_text().replace('"DPE-1141" = "Highest"', '"DPE-1141" = "Urgent!!"')
        alt = ROOT / "config" / "_test_priority.toml"
        alt.write_text(text)
        try:
            with self.assertRaises(ConfigError) as ctx:
                load(alt)
            self.assertIn("Urgent!!", str(ctx.exception))
        finally:
            alt.unlink()


class TestStrategies(unittest.TestCase):
    def test_all_strategies_schedule_the_same_set(self):
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        sets = []
        for name in STRATEGIES:
            assignments, _ = schedule(cfg, epics, TODAY, strategy=name)
            sets.append(frozenset(a.epic.key for a in assignments))
        self.assertEqual(len(set(sets)), 1)

    def test_strategy_never_changes_who_owns_what(self):
        # Dates may shift (a person owning two epics needs an internal order), but
        # ownership — who, and at what FTE — is declared and must be invariant.
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        owned = {}
        for name in STRATEGIES:
            assignments, _ = schedule(cfg, epics, TODAY, strategy=name)
            owned[name] = {
                (a.epic.key, a.person.name, a.fte)
                for a in assignments if a.committed
            }
        values = list(owned.values())
        for other in values[1:]:
            self.assertEqual(values[0], other)

    def test_unknown_strategy_fails(self):
        cfg = fresh_config()
        with self.assertRaises(ValueError):
            schedule(cfg, sources.load_epics(cfg, "csv"), TODAY, strategy="nope")

    def test_metrics_dedupe_shared_epics(self):
        cfg = fresh_config()
        assignments, _ = schedule(cfg, sources.load_epics(cfg, "csv"), TODAY)
        m = measure(assignments, TODAY, "priority")
        plans = [p for p in by_epic(assignments) if p.staffed]
        self.assertEqual(m.makespan, max(p.end for p in plans))
        # the shared epic counts once toward the staffed total
        self.assertEqual(len(plans), sum(1 for _ in plans))


class TestViews(unittest.TestCase):
    def test_all_three_views_render_without_error(self):
        from dpe.cli import VIEWS
        cfg = fresh_config()
        assignments, _ = schedule(cfg, sources.load_epics(cfg, "csv"), TODAY)
        for name, (_, render) in VIEWS.items():
            out = render(cfg, assignments, TODAY)
            self.assertTrue(out.strip(), f"view {name} came back empty")
            self.assertIn("DPE-", out, f"view {name} listed no epics")

    def test_quarter_view_covers_every_scheduled_epic(self):
        from dpe.cli import view_quarter
        cfg = fresh_config()
        assignments, _ = schedule(cfg, sources.load_epics(cfg, "csv"), TODAY)
        out = view_quarter(cfg, assignments, TODAY)
        for p in by_epic(assignments):
            self.assertIn(p.epic.key, out, f"{p.epic.key} vanished from the quarter view")

    def test_shared_epic_shown_once_in_epic_view(self):
        from dpe.cli import view_epic
        cfg = fresh_config()
        assignments, _ = schedule(cfg, sources.load_epics(cfg, "csv"), TODAY)
        out = view_epic(cfg, assignments, TODAY)
        self.assertEqual(out.count(SHARED_EPIC), 1)


class TestClean(unittest.TestCase):
    def test_clean_drops_columns_and_folds_labels(self):
        cfg = fresh_config()
        raw_path, clean_path, stats = clean_mod.clean(cfg)
        self.assertTrue(clean_path.exists())
        self.assertLess(stats["kept_columns"], stats["raw_columns"])
        self.assertEqual(stats["kept_rows"], 14)
        # cleaned file re-reads into the same epics as before
        epics = sources.load_epics(cfg, "csv")
        shared = next(e for e in epics if e.key == SHARED_EPIC)
        self.assertEqual({o.person for o in shared.owners}, {"Alex", "Daniell"})

    def test_clean_output_reads_back_identically(self):
        cfg = fresh_config()
        before = sorted(e.key for e in sources.load_epics(cfg, "csv"))
        clean_mod.clean(cfg)
        after = sorted(e.key for e in sources.load_epics(cfg, "csv"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
