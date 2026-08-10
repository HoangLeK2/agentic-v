from unittest import TestCase
from unittest.mock import AsyncMock, MagicMock, patch

from workforce.learning import promote_learning_candidate, propose_learning_candidate


class LearningPromotionTest(TestCase):
    def test_duplicate_candidate_reuses_existing_id(self) -> None:
        connection = MagicMock()
        statements = [MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(rowcount=0), MagicMock()]
        statements[-1].scalar_one.return_value = "existing-candidate"
        connection.execute.side_effect = statements
        transaction = MagicMock()
        transaction.__enter__.return_value = connection
        engine = MagicMock()
        engine.begin.return_value = transaction

        with patch("workforce.learning.create_engine", return_value=engine):
            result = propose_learning_candidate("engineering", "Always run focused checks before review.", ["check"])

        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["learning_candidate_id"], "existing-candidate")

    def test_promotion_rejects_task_level_or_missing_candidate_pass(self) -> None:
        row = {
            "namespace": "engineering",
            "insight": "Use fixed argv",
            "evidence": ["test-1"],
            "source_run_id": "run-1",
            "verdict": None,
            "promoted_at": None,
        }
        connection = MagicMock()
        connection.execute.return_value.mappings.return_value.first.return_value = row
        transaction = MagicMock()
        transaction.__enter__.return_value = connection
        engine = MagicMock()
        engine.begin.return_value = transaction

        with patch("workforce.learning.create_engine", return_value=engine):
            with self.assertRaisesRegex(ValueError, "candidate-level PASS"):
                import asyncio

                asyncio.run(promote_learning_candidate("candidate-1"))

    def test_candidate_pass_is_promoted_with_candidate_id_metadata(self) -> None:
        row = {
            "namespace": "engineering",
            "insight": "Use fixed argv",
            "evidence": ["test-1"],
            "source_run_id": "run-1",
            "verdict": "PASS",
            "promoted_at": None,
        }
        connection = MagicMock()
        connection.execute.return_value.mappings.return_value.first.return_value = row
        transaction = MagicMock()
        transaction.__enter__.return_value = connection
        engine = MagicMock()
        engine.begin.return_value = transaction
        knowledge = MagicMock()
        knowledge.add_content_async = AsyncMock()

        with (
            patch("workforce.learning.create_engine", return_value=engine),
            patch("workforce.learning.create_knowledge", return_value=knowledge),
        ):
            import asyncio

            result = asyncio.run(promote_learning_candidate("candidate-1"))

        self.assertEqual(result["status"], "promoted")
        self.assertEqual(
            knowledge.add_content_async.await_args.kwargs["metadata"]["learning_candidate_id"], "candidate-1"
        )
