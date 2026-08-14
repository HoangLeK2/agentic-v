"""Factories and shared tool views for private workforce members."""

import json
import re
from collections.abc import Sequence
from datetime import datetime
from os import getenv
from typing import Any

from agno.agent import Agent
from agno.learn import LearnedKnowledgeConfig, LearningMachine, LearningMode, UserMemoryConfig, UserProfileConfig
from agno.session import SessionSummaryManager
from agno.session.summary import SessionSummary
from agno.tools.mcp import MCPTools

from agents.workforce.prompt_provenance import grounded_instructions
from app.settings import ModelRole, model_for
from db import create_knowledge, get_postgres_db
from workforce.capabilities import list_operation_capabilities

SESSION_SUMMARY_PROMPT = """\
Analyze the conversation and return exactly one JSON object with lowercase keys:
{"summary":"...", "topics":["..."]}.
The "summary" value must be concise and preserve only durable context needed for future turns.
The "topics" value must be a short array of topic names. Do not use markdown or extra text.
"""


class WorkforceSessionSummaryManager(SessionSummaryManager):
    """Normalize common OpenAI-compatible schema drift before Agno stores summaries."""

    def _process_summary_response(self, summary_response: Any, session_summary_model: Any) -> SessionSummary | None:
        payload = _summary_payload(getattr(summary_response, "content", None))
        if payload is None:
            return super()._process_summary_response(summary_response, session_summary_model)
        summary = payload.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            return super()._process_summary_response(summary_response, session_summary_model)
        topics_value = payload.get("topics")
        topics = (
            [str(topic) for topic in topics_value if str(topic).strip()]
            if isinstance(topics_value, list)
            else None
        )
        session_summary = SessionSummary(summary=summary.strip(), topics=topics, updated_at=datetime.now())
        self.summary = session_summary
        self.summaries_updated = True
        return session_summary


def _summary_payload(content: Any) -> dict[str, Any] | None:
    if isinstance(content, dict):
        return {str(key).casefold(): value for key, value in content.items()}
    if not isinstance(content, str):
        return None

    text = content.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match is None:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    return {str(key).casefold(): value for key, value in payload.items()}


def workforce_session_summary_manager() -> WorkforceSessionSummaryManager:
    return WorkforceSessionSummaryManager(
        model=model_for(ModelRole.FAST),
        session_summary_prompt=SESSION_SUMMARY_PROMPT,
        last_n_runs=6,
        conversation_limit=24,
    )


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
        add_history_to_context=False,
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
