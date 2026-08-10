"""Factories and shared tool views for private workforce members."""

from collections.abc import Sequence
from os import getenv

from agno.agent import Agent
from agno.learn import LearnedKnowledgeConfig, LearningMachine, LearningMode, UserMemoryConfig, UserProfileConfig
from agno.tools.mcp import MCPTools

from agents.workforce.prompt_provenance import grounded_instructions
from app.settings import ModelRole, model_for
from db import create_knowledge, get_postgres_db
from workforce.capabilities import list_operation_capabilities


def specialist(
    *,
    agent_id: str,
    name: str,
    role: str,
    instructions: str,
    model_role: ModelRole,
    tools: Sequence | None = None,
    learning: LearningMachine | None = None,
) -> Agent:
    return Agent(
        id=agent_id,
        name=name,
        role=role,
        model=model_for(model_role),
        db=get_postgres_db(),
        tools=[list_operation_capabilities, *(tools or ())],
        learning=learning,
        instructions=grounded_instructions(agent_id, instructions),
        add_datetime_to_context=True,
        add_history_to_context=True,
        num_history_runs=5,
        markdown=True,
    )


def workspace_tools(
    *tool_names: str,
    requires_confirmation_tools: Sequence[str] | None = None,
) -> list[MCPTools]:
    url = getenv("WORKSPACE_EXECUTOR_MCP_URL")
    if not url:
        return []

    def headers() -> dict[str, str]:
        token = getenv("WORKSPACE_EXECUTOR_TOKEN")
        return {"Authorization": f"Bearer {token}"} if token else {}

    return [
        MCPTools(
            name="workspace_executor",
            url=url,
            transport="streamable-http",
            include_tools=list(tool_names),
            requires_confirmation_tools=list(requires_confirmation_tools or ()),
            header_provider=headers,
            timeout_seconds=180,
        )
    ]


def domain_learning(namespace: str) -> LearningMachine:
    knowledge = create_knowledge(f"{namespace.title()} Learnings", "workforce_learnings")
    return LearningMachine(
        db=get_postgres_db(),
        learned_knowledge=LearnedKnowledgeConfig(
            knowledge=knowledge,
            namespace=namespace,
            mode=LearningMode.AGENTIC,
            agent_can_search=True,
            agent_can_save=False,
        ),
        namespace=namespace,
    )


def lead_learning(namespace: str) -> LearningMachine:
    knowledge = create_knowledge(f"{namespace.title()} Learnings", "workforce_learnings")
    return LearningMachine(
        db=get_postgres_db(),
        user_profile=UserProfileConfig(mode=LearningMode.AGENTIC),
        user_memory=UserMemoryConfig(mode=LearningMode.AGENTIC),
        learned_knowledge=LearnedKnowledgeConfig(
            knowledge=knowledge,
            namespace=namespace,
            mode=LearningMode.AGENTIC,
            agent_can_search=True,
            agent_can_save=False,
        ),
        namespace=namespace,
    )
