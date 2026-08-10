"""
Delivery Team
=============

A four-role team for planning, research, implementation, and review.
"""

from agno.agent import Agent
from agno.team import Team

from app.settings import default_model
from db import get_postgres_db


def delivery_agent(agent_id: str, name: str, role: str, instructions: str) -> Agent:
    return Agent(
        id=agent_id,
        name=name,
        role=role,
        model=default_model(),
        db=get_postgres_db(),
        instructions=instructions,
        add_datetime_to_context=True,
        add_history_to_context=True,
        num_history_runs=5,
        markdown=True,
    )


planner = delivery_agent(
    "planner",
    "Planner",
    "Analyze requirements and divide the work.",
    "Clarify the objective, assumptions, constraints, dependencies, and an ordered execution plan.",
)

researcher = delivery_agent(
    "researcher",
    "Researcher",
    "Verify facts and evaluate options.",
    "Investigate the delegated questions, distinguish facts from assumptions, and compare credible options.",
)

builder = delivery_agent(
    "builder",
    "Builder",
    "Design the solution and implementation plan.",
    "Turn the plan and evidence into a concrete solution with implementation and verification details.",
)

reviewer = delivery_agent(
    "reviewer",
    "Reviewer",
    "Review risks and acceptance criteria.",
    "Critically review the proposed solution, identify gaps and risks, and define clear acceptance criteria.",
)

delivery_team = Team(
    id="delivery-team",
    name="Delivery Team",
    description="A role-based team that plans, researches, builds, and reviews every mission.",
    model=default_model(),
    db=get_postgres_db(),
    members=[planner, researcher, builder, reviewer],
    instructions=[
        "Delegate work to every member according to their role.",
        "Do not skip a member; each member must produce a separate result.",
        "The reviewer must inspect the proposed solution before the final response.",
        "Synthesize the final answer in the language used by the user.",
    ],
    add_datetime_to_context=True,
    add_history_to_context=True,
    num_history_runs=5,
    show_members_responses=True,
    markdown=True,
)
