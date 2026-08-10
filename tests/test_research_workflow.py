from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from agno.workflow.step import StepInput

with patch("db.create_knowledge", return_value=MagicMock()):
    from agents.workforce.research import search_agent, source_verifier, synthesis_agent, web_research_agent
    from workflows.research_pipeline import research_pipeline_step
from workforce.capabilities import CapabilityEvaluation, CapabilityStatus
from workforce.contracts import OutcomeStatus


class ResearchWorkflowTest(IsolatedAsyncioTestCase):
    async def test_verifier_blocks_unsupported_research(self) -> None:
        capability = CapabilityEvaluation(CapabilityStatus.AVAILABLE, ("research.web_search",), (), ())

        async def ask(agent, _prompt):
            if agent is search_agent:
                return "https://primary.example/source"
            if agent is web_research_agent:
                return "Claim without enough primary evidence"
            if agent is source_verifier:
                return "VERDICT: INSUFFICIENT_EVIDENCE"
            if agent is synthesis_agent:
                return "The available evidence is insufficient."
            raise AssertionError(f"unexpected agent: {agent.id}")

        with (
            patch("workflows.research_pipeline.capability_registry.evaluate", return_value=capability),
            patch("workflows.research_pipeline._ask", new=AsyncMock(side_effect=ask)) as ask_mock,
        ):
            output = await research_pipeline_step(StepInput(input={"question": "Is the claim true?"}))

        self.assertFalse(output.success)
        assert isinstance(output.content, dict)
        self.assertEqual(output.content["status"], OutcomeStatus.INSUFFICIENT_EVIDENCE)
        self.assertEqual(
            [call.args[0] for call in ask_mock.await_args_list],
            [search_agent, web_research_agent, source_verifier, synthesis_agent],
        )

    async def test_unavailable_search_fails_before_any_model_call(self) -> None:
        capability = CapabilityEvaluation(
            CapabilityStatus.UNAVAILABLE,
            (),
            (),
            ("research.web_search", "research.deep_fetch"),
        )

        with (
            patch("workflows.research_pipeline.capability_registry.evaluate", return_value=capability),
            patch("workflows.research_pipeline._ask", new=AsyncMock()) as ask_mock,
        ):
            output = await research_pipeline_step(StepInput(input={"question": "Research this"}))

        self.assertFalse(output.success)
        assert isinstance(output.content, dict)
        self.assertEqual(output.content["status"], OutcomeStatus.CAPABILITY_UNAVAILABLE)
        ask_mock.assert_not_awaited()

    async def test_malformed_verifier_output_fails_closed(self) -> None:
        capability = CapabilityEvaluation(CapabilityStatus.AVAILABLE, ("research.web_search",), (), ())

        async def ask(agent, _prompt):
            if agent is search_agent:
                return "https://primary.example/source"
            if agent is web_research_agent:
                return "Evidence"
            if agent is source_verifier:
                return "Looks plausible but no structured verdict"
            if agent is synthesis_agent:
                return "Insufficient evidence"
            raise AssertionError(f"unexpected agent: {agent.id}")

        with (
            patch("workflows.research_pipeline.capability_registry.evaluate", return_value=capability),
            patch("workflows.research_pipeline._ask", new=AsyncMock(side_effect=ask)),
        ):
            output = await research_pipeline_step(StepInput(input={"question": "Research this"}))

        self.assertFalse(output.success)
        assert isinstance(output.content, dict)
        self.assertEqual(output.content["status"], OutcomeStatus.INSUFFICIENT_EVIDENCE)

    async def test_conflicting_verifier_verdicts_fail_closed(self) -> None:
        capability = CapabilityEvaluation(CapabilityStatus.AVAILABLE, ("research.web_search",), (), ())

        async def ask(agent, _prompt):
            if agent is search_agent:
                return "https://primary.example/source"
            if agent is web_research_agent:
                return "Evidence"
            if agent is source_verifier:
                return "VERDICT: PASS\nVERDICT: INSUFFICIENT_EVIDENCE"
            if agent is synthesis_agent:
                return "Insufficient evidence"
            raise AssertionError(f"unexpected agent: {agent.id}")

        with (
            patch("workflows.research_pipeline.capability_registry.evaluate", return_value=capability),
            patch("workflows.research_pipeline._ask", new=AsyncMock(side_effect=ask)),
        ):
            output = await research_pipeline_step(StepInput(input={"question": "Research this"}))

        self.assertFalse(output.success)
        assert isinstance(output.content, dict)
        self.assertEqual(output.content["status"], OutcomeStatus.INSUFFICIENT_EVIDENCE)

    async def test_strict_quality_revises_and_rechecks_draft(self) -> None:
        capability = CapabilityEvaluation(CapabilityStatus.AVAILABLE, ("research.web_search",), (), ())
        synthesis_calls = 0

        async def ask(agent, _prompt):
            nonlocal synthesis_calls
            if agent is search_agent:
                return "https://primary.example/source"
            if agent is web_research_agent:
                return "Verified evidence"
            if agent is source_verifier:
                return "VERDICT: PASS"
            if agent is synthesis_agent:
                synthesis_calls += 1
                return "Revised draft" if synthesis_calls == 2 else "Initial draft"
            raise AssertionError(f"unexpected agent: {agent.id}")

        council = AsyncMock(side_effect=["Missing limitation\nVERDICT: FIX_REQUIRED", "VERDICT: PASS"])
        with (
            patch("workflows.research_pipeline.capability_registry.evaluate", return_value=capability),
            patch("workflows.research_pipeline._ask", new=AsyncMock(side_effect=ask)),
            patch("workflows.research_pipeline.review_with_quality_council", new=council),
        ):
            output = await research_pipeline_step(StepInput(input={"question": "Research this", "quality": "strict"}))

        self.assertTrue(output.success)
        assert isinstance(output.content, dict)
        self.assertEqual(output.content["status"], OutcomeStatus.COMPLETED)
        self.assertEqual(output.content["summary"], "Revised draft")
        self.assertEqual(council.await_count, 2)
