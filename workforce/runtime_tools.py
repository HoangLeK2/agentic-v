"""Lazy orchestration tools exposed only on public domain teams."""

from typing import Literal


def _clamp(value: int, *, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


async def list_workspace_repositories() -> tuple[dict[str, object], ...]:
    """List allowlisted internal repositories without exposing host paths."""
    from services.workspace_executor.client import WorkspaceExecutorClient

    async with WorkspaceExecutorClient() as client:
        return await client.list_repositories()


async def run_engineering_delivery(
    repo_id: str,
    task: str,
    intent: Literal["implement", "audit"] = "implement",
    apply_fixes: bool = True,
    execution_mode: Literal["fast", "standard", "deep"] = "standard",
    max_fix_loops: int | None = None,
    acceptance_criteria: list[str] | None = None,
    quality: Literal["auto", "basic", "strict"] = "auto",
) -> dict:
    """Run the deterministic engineering delivery workflow for repository work."""
    from workflows.engineering_delivery import engineering_delivery

    payload: dict[str, object] = {
        "repo_id": repo_id,
        "task": task,
        "intent": intent,
        "apply_fixes": apply_fixes,
        "execution_mode": execution_mode,
        "acceptance_criteria": acceptance_criteria or [],
        "quality": quality,
    }
    if max_fix_loops is not None:
        payload["max_fix_loops"] = max_fix_loops
    output = await engineering_delivery.arun(input=payload)
    return output.to_dict()


async def run_research_pipeline(
    question: str,
    quality: Literal["auto", "basic", "strict"] = "auto",
    execution_mode: Literal["fast", "standard", "deep"] = "fast",
    max_search_rounds: int | None = None,
    max_sources: int | None = None,
) -> dict:
    """Run the deterministic research pipeline for long-horizon questions."""
    from workflows.research_pipeline import research_pipeline

    payload: dict[str, object] = {"question": question, "quality": quality, "execution_mode": execution_mode}
    if max_search_rounds is not None:
        payload["max_search_rounds"] = _clamp(max_search_rounds, minimum=1, maximum=3)
    if max_sources is not None:
        payload["max_sources"] = _clamp(max_sources, minimum=1, maximum=8)
    output = await research_pipeline.arun(input=payload)
    return output.to_dict()


async def review_with_quality_council(draft: str, policy: Literal["critic", "logic", "evidence", "strict"]) -> str:
    """Review a completed draft with the private Quality Council."""
    from agents.workforce.quality import quality_council

    result = await quality_council.arun(
        f"Review this completed draft under the {policy!r} policy. Do not redo domain execution. "
        "End with exactly VERDICT: PASS or VERDICT: FIX_REQUIRED.\n\n"
        f"{draft}",
        stream=False,
    )
    return result.get_content_as_string()
