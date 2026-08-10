"""Domain-boundary team type for non-recursive root routing."""

from agno.agent import Agent
from agno.run import RunContext
from agno.team import Team

ROUTER_DOMAIN_MEMBERS = frozenset({"chief", "engineering-team", "growth-team", "research-team"})


class DomainBoundaryTeam(Team):
    """Keep this Team's specialists private when it is nested under another Team."""

    def _find_member_by_id(
        self, member_id: str, run_context: RunContext | None = None
    ) -> tuple[int, Agent | Team] | None:
        if run_context is None:
            return super()._find_member_by_id(member_id, run_context=run_context)
        return None
