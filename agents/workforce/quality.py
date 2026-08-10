"""Private post-draft quality council."""

from agno.team import Team
from agno.team.mode import TeamMode

from agents.workforce.common import specialist
from agents.workforce.prompt_provenance import grounded_team_instructions
from app.registry import get_parallel_tools
from app.settings import ModelRole, model_for
from db import get_postgres_db
from workforce.learning import evaluate_learning_candidate, promote_learning_candidate

critic_agent = specialist(
    agent_id="critic-agent",
    name="Critic",
    role="Find assumptions, blind spots, counterarguments, and failure scenarios.",
    instructions=(
        "Review the supplied draft after domain work. Identify concrete gaps without rewriting it prematurely."
    ),
    model_role=ModelRole.REVIEW,
)

logic_agent = specialist(
    agent_id="logic-agent",
    name="Logic Verifier",
    role="Check inference validity and contradictions.",
    instructions=(
        "Inspect whether conclusions follow from premises and flag weak, circular, or contradictory reasoning."
    ),
    model_role=ModelRole.REVIEW,
)

evidence_verifier = specialist(
    agent_id="evidence-verifier",
    name="Evidence Verifier",
    role="Check that evidence supports each material claim and learning candidate.",
    instructions=(
        "Return a separate PASS or FAIL verdict for every learning_candidate_id. "
        "Never use a task-level PASS as candidate approval."
    ),
    model_role=ModelRole.REVIEW,
    tools=[*get_parallel_tools(), evaluate_learning_candidate, promote_learning_candidate],
)

quality_council = Team(
    id="quality-council",
    name="Quality Council",
    mode=TeamMode.coordinate,
    model=model_for(ModelRole.REVIEW),
    db=get_postgres_db(),
    members=[critic_agent, logic_agent, evidence_verifier],
    instructions=grounded_team_instructions(
        "quality-council",
        [
            "Evaluate only after a domain draft exists.",
            "Invoke only the evaluators required by the quality policy.",
            "For draft review, end with exactly VERDICT: PASS or VERDICT: FIX_REQUIRED.",
            "Learning verdicts are independent and keyed by learning_candidate_id.",
            "Call promotion only after that exact candidate has a PASS verdict.",
        ],
    ),
    tools=[evaluate_learning_candidate, promote_learning_candidate],
    markdown=True,
)
