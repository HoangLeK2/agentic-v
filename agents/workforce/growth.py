"""Growth specialists and their public coordinating team."""

from agno.team.mode import TeamMode

from agents.workforce.common import lead_learning, specialist
from agents.workforce.prompt_provenance import grounded_team_instructions
from app.registry import get_parallel_tools
from app.settings import ModelRole, model_for
from db import get_postgres_db
from workforce.capabilities import list_operation_capabilities
from workforce.delegation import DomainBoundaryTeam
from workforce.learning import propose_learning_candidate
from workforce.runtime_tools import review_with_quality_council

WEB_TOOLS = get_parallel_tools()

growth_lead = specialist(
    agent_id="growth-lead",
    name="Growth Lead",
    role="Coordinate growth work from evidence to deliverable.",
    instructions=(
        "Check operation capabilities and delegate only needed specialists. "
        "Preserve the boundary between SEO, marketing, content, and analytics."
    ),
    model_role=ModelRole.GROWTH,
    learning=lead_learning("growth"),
)

seo_agent = specialist(
    agent_id="seo-agent",
    name="SEO Agent",
    role="Perform public-data keyword, content, and static SEO analysis.",
    instructions=(
        "Use public web evidence for static audit and keyword research. "
        "Fail closed when Search Console or Analytics operations are unavailable."
    ),
    model_role=ModelRole.GROWTH,
    tools=WEB_TOOLS,
)

marketing_agent = specialist(
    agent_id="marketing-agent",
    name="Marketing Agent",
    role="Develop positioning and campaign strategy from supplied brand context.",
    instructions="Separate known brand/product facts from assumptions and never invent CRM, ads, or conversion data.",
    model_role=ModelRole.GROWTH,
    tools=WEB_TOOLS,
)

content_agent = specialist(
    agent_id="content-agent",
    name="Content Agent",
    role="Create channel-appropriate content from verified strategy and evidence.",
    instructions=(
        "Write from supplied brand context and verified research. Do not introduce unsupported product claims."
    ),
    model_role=ModelRole.GROWTH,
)

market_research_agent = specialist(
    agent_id="market-research-agent",
    name="Market Research Agent",
    role="Research markets, customers, and positioning with sources.",
    instructions="Search multiple credible sources, separate facts from inference, and include direct source links.",
    model_role=ModelRole.RESEARCH,
    tools=WEB_TOOLS,
)

analytics_agent = specialist(
    agent_id="analytics-agent",
    name="Analytics Agent",
    role="Interpret configured analytics data without fabricating metrics.",
    instructions=(
        "Check seo.analytics first. If unavailable, return capability_unavailable "
        "instead of estimating private metrics."
    ),
    model_role=ModelRole.GROWTH,
)

competitor_agent = specialist(
    agent_id="competitor-agent",
    name="Competitor Agent",
    role="Compare competitor products and public positioning.",
    instructions="Use current public sources, attach dates and links, and label inference explicitly.",
    model_role=ModelRole.RESEARCH,
    tools=WEB_TOOLS,
)

growth_team = DomainBoundaryTeam(
    id="growth-team",
    name="Growth Team",
    mode=TeamMode.coordinate,
    model=model_for(ModelRole.GROWTH),
    db=get_postgres_db(),
    members=[
        growth_lead,
        seo_agent,
        marketing_agent,
        content_agent,
        market_research_agent,
        analytics_agent,
        competitor_agent,
    ],
    tools=[list_operation_capabilities, propose_learning_candidate, review_with_quality_council],
    instructions=grounded_team_instructions(
        "growth-team",
        [
            "Delegate only specialists required by the requested deliverable.",
            "Use operation capabilities; one unavailable integration must not disable unrelated operations.",
            "Never substitute public estimates for unavailable private analytics without an explicit user request.",
            "For strict quality, send the completed draft to the Quality Council and fail closed on an "
            "invalid verdict.",
            "Return the requested growth artifact with source-grounded findings. "
            "The public router normalizes the final outcome.",
        ],
    ),
    add_datetime_to_context=True,
    add_history_to_context=True,
    num_history_runs=5,
    show_members_responses=True,
    markdown=True,
)
