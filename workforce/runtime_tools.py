"""Lazy orchestration tools exposed only on public domain teams."""

from typing import Literal


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
    acceptance_criteria: list[str] | None = None,
    quality: Literal["auto", "basic", "strict"] = "auto",
) -> dict:
    """Run the deterministic engineering delivery workflow for repository work."""
    from workflows.engineering_delivery import engineering_delivery

    output = await engineering_delivery.arun(
        input={
            "repo_id": repo_id,
            "task": task,
            "intent": intent,
            "apply_fixes": apply_fixes,
            "acceptance_criteria": acceptance_criteria or [],
            "quality": quality,
        }
    )
    return output.to_dict()


async def run_research_pipeline(question: str, quality: Literal["auto", "basic", "strict"] = "auto") -> dict:
    """Run the deterministic research pipeline for long-horizon questions."""
    from workflows.research_pipeline import research_pipeline

    output = await research_pipeline.arun(input={"question": question, "quality": quality})
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
