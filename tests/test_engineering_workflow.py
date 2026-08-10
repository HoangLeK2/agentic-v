from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from agno.workflow.step import StepInput

with patch("db.create_knowledge", return_value=MagicMock()):
    from workflows.engineering_delivery import (
        code_agent,
        engineering_delivery_step,
        fixer_agent,
        reviewer_agent,
        security_agent,
        tester_agent,
    )
from workforce.capabilities import CapabilityEvaluation, CapabilityStatus
from workforce.contracts import OutcomeStatus


class FakeExecutorClient:
    closed: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def create_workspace(self, _repo_id: str) -> str:
        return "workspace-1"

    async def list_checks(self, _workspace_id: str) -> tuple[str, ...]:
        return ("unit",)

    async def run_check(self, _workspace_id: str, check_id: str) -> dict:
        return {"check_id": check_id, "success": True, "stdout": "ok", "stderr": ""}

    async def git_diff(self, _workspace_id: str) -> str:
        return "diff --git a/a.py b/a.py\n"

    async def close_workspace(self, workspace_id: str) -> None:
        self.closed.append(workspace_id)


class EngineeringWorkflowTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        FakeExecutorClient.closed.clear()

    async def test_implement_uses_code_then_independent_test_and_review(self) -> None:
        capability = CapabilityEvaluation(CapabilityStatus.AVAILABLE, ("code.read",), (), ())

        async def ask(agent, _prompt):
            if agent is code_agent:
                return "implemented"
            if agent is tester_agent:
                return "tests pass"
            if agent is reviewer_agent:
                return "VERDICT: PASS"
            raise AssertionError(f"unexpected agent: {agent.id}")

        with (
            patch("workflows.engineering_delivery.capability_registry.evaluate", return_value=capability),
            patch("workflows.engineering_delivery.WorkspaceExecutorClient", FakeExecutorClient),
            patch("workflows.engineering_delivery._ask", new=AsyncMock(side_effect=ask)) as ask_mock,
        ):
            output = await engineering_delivery_step(
                StepInput(input={"repo_id": "agentos", "task": "Implement parser", "intent": "implement"})
            )

        self.assertTrue(output.success)
        assert isinstance(output.content, dict)
        self.assertEqual(output.content["status"], OutcomeStatus.COMPLETED)
        self.assertEqual(
            [call.args[0] for call in ask_mock.await_args_list], [code_agent, tester_agent, reviewer_agent]
        )
        self.assertEqual(FakeExecutorClient.closed, ["workspace-1"])

    async def test_read_only_audit_skips_code_and_fixer(self) -> None:
        capability = CapabilityEvaluation(CapabilityStatus.AVAILABLE, ("code.read",), (), ())

        async def ask(agent, _prompt):
            if agent is tester_agent:
                return "baseline pass"
            if agent is reviewer_agent:
                return "VERDICT: PASS"
            raise AssertionError(f"unexpected agent: {agent.id}")

        with (
            patch("workflows.engineering_delivery.capability_registry.evaluate", return_value=capability),
            patch("workflows.engineering_delivery.WorkspaceExecutorClient", FakeExecutorClient),
            patch("workflows.engineering_delivery._ask", new=AsyncMock(side_effect=ask)) as ask_mock,
        ):
            output = await engineering_delivery_step(
                StepInput(input={"repo_id": "agentos", "task": "Audit parser", "intent": "audit"})
            )

        called_agents = [call.args[0] for call in ask_mock.await_args_list]
        self.assertTrue(output.success)
        self.assertIn(tester_agent, called_agents)
        self.assertIn(reviewer_agent, called_agents)
        self.assertNotIn(code_agent, called_agents)
        self.assertNotIn(fixer_agent, called_agents)

    async def test_empty_reviewer_verdict_fails_closed(self) -> None:
        capability = CapabilityEvaluation(CapabilityStatus.AVAILABLE, ("code.read",), (), ())

        async def ask(agent, _prompt):
            if agent is tester_agent:
                return "tests pass"
            if agent is reviewer_agent:
                return ""
            if agent is code_agent:
                return "implemented"
            if agent is fixer_agent:
                return "attempted fix"
            raise AssertionError(f"unexpected agent: {agent.id}")

        with (
            patch("workflows.engineering_delivery.capability_registry.evaluate", return_value=capability),
            patch("workflows.engineering_delivery.WorkspaceExecutorClient", FakeExecutorClient),
            patch("workflows.engineering_delivery._ask", new=AsyncMock(side_effect=ask)),
        ):
            output = await engineering_delivery_step(
                StepInput(input={"repo_id": "agentos", "task": "Implement parser", "intent": "implement"})
            )

        self.assertFalse(output.success)
        assert isinstance(output.content, dict)
        self.assertEqual(output.content["status"], OutcomeStatus.BLOCKED)

    async def test_conflicting_reviewer_verdicts_fail_closed(self) -> None:
        capability = CapabilityEvaluation(CapabilityStatus.AVAILABLE, ("code.read",), (), ())

        async def ask(agent, _prompt):
            if agent is code_agent:
                return "implemented"
            if agent is tester_agent:
                return "tests pass"
            if agent is reviewer_agent:
                return "VERDICT: PASS\nMore review\nVERDICT: FIX_REQUIRED"
            if agent is fixer_agent:
                return "attempted fix"
            raise AssertionError(f"unexpected agent: {agent.id}")

        with (
            patch("workflows.engineering_delivery.capability_registry.evaluate", return_value=capability),
            patch("workflows.engineering_delivery.WorkspaceExecutorClient", FakeExecutorClient),
            patch("workflows.engineering_delivery._ask", new=AsyncMock(side_effect=ask)),
        ):
            output = await engineering_delivery_step(
                StepInput(input={"repo_id": "agentos", "task": "Implement parser", "intent": "implement"})
            )

        self.assertFalse(output.success)
        assert isinstance(output.content, dict)
        self.assertEqual(output.content["status"], OutcomeStatus.BLOCKED)

    async def test_read_only_audit_blocks_when_strict_council_requires_fix(self) -> None:
        capability = CapabilityEvaluation(CapabilityStatus.AVAILABLE, ("code.read",), (), ())

        async def ask(agent, _prompt):
            if agent is tester_agent:
                return "baseline pass"
            if agent is reviewer_agent:
                return "VERDICT: PASS"
            raise AssertionError(f"unexpected agent: {agent.id}")

        with (
            patch("workflows.engineering_delivery.capability_registry.evaluate", return_value=capability),
            patch("workflows.engineering_delivery.WorkspaceExecutorClient", FakeExecutorClient),
            patch("workflows.engineering_delivery._ask", new=AsyncMock(side_effect=ask)),
            patch(
                "workflows.engineering_delivery.review_with_quality_council",
                new=AsyncMock(return_value="VERDICT: FIX_REQUIRED"),
            ),
        ):
            output = await engineering_delivery_step(
                StepInput(
                    input={
                        "repo_id": "agentos",
                        "task": "Audit parser",
                        "intent": "audit",
                        "apply_fixes": False,
                        "quality": "strict",
                    }
                )
            )

        self.assertFalse(output.success)
        assert isinstance(output.content, dict)
        self.assertEqual(output.content["status"], OutcomeStatus.BLOCKED)

    async def test_security_failure_enters_fix_loop_and_is_rechecked(self) -> None:
        capability = CapabilityEvaluation(CapabilityStatus.AVAILABLE, ("code.read",), (), ())
        security_calls = 0

        async def ask(agent, _prompt):
            nonlocal security_calls
            if agent is code_agent:
                return "implemented"
            if agent is tester_agent:
                return "tests pass"
            if agent is reviewer_agent:
                return "VERDICT: PASS"
            if agent is security_agent:
                security_calls += 1
                return "VERDICT: FIX_REQUIRED" if security_calls == 1 else "VERDICT: PASS"
            if agent is fixer_agent:
                return "fixed"
            raise AssertionError(f"unexpected agent: {agent.id}")

        with (
            patch("workflows.engineering_delivery.capability_registry.evaluate", return_value=capability),
            patch("workflows.engineering_delivery.WorkspaceExecutorClient", FakeExecutorClient),
            patch("workflows.engineering_delivery._ask", new=AsyncMock(side_effect=ask)) as ask_mock,
        ):
            output = await engineering_delivery_step(
                StepInput(input={"repo_id": "agentos", "task": "Fix auth token validation", "intent": "implement"})
            )

        called_agents = [call.args[0] for call in ask_mock.await_args_list]
        self.assertTrue(output.success)
        self.assertEqual(called_agents.count(fixer_agent), 1)
        self.assertEqual(called_agents.count(tester_agent), 2)
        self.assertEqual(called_agents.count(reviewer_agent), 2)
        self.assertEqual(called_agents.count(security_agent), 2)

    async def test_strict_quality_invokes_private_council_after_draft(self) -> None:
        capability = CapabilityEvaluation(CapabilityStatus.AVAILABLE, ("code.read",), (), ())

        async def ask(agent, _prompt):
            if agent is tester_agent:
                return "baseline pass"
            if agent is reviewer_agent:
                return "VERDICT: PASS"
            raise AssertionError(f"unexpected agent: {agent.id}")

        council = AsyncMock(return_value="VERDICT: PASS")
        with (
            patch("workflows.engineering_delivery.capability_registry.evaluate", return_value=capability),
            patch("workflows.engineering_delivery.WorkspaceExecutorClient", FakeExecutorClient),
            patch("workflows.engineering_delivery._ask", new=AsyncMock(side_effect=ask)),
            patch("workflows.engineering_delivery.review_with_quality_council", new=council),
        ):
            output = await engineering_delivery_step(
                StepInput(
                    input={
                        "repo_id": "agentos",
                        "task": "Audit parser",
                        "intent": "audit",
                        "quality": "strict",
                    }
                )
            )

        self.assertTrue(output.success)
        council.assert_awaited_once()
