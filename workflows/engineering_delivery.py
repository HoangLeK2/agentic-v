"""Deterministic engineering workflow with implement and audit branches."""

import asyncio
import json
import logging
from typing import Any

from agno.workflow.step import Step, StepInput, StepOutput
from agno.workflow.workflow import Workflow

from agents.workforce.engineering import code_agent, fixer_agent, reviewer_agent, security_agent, tester_agent
from db import get_postgres_db
from services.workspace_executor.client import WorkspaceExecutorClient
from workforce.capabilities import CapabilityStatus, capability_registry
from workforce.contracts import (
    Artifact,
    EngineeringIntent,
    EngineeringTaskInput,
    OutcomeStatus,
    QualityLevel,
    WorkforceOutcome,
)
from workforce.runtime_tools import review_with_quality_council
from workforce.verdicts import terminal_verdict

MAX_FIX_LOOPS = 2
logger = logging.getLogger(__name__)


def _task(step_input: StepInput) -> EngineeringTaskInput:
    value = step_input.input
    if isinstance(value, EngineeringTaskInput):
        return value
    if isinstance(value, dict):
        return EngineeringTaskInput.model_validate(value)
    if isinstance(value, str):
        try:
            return EngineeringTaskInput.model_validate_json(value)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("engineering-delivery requires structured input with repo_id and task") from exc
    raise ValueError("engineering-delivery requires EngineeringTaskInput")


async def _ask(agent: Any, prompt: str) -> str:
    result = await agent.arun(prompt, stream=False)
    return result.get_content_as_string()


async def _run_checks(client: WorkspaceExecutorClient, workspace_id: str) -> list[dict]:
    check_ids = await client.list_checks(workspace_id)
    return [await client.run_check(workspace_id, check_id) for check_id in check_ids]


def _checks_pass(checks: list[dict]) -> bool:
    return bool(checks) and all(bool(check.get("success")) for check in checks)


def _fix_required(review: str, checks: list[dict]) -> bool:
    return not _checks_pass(checks) or terminal_verdict(review) != "PASS"


def _reviews_require_fix(reviews: list[str], checks: list[dict]) -> bool:
    return not _checks_pass(checks) or any(_fix_required(review, checks) for review in reviews)


def _security_sensitive(task: str, diff: str) -> bool:
    haystack = f"{task}\n{diff}".lower()
    markers = (
        "auth",
        "permission",
        "secret",
        "token",
        "password",
        "subprocess",
        "shell",
        "dependency",
        "migration",
        "database",
        "network",
    )
    return any(marker in haystack for marker in markers)


def _check_summary(checks: list[dict]) -> str:
    return "\n".join(
        f"- {check.get('check_id')}: {'PASS' if check.get('success') else 'FAIL'}\n"
        f"  stdout: {str(check.get('stdout') or '')[-4000:]}\n"
        f"  stderr: {str(check.get('stderr') or '')[-4000:]}"
        for check in checks
    )


def _quality_draft(task: EngineeringTaskInput, diff: str, checks: list[dict], findings: list[str]) -> str:
    return (
        f"Task: {task.task}\nAcceptance criteria: {list(task.acceptance_criteria)}\n"
        f"CHECKS:\n{_check_summary(checks)}\nDIFF:\n{diff}\nINDEPENDENT REVIEWS:\n" + "\n\n".join(findings)
    )


async def engineering_delivery_step(step_input: StepInput) -> StepOutput:
    task = _task(step_input)
    required: tuple[str, ...] = ("code.read", "code.run_checks")
    if task.intent == EngineeringIntent.IMPLEMENT or task.apply_fixes:
        required += ("code.sandbox_write",)
    capabilities = capability_registry.evaluate(required)
    if capabilities.status == CapabilityStatus.UNAVAILABLE:
        outcome = WorkforceOutcome(
            status=OutcomeStatus.CAPABILITY_UNAVAILABLE,
            summary="The engineering workflow cannot run because required executor operations are unavailable.",
            unavailable_capabilities=capabilities.unavailable,
            degraded_capabilities=capabilities.degraded,
        )
        return StepOutput(content=outcome.model_dump(mode="json"), success=False)

    workspace_id: str | None = None
    checks: list[dict] = []
    findings: list[str] = []
    delegated_to: list[str] = []
    final_diff = ""
    try:
        async with WorkspaceExecutorClient() as client:
            workspace_id = await client.create_workspace(task.repo_id)
            context = (
                f"Workspace id: {workspace_id}\nTask: {task.task}\n"
                f"Acceptance criteria: {list(task.acceptance_criteria)}"
            )

            if task.intent == EngineeringIntent.IMPLEMENT:
                delegated_to.append("code-agent")
                await _ask(
                    code_agent,
                    context + "\nImplement the request using workspace tools. Do not review your own patch. "
                    "Finish with a concise change summary.",
                )
                checks = await _run_checks(client, workspace_id)
                delegated_to.append("tester-agent")
                tester_report = await _ask(
                    tester_agent,
                    context + "\nEvaluate this check evidence:\n" + _check_summary(checks),
                )
                final_diff = await client.git_diff(workspace_id)
                reviewer_report = await _ask(
                    reviewer_agent,
                    context + "\nReview this diff and independent test report. "
                    "End with VERDICT: PASS or VERDICT: FIX_REQUIRED.\n"
                    f"TEST REPORT:\n{tester_report}\nDIFF:\n{final_diff}",
                )
                delegated_to.append("reviewer-agent")
                findings.append(reviewer_report)
                if _security_sensitive(task.task, final_diff):
                    delegated_to.append("security-agent")
                    findings.append(
                        await _ask(
                            security_agent,
                            context + "\nReview this security-sensitive diff read-only. "
                            "End with VERDICT: PASS or VERDICT: FIX_REQUIRED.\n" + final_diff,
                        )
                    )
            else:
                checks = await _run_checks(client, workspace_id)
                final_diff = await client.git_diff(workspace_id)
                tester_prompt = context + "\nAnalyze this baseline check evidence read-only:\n" + _check_summary(checks)
                reviewer_prompt = (
                    context + "\nAudit the repository read-only and return severity-ordered findings. "
                    "End with exactly VERDICT: PASS or VERDICT: FIX_REQUIRED."
                )
                tasks = [_ask(tester_agent, tester_prompt), _ask(reviewer_agent, reviewer_prompt)]
                delegated_to.extend(("tester-agent", "reviewer-agent"))
                if _security_sensitive(task.task, final_diff):
                    tasks.append(
                        _ask(
                            security_agent,
                            context + "\nPerform a read-only security audit. "
                            "End with VERDICT: PASS or VERDICT: FIX_REQUIRED.",
                        )
                    )
                    delegated_to.append("security-agent")
                reports = await asyncio.gather(*tasks)
                findings.extend(reports)
                latest_reviews = [reports[1], *reports[2:]]

            if task.intent == EngineeringIntent.IMPLEMENT:
                latest_reviews = findings.copy()
            if task.quality == QualityLevel.STRICT:
                quality_review = await review_with_quality_council(
                    _quality_draft(task, final_diff, checks, findings),
                    "strict",
                )
                findings.append(quality_review)
                latest_reviews.append(quality_review)
            if task.apply_fixes and _reviews_require_fix(latest_reviews, checks):
                delegated_to.append("fixer-agent")
                for _iteration in range(MAX_FIX_LOOPS):
                    await _ask(
                        fixer_agent,
                        context
                        + "\nAddress only these independent findings, using workspace tools:\n"
                        + "\n\n".join(findings),
                    )
                    checks = await _run_checks(client, workspace_id)
                    tester_report = await _ask(
                        tester_agent,
                        context + "\nEvaluate the post-fix check evidence:\n" + _check_summary(checks),
                    )
                    final_diff = await client.git_diff(workspace_id)
                    final_review = await _ask(
                        reviewer_agent,
                        context + f"\nFinal review after fixes. End with VERDICT: PASS or VERDICT: FIX_REQUIRED.\n"
                        f"TEST REPORT:\n{tester_report}\nCHECKS:\n{_check_summary(checks)}\nDIFF:\n{final_diff}",
                    )
                    latest_reviews = [final_review]
                    if _security_sensitive(task.task, final_diff):
                        latest_reviews.append(
                            await _ask(
                                security_agent,
                                context + "\nRe-review the post-fix security-sensitive diff. "
                                "End with VERDICT: PASS or VERDICT: FIX_REQUIRED.\n" + final_diff,
                            )
                        )
                    if task.quality == QualityLevel.STRICT:
                        latest_reviews.append(
                            await review_with_quality_council(
                                _quality_draft(task, final_diff, checks, latest_reviews),
                                "strict",
                            )
                        )
                    findings.extend(latest_reviews)
                    if not _reviews_require_fix(latest_reviews, checks):
                        break

            blocked = _reviews_require_fix(latest_reviews, checks)
            outcome = WorkforceOutcome(
                status=OutcomeStatus.BLOCKED if blocked else OutcomeStatus.COMPLETED,
                summary=(
                    "Engineering delivery has unresolved findings; fixes were not requested."
                    if blocked and not task.apply_fixes
                    else "Engineering delivery stopped after the maximum fix loops."
                    if blocked
                    else "Engineering delivery completed with independent test and review evidence."
                ),
                delegated_to=tuple(dict.fromkeys(delegated_to)),
                artifacts=(Artifact(kind="diff", name="workspace.diff", content=final_diff),),
                checks=tuple(
                    f"{check.get('check_id')}:{'PASS' if check.get('success') else 'FAIL'}" for check in checks
                ),
                findings=tuple(findings),
                degraded_capabilities=capabilities.degraded,
            )
            return StepOutput(content=outcome.model_dump(mode="json"), success=not blocked)
    except Exception:
        logger.exception("Engineering delivery failed")
        outcome = WorkforceOutcome(
            status=OutcomeStatus.FAILED,
            summary="Engineering delivery failed. Inspect the server trace with the run id for diagnostic details.",
        )
        return StepOutput(content=outcome.model_dump(mode="json"), success=False)
    finally:
        if workspace_id:
            try:
                async with WorkspaceExecutorClient() as cleanup_client:
                    await cleanup_client.close_workspace(workspace_id)
            except Exception:
                logger.exception("Could not close disposable workspace %s", workspace_id)


engineering_delivery = Workflow(
    id="engineering-delivery",
    name="Engineering Delivery",
    description="Implement, audit, and optionally fix an allowlisted repository in a disposable workspace.",
    db=get_postgres_db(),
    input_schema=EngineeringTaskInput,
    steps=[Step(name="engineering-delivery", executor=engineering_delivery_step, max_retries=0)],
)
