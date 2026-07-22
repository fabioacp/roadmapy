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

from dpe import sources  # noqa: E402
from dpe.config import load  # noqa: E402
from dpe.jira import JiraError, RawIssue, normalize, normalize_all  # noqa: E402
from dpe.config import Commitment
from dpe.scheduler import (COMMITTED, STRATEGIES, SUGGESTED, UNSTAFFABLE,
                           measure, schedule)  # noqa: E402
from dpe.sources import api_source  # noqa: E402

CONFIG = ROOT / "config" / "config.toml"
TODAY = date(2026, 7, 21)


def fresh_config():
    return load(CONFIG)


class TestSourceParity(unittest.TestCase):
    def test_csv_and_api_produce_the_same_roadmap(self):
        cfg = fresh_config()
        plans = {}
        for source in ("csv", "api"):
            epics = sources.load_epics(cfg, source)
            assignments, _ = schedule(cfg, epics, TODAY)
            plans[source] = sorted(
                (a.epic.key, a.person.name if a.person else None, a.start, a.end)
                for a in assignments
            )
        self.assertEqual(plans["csv"], plans["api"])

    def test_both_sources_filter_out_the_done_epic(self):
        cfg = fresh_config()
        for source in ("csv", "api"):
            keys = {e.key for e in sources.load_epics(cfg, source)}
            self.assertNotIn("DPE-113", keys, f"{source} did not filter out the Done epic")


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
                return {"warningMessages": ["jql ruim"]}

        with self.assertRaises(JiraError) as ctx:
            api_source.fetch(fresh_config(), Broken())
        self.assertIn("issues", str(ctx.exception))

    def test_jql_and_fields_go_in_the_url(self):
        capturado = {}

        class Spy:
            def get(self, url, headers, timeout):
                capturado["url"] = url
                return {"issues": [], "isLast": True}

        cfg = fresh_config()
        api_source.fetch(cfg, Spy())
        self.assertIn("jql=", capturado["url"])
        self.assertIn("customfield_10016", capturado["url"])
        self.assertIn("/rest/api/3/search/jql", capturado["url"])


class TestCredentials(unittest.TestCase):
    def test_http_without_token_fails_before_any_network(self):
        cfg = fresh_config()
        cfg.jira.api.transport = "http"
        cfg.jira.api.token_env = "VARIAVEL_QUE_NAO_EXISTE_DPE"
        with self.assertRaises(JiraError) as ctx:
            api_source.build_transport(cfg)
        self.assertIn("VARIAVEL_QUE_NAO_EXISTE_DPE", str(ctx.exception))

    def test_missing_fixture_gives_a_clear_error(self):
        with self.assertRaises(JiraError) as ctx:
            api_source.StubTransport(Path("/nao/existe.json"), 100)
        self.assertIn("not found", str(ctx.exception))


class TestNormalisation(unittest.TestCase):
    def test_labels_become_skills_and_min_seniority(self):
        cfg = fresh_config()
        epic = normalize(cfg, RawIssue(
            key="T-1", status="To Do", estimate="10",
            labels=["skill:go", "skill:AWS", "min:senior", "tech-debt"],
        ))
        self.assertEqual(epic.skills, {"go", "aws"})
        self.assertEqual(epic.min_seniority, "senior")

    def test_points_become_senior_days(self):
        cfg = fresh_config()  # points_to_days = 1.5
        epic = normalize(cfg, RawIssue(key="T-1", status="To Do", estimate="8"))
        self.assertAlmostEqual(epic.estimate_days, 12.0)

    def test_missing_estimate_uses_default_and_is_flagged(self):
        cfg = fresh_config()
        for valor in (None, "", "0"):
            epic = normalize(cfg, RawIssue(key="T-1", status="To Do", estimate=valor))
            self.assertTrue(epic.estimate_missing, f"estimate={valor!r}")
            self.assertEqual(epic.estimate_days, cfg.jira.default_estimate_days)

    def test_assignee_outside_the_roster_is_ignored(self):
        cfg = fresh_config()
        epic = normalize(cfg, RawIssue(key="T-1", status="To Do", assignee="Someone Outside"))
        self.assertIsNone(epic.assignee)

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


class TestSchedulerHonoursConstraints(unittest.TestCase):
    def test_min_senior_never_lands_on_a_junior(self):
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        assignments, _ = schedule(cfg, epics, TODAY)
        for a in assignments:
            if a.kind != SUGGESTED:
                continue  # a manual assignment may violate on purpose — that is your call
            if a.epic.min_seniority and a.person:
                self.assertGreaterEqual(
                    cfg.seniority_rank(a.person.seniority),
                    cfg.seniority_rank(a.epic.min_seniority),
                    f"{a.epic.key} foi para {a.person.name}",
                )

    def test_nobody_gets_an_epic_without_the_skill(self):
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        assignments, _ = schedule(cfg, epics, TODAY)
        for a in assignments:
            if a.kind == SUGGESTED and a.person:
                self.assertTrue(a.epic.skills <= a.person.skills, a.epic.key)

    def test_nobody_works_during_time_off(self):
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        _, ledgers = schedule(cfg, epics, TODAY)
        for person in cfg.people:
            for day, used in ledgers[person.name].used.items():
                if used > 0:
                    self.assertTrue(person.available_on(day),
                                    f"{person.name} allocated during time off on {day}")
                    self.assertLess(day.weekday(), 5, f"{person.name} allocated on {day} (weekend)")
                    self.assertNotIn(day, cfg.holidays, f"{person.name} allocated on holiday {day}")

    def test_daily_capacity_is_never_exceeded(self):
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        _, ledgers = schedule(cfg, epics, TODAY)
        for person in cfg.people:
            ledger = ledgers[person.name]
            for day, used in ledger.used.items():
                self.assertLessEqual(used, ledger.capacity_on(day) + 1e-9,
                                     f"{person.name} overallocated on {day}")

    def test_epic_with_nobody_eligible_stays_unstaffed(self):
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        assignments, _ = schedule(cfg, epics, TODAY)
        rust = next(a for a in assignments if a.epic.key == "DPE-108")
        self.assertIsNone(rust.person)
        self.assertIn("rust", rust.reason)


class TestManualAssignments(unittest.TestCase):
    """[[assignments]] is fact: the tool computes dates, it does not choose."""

    def test_config_owners_versus_tool_picks(self):
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        assignments, _ = schedule(cfg, epics, TODAY)
        por_epic = {a.epic.key: a for a in assignments}
        for c in cfg.commitments:
            a = por_epic[c.epic]
            self.assertEqual(a.kind, COMMITTED, f"{c.epic} should be a fact")
            self.assertEqual(a.person.name, c.person)
            self.assertEqual(a.fte, c.fte)

    def test_epic_outside_the_config_becomes_a_suggestion(self):
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        assignments, _ = schedule(cfg, epics, TODAY)
        declarados = {c.epic for c in cfg.commitments}
        for a in assignments:
            if a.epic.key not in declarados:
                self.assertIn(a.kind, (SUGGESTED, UNSTAFFABLE), a.epic.key)

    def test_manual_assignment_beats_the_tools_choice(self):
        """Without the commitment, DPE-103 would go to a different person and date."""
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")

        cfg.commitments = [c for c in cfg.commitments if c.epic != "DPE-103"]
        free_pick = {a.epic.key: a for a in schedule(cfg, epics, TODAY)[0]}["DPE-103"]

        cfg.commitments.append(Commitment(epic="DPE-103", person="Carla Nunes", fte=1.0))
        forced = {a.epic.key: a for a in schedule(cfg, epics, TODAY)[0]}["DPE-103"]

        self.assertEqual(forced.person.name, "Carla Nunes")
        self.assertEqual(forced.kind, COMMITTED)
        self.assertNotEqual(free_pick.person.name, "Carla Nunes")

    def test_skill_violating_assignment_is_honoured_but_flagged(self):
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        # Elis (junior, only python/ci-cd) on the epic requiring kubernetes + min:senior
        cfg.commitments = [Commitment(epic="DPE-101", person="Elis Ferreira", fte=1.0)]
        a = {x.epic.key: x for x in schedule(cfg, epics, TODAY)[0]}["DPE-101"]
        self.assertEqual(a.person.name, "Elis Ferreira")   # respeitado
        self.assertTrue(a.warning)                          # mas marcado
        self.assertIn("kubernetes", a.warning)

    def test_partial_fte_leaves_capacity_for_another_epic(self):
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        bruno = cfg.person("Bruno Lima")

        cfg.commitments = [Commitment(epic="DPE-103", person="Bruno Lima", fte=0.5)]
        _, ledgers = schedule(cfg, epics, TODAY)
        half = ledgers["Bruno Lima"]
        # On the first working day he spends half his capacity on the declared epic,
        # leaving the other half for the tool to suggest something else.
        self.assertLessEqual(half.used.get(TODAY, 0), cfg.daily_capacity(bruno) + 1e-9)

    def test_fte_above_100_does_not_exceed_capacity(self):
        """Overcommitment stretches the schedule; it never makes a person exceed 100%."""
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        cfg.commitments = [
            Commitment(epic="DPE-103", person="Bruno Lima", fte=1.0),
            Commitment(epic="DPE-109", person="Bruno Lima", fte=1.0),
            Commitment(epic="DPE-112", person="Bruno Lima", fte=1.0),
        ]
        _, ledgers = schedule(cfg, epics, TODAY)
        ledger = ledgers["Bruno Lima"]
        for day, used in ledger.used.items():
            self.assertLessEqual(used, ledger.capacity_on(day) + 1e-9,
                                 f"Bruno exceeded 100% on {day}")


class TestSuggestions(unittest.TestCase):
    def test_suggestion_carries_sorted_alternatives(self):
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        assignments, _ = schedule(cfg, epics, TODAY)
        for a in assignments:
            if a.kind == SUGGESTED and a.alternatives:
                # a escolhida entrega before de qualquer alternativa
                for alt in a.alternatives:
                    self.assertLessEqual(a.end, alt.end, a.epic.key)

    def test_suggestions_do_not_overlap_on_the_same_person(self):
        """Two suggestions for the same person compete for capacity, they do not clone it."""
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        _, ledgers = schedule(cfg, epics, TODAY)
        for person in cfg.people:
            ledger = ledgers[person.name]
            for day, used in ledger.used.items():
                self.assertLessEqual(used, ledger.capacity_on(day) + 1e-9,
                                     f"{person.name} overallocated on {day}")

    def test_epic_with_no_team_skill_gets_no_suggestion(self):
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        assignments, _ = schedule(cfg, epics, TODAY)
        rust = next(a for a in assignments if a.epic.key == "DPE-108")
        self.assertEqual(rust.kind, UNSTAFFABLE)
        self.assertIsNone(rust.person)


class TestPriority(unittest.TestCase):
    def test_config_override_beats_jira(self):
        cfg = fresh_config()
        cfg.priority_overrides = {"DPE-105": "Highest"}
        epic = next(e for e in sources.load_epics(cfg, "csv") if e.key == "DPE-105")
        self.assertEqual(epic.priority, "Highest")
        self.assertEqual(epic.jira_priority, "Medium")   # the original is kept on record
        self.assertTrue(epic.priority_forced)

    def test_epic_without_override_keeps_jira_priority(self):
        cfg = fresh_config()
        cfg.priority_overrides = {}
        epic = next(e for e in sources.load_epics(cfg, "csv") if e.key == "DPE-105")
        self.assertEqual(epic.priority, "Medium")
        self.assertFalse(epic.priority_forced)

    def test_override_changes_the_scheduling_order(self):
        cfg = fresh_config()
        cfg.priority_overrides = {}
        cfg.commitments = []
        epics = sources.load_epics(cfg, "csv")
        before = {a.epic.key: a.start for a in schedule(cfg, epics, TODAY)[0] if a.start}

        cfg.priority_overrides = {"DPE-112": "Highest"}
        epics = sources.load_epics(cfg, "csv")
        after = {a.epic.key: a.start for a in schedule(cfg, epics, TODAY)[0] if a.start}

        self.assertLess(after["DPE-112"], before["DPE-112"],
                        "raising to Highest should pull the start earlier")

    def test_invalid_priority_is_rejected_at_load(self):
        import tomllib
        from dpe.config import ConfigError, load
        text = CONFIG.read_text().replace('"DPE-114" = "Highest"', '"DPE-114" = "Urgentissimo"')
        alt = ROOT / "config" / "_test_priority.toml"
        alt.write_text(text)
        try:
            with self.assertRaises(ConfigError) as ctx:
                load(alt)
            self.assertIn("Urgentissimo", str(ctx.exception))
        finally:
            alt.unlink()


class TestStrategies(unittest.TestCase):
    def test_all_strategies_schedule_the_same_set(self):
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        sets = []
        for name in STRATEGIES:
            assignments, _ = schedule(cfg, epics, TODAY, strategy=name)
            sets.append({a.epic.key for a in assignments})
        self.assertEqual(len(set(map(frozenset, sets))), 1,
                         "a strategy must not lose or invent an epic")

    def test_quick_wins_delivers_the_first_epic_earlier(self):
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        by_priority = measure(schedule(cfg, epics, TODAY, strategy="priority")[0], TODAY, "priority")
        by_size = measure(schedule(cfg, epics, TODAY, strategy="quick-wins")[0], TODAY, "quick-wins")
        self.assertLessEqual(by_size.first_delivery, by_priority.first_delivery)

    def test_strategy_does_not_alter_committed_work(self):
        """[[assignments]] is fact — the open backlog order must not touch it."""
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        dates = {}
        for name in STRATEGIES:
            assignments, _ = schedule(cfg, epics, TODAY, strategy=name)
            dates[name] = {a.epic.key: (a.person.name, a.start, a.end)
                           for a in assignments if a.committed}
        values = list(dates.values())
        for other in values[1:]:
            self.assertEqual(values[0], other)

    def test_unknown_strategy_fails(self):
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        with self.assertRaises(ValueError):
            schedule(cfg, epics, TODAY, strategy="nao-existe")

    def test_metrics_match_the_assignments(self):
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        assignments, _ = schedule(cfg, epics, TODAY)
        m = measure(assignments, TODAY, "priority")
        staffed = [a for a in assignments if a.staffed]
        self.assertEqual(m.makespan, max(a.end for a in staffed))
        self.assertEqual(m.unstaffed, len(assignments) - len(staffed))
        late = [a for a in staffed if a.epic.due and a.end > a.epic.due]
        self.assertEqual(m.late_epics, len(late))


class TestViews(unittest.TestCase):
    def test_all_three_views_render_without_error(self):
        from dpe.cli import VIEWS
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        assignments, _ = schedule(cfg, epics, TODAY)
        for name, (_, render) in VIEWS.items():
            output = render(cfg, assignments, TODAY)
            self.assertTrue(output.strip(), f"view {name} came back empty")
            self.assertIn("DPE-", output, f"view {name} listed no epics at all")

    def test_quarter_view_covers_every_scheduled_epic(self):
        from dpe.cli import view_quarter
        cfg = fresh_config()
        epics = sources.load_epics(cfg, "csv")
        assignments, _ = schedule(cfg, epics, TODAY)
        output = view_quarter(cfg, assignments, TODAY)
        for a in assignments:
            self.assertIn(a.epic.key, output, f"{a.epic.key} vanished from the quarter view")


if __name__ == "__main__":
    unittest.main()
