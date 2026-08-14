from unittest import TestCase
from unittest.mock import MagicMock, patch

with patch("db.create_knowledge", return_value=MagicMock()):
    from agents.workforce.common import WorkforceSessionSummaryManager, workforce_session_summary_manager
    from agents.workforce.engineering import engineering_team
    from agents.workforce.growth import growth_team
    from agents.workforce.research import research_team
    from agents.workforce.router import workforce_router


class WorkforceSummaryTest(TestCase):
    def test_summary_manager_accepts_case_drift_from_openai_compatible_model(self) -> None:
        manager = WorkforceSessionSummaryManager()
        response = MagicMock(content='{"Summary":"User asked about Device Farm routing.", "Topics":["Device Farm"]}')

        summary = manager._process_summary_response(response, MagicMock())

        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.summary, "User asked about Device Farm routing.")
        self.assertEqual(summary.topics, ["Device Farm"])

    def test_public_teams_use_custom_session_summary_manager(self) -> None:
        for team in (workforce_router, engineering_team, growth_team, research_team):
            with self.subTest(team=team.id):
                self.assertTrue(team.enable_session_summaries)
                self.assertIsInstance(team.session_summary_manager, WorkforceSessionSummaryManager)
                self.assertTrue(team.add_session_summary_to_context)

    def test_summary_manager_uses_fast_limited_context(self) -> None:
        manager = workforce_session_summary_manager()

        self.assertEqual(manager.last_n_runs, 6)
        self.assertEqual(manager.conversation_limit, 24)
