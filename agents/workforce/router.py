"""Public workforce coordinator."""

from agno.team import Team
from agno.team.mode import TeamMode

from agents.chief import chief
from agents.workforce.common import domain_learning, workforce_session_summary_manager
from agents.workforce.engineering import engineering_team
from agents.workforce.growth import growth_team
from agents.workforce.prompt_provenance import grounded_team_instructions
from agents.workforce.research import research_team
from app.settings import ModelRole, model_for
from db import get_postgres_db
from workforce.capabilities import list_operation_capabilities
from workforce.contracts import WorkforceOutcome
from workforce.runtime_tools import list_workspace_repositories, review_with_quality_council

workforce_router = Team(
    id="workforce-router",
    name="Workforce Router",
    description="Routes work to the minimum set of domain teams and synthesizes the result.",
    mode=TeamMode.coordinate,
    model=model_for(ModelRole.ROUTER),
    db=get_postgres_db(),
    members=[chief, engineering_team, growth_team, research_team],
    tools=[list_operation_capabilities, list_workspace_repositories, review_with_quality_council],
    output_schema=WorkforceOutcome,
    learning=domain_learning("global"),
    enable_session_summaries=True,
    session_summary_manager=workforce_session_summary_manager(),
    add_session_summary_to_context=True,
    determine_input_for_members=True,
    delegate_to_all_members=False,
    instructions=grounded_team_instructions(
        "workforce-router",
        [
            "Classify the request, inspect operation capabilities, and delegate only to these direct members: "
            "chief, engineering-team, growth-team, or research-team. Never address a nested specialist from here; "
            "the selected domain Team owns specialist routing.",
            "Default to the fastest sufficient lane. Answer simple conversational/status questions directly. "
            "Use deep multi-agent workflows only when the user explicitly asks for deep research, a large feature, "
            "or the task has high risk.",
            "Before routing a request that names an internal project or repository, list the registered repositories. "
            "When one matches, use its exact repo_id, delegate to Engineering, and require repository file or code "
            "evidence before external research. Use Research for external facts or an explicit web-research request. "
            "For repository questions that can be answered from code, prefer Engineering/code tools over web research.",
            "For engineering capability checks, use prefix='engineering' or inspect code.read, code.sandbox_write, "
            "code.run_checks, and security.static_review. Never treat an empty made-up engineering.* prefix as proof "
            "that repository writes are unavailable.",
            "When logs, checks, audits, or repository evidence identify a fixable defect, require Engineering to "
            "remediate, test, independently review, and publish it. Do not return repair instructions for the user to "
            "execute. Stop only for a missing capability, irreducible missing input, an approval_required write, or a "
            "quality gate that remains failed after the bounded fix loop.",
            "For requests to add a feature, change code, or fix a repository, delegate to Engineering with explicit "
            "instructions to call run_engineering_delivery(repo_id, task, intent='implement', apply_fixes=true, "
            "execution_mode='standard'). Use execution_mode='deep' only for large features or explicit deep work.",
            "Use Chief for organizational memory and decisions, not as a substitute for domain execution.",
            "Order multi-domain delegations by dependency and synthesize after required drafts exist. "
            "Do not delegate to multiple domains when one domain can satisfy the request.",
            "Apply the requested quality policy only after a domain draft exists; "
            "strict work uses the Quality Council.",
            "Never broadcast to every member. Never claim an unavailable operation ran.",
            "Answer in the user's language and state degraded or unavailable capabilities explicitly.",
            "Always return the WorkforceOutcome contract; never return an unstructured success claim.",
        ],
    ),
    add_datetime_to_context=True,
    add_history_to_context=True,
    num_history_runs=2,
    show_members_responses=True,
    markdown=True,
)
