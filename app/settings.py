"""
App Settings
============

Shared runtime objects for the platform.
"""

from enum import StrEnum
from os import getenv

from agno.models.openai import OpenAIChat


class ModelRole(StrEnum):
    DEFAULT = "default"
    ROUTER = "router"
    FAST = "fast"
    GROWTH = "growth"
    RESEARCH = "research"
    ENGINEERING = "engineering"
    REVIEW = "review"


_MODEL_DEFAULTS = {
    ModelRole.DEFAULT: "cx/gpt-5.6-sol",
    ModelRole.ROUTER: "cx/gpt-5.6-luna",
    ModelRole.FAST: "cx/gpt-5.6-luna",
    ModelRole.GROWTH: "cx/gpt-5.6-terra",
    ModelRole.RESEARCH: "cx/gpt-5.6-terra",
    ModelRole.ENGINEERING: "cx/gpt-5.6-sol",
    ModelRole.REVIEW: "cx/gpt-5.6-sol-review",
}


def model_for(role: ModelRole | str) -> OpenAIChat:
    """Return a fresh, role-specific model with no runtime fallback."""
    resolved_role = ModelRole(role)
    env_name = f"WORKFORCE_MODEL_{resolved_role.value.upper()}_ID"
    if resolved_role == ModelRole.DEFAULT:
        model_id = getenv(env_name) or getenv("OPENAI_MODEL_ID") or _MODEL_DEFAULTS[resolved_role]
    else:
        model_id = getenv(env_name) or _MODEL_DEFAULTS[resolved_role]
    timeout = float(getenv("WORKFORCE_MODEL_TIMEOUT_SECONDS", "120"))
    return OpenAIChat(id=model_id, base_url=getenv("OPENAI_BASE_URL") or None, timeout=timeout)


def default_model() -> OpenAIChat:
    """Fresh model instance per agent — avoids shared-state footguns."""
    return model_for(ModelRole.DEFAULT)
