"""
AgentOS Entrypoint
==================
"""

from contextlib import asynccontextmanager
from os import getenv
from pathlib import Path

from agno.os import AgentOS
from agno.os.config import AuthorizationConfig
from agno.os.interfaces.agui import AGUI
from agno.utils.log import log_info

from agents.agent_builder import agent_builder
from agents.chief import chief
from agents.platform_manager import platform_manager
from agents.workforce.engineering import engineering_team
from agents.workforce.growth import growth_team
from agents.workforce.research import research_team
from agents.workforce.router import workforce_router
from app.registry import registry
from app.schedules import register_schedules
from db import get_postgres_db
from workflows.deployment_check import deployment_check
from workflows.continuous_learning import continuous_learning
from workflows.engineering_delivery import engineering_delivery
from workflows.research_pipeline import research_pipeline
from workflows.run_evals import run_evals

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
runtime_env = getenv("RUNTIME_ENV", "prd")
# Used by the scheduler and the OAuth server when MCP OAuth is enabled.
agentos_url = getenv("AGENTOS_URL", "http://127.0.0.1:8000")

# ---------------------------------------------------------------------------
# Interfaces
# - Chief becomes available on Slack when both env vars are set
# ---------------------------------------------------------------------------
SLACK_BOT_TOKEN = getenv("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = getenv("SLACK_SIGNING_SECRET", "")

interfaces: list = [AGUI(team=workforce_router)]
if SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET:
    from agno.os.interfaces.slack import Slack

    interfaces.append(
        Slack(
            agent=chief,
            streaming=True,
            token=SLACK_BOT_TOKEN,
            signing_secret=SLACK_SIGNING_SECRET,
            resolve_user_identity=True,
            loading_text="Barbequeing...",
        )
    )


# ---------------------------------------------------------------------------
# MCP OAuth — enabled by setting the MCP_CONNECT_SECRET environment variable.
# Connect your favorite AI apps and coding agents to a secure /mcp using OAuth.
# ---------------------------------------------------------------------------
MCP_CONNECT_SECRET = getenv("MCP_CONNECT_SECRET", "")

mcp_auth = None
if MCP_CONNECT_SECRET:
    from agno.os import AgentOSBuiltinAuth

    mcp_auth = AgentOSBuiltinAuth(
        url=agentos_url,
        secret=MCP_CONNECT_SECRET,
        signing_key_material=getenv("AGENTOS_MCP_SIGNING_KEY"),
    )


def authorization_config() -> AuthorizationConfig:
    """Enable ownership isolation and accept the optional short-lived Buzz JWT signer."""
    verification_keys: list[str] = []
    buzz_key = getenv("BUZZ_JWT_VERIFICATION_KEY")
    if buzz_key:
        verification_keys.append(buzz_key)
    buzz_key_file = getenv("BUZZ_JWT_VERIFICATION_KEY_FILE")
    if buzz_key_file:
        verification_keys.append(Path(buzz_key_file).read_text(encoding="utf-8"))
    audience = getenv("JWT_AUDIENCE")
    if verification_keys and not audience:
        raise RuntimeError("JWT_AUDIENCE is required when the Buzz JWT verification key is configured")
    return AuthorizationConfig(
        verification_keys=verification_keys or None,
        verify_audience=bool(audience),
        audience=audience,
        user_isolation=True,
    )


# ---------------------------------------------------------------------------
# Lifespan — app-level startup / teardown.
#
# AgentOS handles the MCP lifecycle (connect on startup, close on shutdown)
# for agent-attached and registry tools. Keep this hook to plug in your own setup.
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app):  # type: ignore[no-untyped-def]
    log_info("AgentOS lifespan: startup")
    # Register schedules on startup. Idempotent and fail-soft.
    register_schedules()
    try:
        yield
    finally:
        log_info("AgentOS lifespan: shutdown")


# ---------------------------------------------------------------------------
# Create AgentOS
# ---------------------------------------------------------------------------
agent_os = AgentOS(
    name="AgentOS",
    tracing=True,
    scheduler=True,
    scheduler_base_url=agentos_url,
    authorization=runtime_env != "dev",
    authorization_config=authorization_config(),
    mcp_server=True,
    mcp_auth=mcp_auth,
    lifespan=lifespan,
    db=get_postgres_db(),
    agents=[chief, agent_builder, platform_manager],
    teams=[workforce_router, engineering_team, growth_team, research_team],
    workflows=[deployment_check, run_evals, engineering_delivery, research_pipeline, continuous_learning],
    interfaces=interfaces,
    registry=registry,
    config=str(Path(__file__).parent / "config.yaml"),
)
app = agent_os.get_app()


if __name__ == "__main__":
    agent_os.serve(app="app.main:app", reload=False)
