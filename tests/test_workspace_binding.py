from os import environ
from unittest import TestCase
from unittest.mock import patch

from agents.workforce.common import workspace_tools
from agents.workforce.engineering import LEAD_WORKSPACE_TOOLS, PATCH_TOOLS, REVIEW_TOOLS, TEST_TOOLS


class WorkspaceBindingTest(TestCase):
    def test_read_tools_are_direct_and_apply_patch_requires_confirmation(self) -> None:
        with patch.dict(
            environ,
            {
                "WORKSPACE_EXECUTOR_MCP_URL": "http://workspace-executor:8100/mcp",
                "WORKSPACE_EXECUTOR_TOKEN": "test-token",
            },
        ):
            toolkits = workspace_tools(
                "open_repository",
                "read_file",
                "apply_patch",
                "apply_trusted_patch",
                "publish_changes",
                "close_workspace",
                requires_confirmation_tools=("apply_patch",),
            )

        self.assertEqual(len(toolkits), 1)
        toolkit = toolkits[0]
        self.assertEqual(
            toolkit.include_tools,
            [
                "open_repository",
                "read_file",
                "apply_patch",
                "apply_trusted_patch",
                "publish_changes",
                "close_workspace",
            ],
        )
        self.assertEqual(toolkit.requires_confirmation_tools, ["apply_patch"])

    def test_engineering_roles_cannot_self_test_review_and_publish(self) -> None:
        lead_tools = set(LEAD_WORKSPACE_TOOLS)
        code_tools = set(PATCH_TOOLS)
        fixer_tools = set(PATCH_TOOLS)
        tester_tools = set(TEST_TOOLS)
        reviewer_tools = set(REVIEW_TOOLS)

        self.assertIn("publish_changes", lead_tools)
        self.assertNotIn("apply_trusted_patch", lead_tools)
        self.assertNotIn("run_check", lead_tools)
        self.assertNotIn("grant_publish", lead_tools)

        for patch_tools in (code_tools, fixer_tools):
            self.assertIn("apply_trusted_patch", patch_tools)
            self.assertNotIn("run_check", patch_tools)
            self.assertNotIn("grant_publish", patch_tools)
            self.assertNotIn("publish_changes", patch_tools)

        self.assertIn("run_check", tester_tools)
        self.assertNotIn("apply_trusted_patch", tester_tools)
        self.assertNotIn("grant_publish", tester_tools)
        self.assertNotIn("publish_changes", tester_tools)

        self.assertIn("grant_publish", reviewer_tools)
        self.assertNotIn("apply_trusted_patch", reviewer_tools)
        self.assertNotIn("run_check", reviewer_tools)
        self.assertNotIn("publish_changes", reviewer_tools)
