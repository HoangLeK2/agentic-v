import json
import tempfile
from datetime import UTC, datetime, timedelta
from os import environ
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from workflows.deployment_check import _check_model_probe_report
from workforce.model_probe import probe_model


class ModelProbeTest(TestCase):
    def _deployment_result(self, *, generated_at: datetime, expected_context: int):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            report.write_text(
                json.dumps(
                    {
                        "generated_at": generated_at.isoformat(),
                        "base_url": "https://api.openai.com/v1",
                        "expected_context": expected_context,
                        "models": [],
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                environ,
                {
                    "WORKFORCE_MODEL_PROBE_REPORT": str(report),
                    "WORKFORCE_MODEL_EXPECTED_CONTEXT": "128000",
                    "OPENAI_BASE_URL": "https://api.openai.com/v1",
                },
            ):
                return _check_model_probe_report()

    def test_deployment_gate_rejects_mismatched_context_requirement(self) -> None:
        result = self._deployment_result(generated_at=datetime.now(UTC), expected_context=4096)

        self.assertEqual(result.status, "FAIL")
        self.assertIn("does not match", result.detail)

    def test_deployment_gate_rejects_future_dated_report(self) -> None:
        result = self._deployment_result(
            generated_at=datetime.now(UTC) + timedelta(hours=1),
            expected_context=128000,
        )

        self.assertEqual(result.status, "FAIL")
        self.assertIn("future", result.detail)

    def test_missing_reported_context_fails_capability_probe(self) -> None:
        client = MagicMock()
        response = MagicMock()
        response.json.return_value = {"choices": [{"message": {"content": '{"ok": true}', "tool_calls": [{}]}}]}
        client.post.return_value = response
        stream = MagicMock()
        stream.__enter__.return_value.iter_lines.return_value = iter(["data: {}"])
        client.stream.return_value = stream

        with patch("workforce.model_probe.getenv", side_effect=lambda name, default=None: default):
            result = probe_model(client, "http://models/v1", {"id": "model-1"}, 60)

        self.assertFalse(result.expected_context)
        self.assertFalse(result.passed)
        self.assertIn("expected_context:not_reported", result.detail)

    def test_listed_model_still_fails_when_chat_endpoint_is_unreachable(self) -> None:
        client = MagicMock()
        client.post.side_effect = RuntimeError("unreachable")
        client.stream.side_effect = RuntimeError("unreachable")

        with patch("workforce.model_probe.getenv", side_effect=lambda name, default=None: default):
            result = probe_model(client, "http://models/v1", {"id": "model-1", "context_length": 128000}, 60)

        self.assertFalse(result.reachable)
        self.assertFalse(result.passed)
        self.assertIn("reachable:RuntimeError", result.detail)
