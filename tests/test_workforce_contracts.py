from unittest import TestCase
from unittest.mock import MagicMock, patch

with patch("db.create_knowledge", return_value=MagicMock()):
    from agents.workforce.engineering import engineering_team
    from agents.workforce.growth import growth_team
    from agents.workforce.research import research_team
    from agents.workforce.router import workforce_router
from workforce.contracts import (
    EngineeringIntent,
    EngineeringTaskInput,
    OutcomeStatus,
    ResearchTaskInput,
    WorkforceOutcome,
)


class WorkforceContractsTest(TestCase):
    def test_audit_defaults_to_autonomous_remediation(self) -> None:
        task = EngineeringTaskInput(repo_id="agentos", task="Review authentication", intent=EngineeringIntent.AUDIT)

        self.assertTrue(task.apply_fixes)
        self.assertEqual(task.execution_mode, "standard")
        self.assertEqual(task.max_fix_loops, 2)

    def test_execution_modes_tune_default_budgets(self) -> None:
        fast_engineering = EngineeringTaskInput(repo_id="agentos", task="Tiny fix", execution_mode="fast")
        deep_engineering = EngineeringTaskInput(repo_id="agentos", task="Large feature", execution_mode="deep")
        fast_research = ResearchTaskInput(question="Quick lookup")
        deep_research = ResearchTaskInput(question="Deep comparison", execution_mode="deep")

        self.assertEqual(fast_engineering.max_fix_loops, 1)
        self.assertEqual(deep_engineering.max_fix_loops, 4)
        self.assertEqual(fast_research.max_search_rounds, 1)
        self.assertEqual(fast_research.max_sources, 3)
        self.assertEqual(deep_research.max_search_rounds, 3)
        self.assertEqual(deep_research.max_sources, 8)

    def test_audit_can_be_explicitly_read_only(self) -> None:
        task = EngineeringTaskInput(
            repo_id="agentos",
            task="Audit and fix authentication",
            intent=EngineeringIntent.AUDIT,
            apply_fixes=False,
        )

        self.assertFalse(task.apply_fixes)

    def test_outcome_preserves_degraded_operations(self) -> None:
        outcome = WorkforceOutcome(
            status=OutcomeStatus.COMPLETED,
            summary="Completed using public data only",
            degraded_capabilities=("seo.keyword_research",),
        )

        self.assertEqual(outcome.status, OutcomeStatus.COMPLETED)
        self.assertEqual(outcome.degraded_capabilities, ("seo.keyword_research",))

    def test_only_public_router_normalizes_domain_artifacts(self) -> None:
        self.assertIs(workforce_router.output_schema, WorkforceOutcome)
        for team in (engineering_team, growth_team, research_team):
            self.assertIsNone(team.output_schema)

    def test_router_retrieves_promoted_global_learning(self) -> None:
        self.assertIsNotNone(workforce_router.learning)
        self.assertEqual(getattr(workforce_router.learning, "namespace", None), "global")

    def test_public_workforce_teams_use_session_summaries(self) -> None:
        for team in (workforce_router, engineering_team, growth_team, research_team):
            with self.subTest(team=team.id):
                self.assertTrue(team.enable_session_summaries)
                self.assertTrue(team.add_session_summary_to_context)

    def test_internal_repository_questions_are_routed_to_engineering_evidence(self) -> None:
        self.assertIsInstance(workforce_router.instructions, list)
        self.assertIsInstance(engineering_team.instructions, list)
        assert isinstance(workforce_router.instructions, list)
        assert isinstance(engineering_team.instructions, list)
        router_prompt = "\n".join(workforce_router.instructions)
        engineering_prompt = "\n".join(engineering_team.instructions)

        self.assertIn("list the registered repositories", router_prompt)
        self.assertIn("delegate to Engineering", router_prompt)
        self.assertIn("repository file or code evidence", router_prompt)
        self.assertIn("code.sandbox_write", router_prompt)
        self.assertIn("run_engineering_delivery", router_prompt)
        self.assertIn("intent='implement'", router_prompt)
        self.assertIn("execution_mode='standard'", router_prompt)
        self.assertIn("run_engineering_delivery", engineering_prompt)
        self.assertIn("intent=implement", engineering_prompt)
        self.assertIn("execution_mode='standard'", engineering_prompt)
        self.assertIn("set apply_fixes=true", engineering_prompt)
        self.assertIn("repository evidence as authoritative", engineering_prompt)
        self.assertIn("Do not require a nonexistent engineering.* operation", engineering_prompt)
        self.assertIn("Never hand the user code-editing", engineering_prompt)
        self.assertIn("remediate, test, independently review, and publish", router_prompt)
