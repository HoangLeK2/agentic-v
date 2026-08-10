from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from agno.workflow.step import StepInput

with patch("db.create_knowledge", return_value=MagicMock()):
    from workflows.continuous_learning import (
        LearningProposal,
        LearningProposalBatch,
        LearningReview,
        continuous_learning_step,
        learning_extractor,
        learning_reviewer,
    )


class ContinuousLearningTest(IsolatedAsyncioTestCase):
    async def test_passed_candidate_is_promoted(self) -> None:
        run = {"team_id": "engineering-team", "run_id": "run-1", "input": "fix", "content": "checks pass"}
        extraction = MagicMock(content=LearningProposalBatch(proposals=(LearningProposal(insight="Run focused checks before independent review.", evidence=("checks pass",)),)))
        review = MagicMock(content=LearningReview(verdict="PASS", rationale="Specific and supported by check evidence."))

        with (
            patch("workflows.continuous_learning._latest_runs", return_value=[run]),
            patch.object(learning_extractor, "arun", new=AsyncMock(return_value=extraction)),
            patch.object(learning_reviewer, "arun", new=AsyncMock(return_value=review)),
            patch("workflows.continuous_learning.propose_learning_candidate", return_value={"learning_candidate_id": "candidate-1", "status": "pending"}),
            patch("workflows.continuous_learning.evaluate_learning_candidate") as evaluate,
            patch("workflows.continuous_learning.promote_learning_candidate", new=AsyncMock()) as promote,
        ):
            output = await continuous_learning_step(StepInput(input="run"))

        self.assertTrue(output.success)
        self.assertEqual(output.content["promoted"], 1)
        evaluate.assert_called_once_with("candidate-1", "PASS", "Specific and supported by check evidence.")
        promote.assert_awaited_once_with("candidate-1")

    async def test_failed_candidate_is_not_promoted(self) -> None:
        run = {"team_id": "research-team", "run_id": "run-2", "input": "research", "content": "one result"}
        extraction = MagicMock(content=LearningProposalBatch(proposals=(LearningProposal(insight="Treat one search result as universally authoritative.", evidence=("one result",)),)))
        review = MagicMock(content=LearningReview(verdict="FAIL", rationale="A single result cannot support this principle."))

        with (
            patch("workflows.continuous_learning._latest_runs", return_value=[run]),
            patch.object(learning_extractor, "arun", new=AsyncMock(return_value=extraction)),
            patch.object(learning_reviewer, "arun", new=AsyncMock(return_value=review)),
            patch("workflows.continuous_learning.propose_learning_candidate", return_value={"learning_candidate_id": "candidate-2", "status": "pending"}),
            patch("workflows.continuous_learning.evaluate_learning_candidate") as evaluate,
            patch("workflows.continuous_learning.promote_learning_candidate", new=AsyncMock()) as promote,
        ):
            output = await continuous_learning_step(StepInput(input="run"))

        self.assertTrue(output.success)
        self.assertEqual(output.content["rejected"], 1)
        evaluate.assert_called_once()
        promote.assert_not_awaited()

    async def test_markdown_extraction_fallback_is_fail_closed_and_parseable(self) -> None:
        from workflows.continuous_learning import _proposal_batch

        result = MagicMock(content="not-json")
        result.get_content_as_string.return_value = (
            "1. Run focused checks before independent review.\n"
            "Evidence: The focused authentication check passed before reviewer approval."
        )

        batch = _proposal_batch(result)

        self.assertEqual(len(batch.proposals), 1)
        self.assertIn("focused checks", batch.proposals[0].insight)

    async def test_backend_principle_alias_is_parseable(self) -> None:
        from workflows.continuous_learning import _proposal_batch

        result = MagicMock(content="not-schema")
        result.get_content_as_string.return_value = (
            '{"principles":[{"principle":"Require independent checks before declaring completion.",'
            '"evidence":["The check suite passed before approval."]}]}'
        )

        batch = _proposal_batch(result)

        self.assertEqual(batch.proposals[0].evidence, ("The check suite passed before approval.",))

    async def test_bare_reviewer_verdict_gets_auditable_rationale(self) -> None:
        from workflows.continuous_learning import _learning_review

        result = MagicMock(content="not-schema")
        result.get_content_as_string.return_value = "PASS"

        review = _learning_review(result)

        self.assertEqual(review.verdict, "PASS")
        self.assertIn("without additional rationale", review.rationale)
