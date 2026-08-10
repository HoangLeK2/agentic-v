from unittest import TestCase
from unittest.mock import MagicMock, patch

with patch("db.create_knowledge", return_value=MagicMock()):
    from agents.workforce.engineering import engineering_team
    from agents.workforce.growth import growth_team
    from agents.workforce.prompt_provenance import AGENT_PROMPT_SOURCES, SOURCES, grounded_instructions
    from agents.workforce.quality import quality_council
    from agents.workforce.research import research_team
    from agents.workforce.router import workforce_router


class PromptProvenanceTest(TestCase):
    def test_every_workforce_component_has_researched_sources(self) -> None:
        specialist_teams = (engineering_team, growth_team, research_team, quality_council)
        component_ids = {workforce_router.id, *(team.id for team in specialist_teams)}
        for team in specialist_teams:
            component_ids.update(member.id for member in team.members)

        self.assertEqual(component_ids - AGENT_PROMPT_SOURCES.keys(), set())

    def test_sources_are_versioned_primary_repositories(self) -> None:
        for source in SOURCES.values():
            self.assertTrue(source.url.startswith("https://github.com/"))
            self.assertEqual(len(source.revision), 40)
            self.assertTrue(source.adopted_principles)

    def test_unresearched_prompt_fails_component_construction(self) -> None:
        with self.assertRaisesRegex(ValueError, "Prompt provenance is required"):
            grounded_instructions("invented-agent", "Do something")

    def test_runtime_prompt_contains_evidence_and_permission_contract(self) -> None:
        prompt = grounded_instructions("code-agent", "Implement the smallest patch.")

        for source_id in AGENT_PROMPT_SOURCES["code-agent"]:
            for principle in SOURCES[source_id].adopted_principles:
                self.assertIn(principle, prompt)
        self.assertIn("unverified data", prompt)
        self.assertIn("tool output or repository evidence", prompt)
        self.assertIn("tools and permissions assigned", prompt)

    def test_engineering_lead_inherits_researched_write_approval_policy(self) -> None:
        self.assertIn("agno-infra", AGENT_PROMPT_SOURCES["engineering-lead"])
        prompt = grounded_instructions("engineering-lead", "Inspect first and request approval before writes.")

        self.assertIn("require approval for writes", prompt)
        self.assertIn("route nested continuation through the owning team", prompt)
