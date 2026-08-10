"""Research specialists and their public coordinating team."""

from agno.team.mode import TeamMode

from agents.workforce.common import lead_learning, specialist
from agents.workforce.prompt_provenance import grounded_team_instructions
from app.registry import get_parallel_tools
from app.settings import ModelRole, model_for
from db import get_postgres_db
from workforce.capabilities import list_operation_capabilities
from workforce.delegation import DomainBoundaryTeam
from workforce.learning import propose_learning_candidate
from workforce.runtime_tools import run_research_pipeline

WEB_TOOLS = get_parallel_tools()

research_lead = specialist(
    agent_id="research-lead",
    name="Research Lead",
    role="Plan research and select the minimum useful specialist sequence.",
    instructions="Use research-pipeline for long-horizon questions. Require source verification before synthesis.",
    model_role=ModelRole.RESEARCH,
    learning=lead_learning("research"),
)

search_agent = specialist(
    agent_id="search-agent",
    name="Search Agent",
    role="Generate and iterate search queries.",
    instructions=(
        "Search iteratively and deduplicate discovered URLs. "
        "Return query and source evidence rather than an unsupported answer."
    ),
    model_role=ModelRole.FAST,
    tools=WEB_TOOLS,
)

web_research_agent = specialist(
    agent_id="web-research-agent",
    name="Web Research Agent",
    role="Read and compare relevant web sources.",
    instructions=("Fetch sources, extract task-relevant evidence, and distinguish publication date from event date."),
    model_role=ModelRole.RESEARCH,
    tools=WEB_TOOLS,
)

source_verifier = specialist(
    agent_id="source-verifier",
    name="Source Verifier",
    role="Verify that cited sources support material claims.",
    instructions=(
        "Check source quality, date, independence, and claim support. "
        "Return insufficient_evidence when support is missing."
    ),
    model_role=ModelRole.REVIEW,
    tools=WEB_TOOLS,
)

synthesis_agent = specialist(
    agent_id="synthesis-agent",
    name="Synthesis Agent",
    role="Synthesize verified evidence into a concise answer.",
    instructions="Use only verified evidence, cite links near claims, and label any inference explicitly.",
    model_role=ModelRole.RESEARCH,
)

research_team = DomainBoundaryTeam(
    id="research-team",
    name="Research Team",
    mode=TeamMode.coordinate,
    model=model_for(ModelRole.RESEARCH),
    db=get_postgres_db(),
    members=[research_lead, search_agent, web_research_agent, source_verifier, synthesis_agent],
    tools=[list_operation_capabilities, run_research_pipeline, propose_learning_candidate],
    instructions=grounded_team_instructions(
        "research-team",
        [
            "Delegate only the research stages the request needs.",
            "Use research-pipeline for long-horizon or evidence-sensitive tasks.",
            "Material factual claims require Source Verifier before final synthesis.",
            "Return the requested research artifact with its sources and clearly preserve "
            "insufficient_evidence status when support is missing. The public router normalizes the final outcome.",
        ],
    ),
    add_datetime_to_context=True,
    add_history_to_context=True,
    num_history_runs=5,
    show_members_responses=True,
    markdown=True,
)
