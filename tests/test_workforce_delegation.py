from typing import Any, cast
from unittest import IsolatedAsyncioTestCase

from agno.run import RunContext
from agno.run.team import TeamRunOutput
from agno.session import TeamSession
from agno.team._default_tools import _get_delegate_task_function
from agno.team._tools import _find_member_by_id

from agents.workforce.engineering import engineering_team
from agents.workforce.router import workforce_router
from workforce.delegation import ROUTER_DOMAIN_MEMBERS, DomainBoundaryTeam


class WorkforceDelegationTest(IsolatedAsyncioTestCase):
    @staticmethod
    def _delegate_function(*, async_mode: bool):
        return _get_delegate_task_function(
            team=workforce_router,
            run_response=TeamRunOutput(run_id="run", team_id="workforce-router", session_id="session"),
            run_context=RunContext(run_id="run", session_id="session", session_state={}),
            session=TeamSession(session_id="session", team_id="workforce-router"),
            team_run_context={},
            async_mode=async_mode,
        )

    def test_live_router_topology_is_domain_only(self) -> None:
        members = cast(list[Any], workforce_router.members)
        self.assertEqual({member.id for member in members}, set(ROUTER_DOMAIN_MEMBERS))
        self.assertTrue(all(isinstance(member, DomainBoundaryTeam) for member in members if member.id != "chief"))

    def test_storage_resolver_can_scrub_nested_specialist_runs(self) -> None:
        result = workforce_router._find_member_by_id("engineering-lead")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result[1].id, "engineering-lead")

    def test_execution_resolver_rejects_nested_specialist_in_sync_and_async_paths(self) -> None:
        run_context = RunContext(run_id="run", session_id="session", session_state={})

        self.assertIsNone(workforce_router._find_member_by_id("engineering-lead", run_context=run_context))
        self.assertIsNone(workforce_router._find_member_route_by_id("engineering-lead", run_context=run_context))

    def test_root_resolver_still_finds_each_direct_domain_member(self) -> None:
        for member_id in ROUTER_DOMAIN_MEMBERS:
            with self.subTest(member_id=member_id):
                result = workforce_router._find_member_by_id(member_id)
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(result[1].id, member_id)

    def test_domain_team_default_resolver_still_finds_its_direct_specialist(self) -> None:
        result = _find_member_by_id(engineering_team, "engineering-lead")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result[1].id, "engineering-lead")

    def test_sync_delegate_tool_rejects_nested_specialist(self) -> None:
        function = self._delegate_function(async_mode=False)

        result = list(function.entrypoint(member_id="engineering-lead", task="fix"))

        self.assertIn("Member with ID engineering-lead not found", result[0])

    async def test_async_delegate_tool_rejects_nested_specialist(self) -> None:
        function = self._delegate_function(async_mode=True)
        result = []

        async for item in function.entrypoint(member_id="engineering-lead", task="fix"):
            result.append(item)

        self.assertIn("Member with ID engineering-lead not found", result[0])
