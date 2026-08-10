"""Deterministic long-horizon research pipeline."""

import json
import logging

from agno.workflow.step import Step, StepInput, StepOutput
from agno.workflow.workflow import Workflow

from agents.workforce.research import search_agent, source_verifier, synthesis_agent, web_research_agent
from db import get_postgres_db
from workforce.capabilities import CapabilityStatus, capability_registry
from workforce.contracts import OutcomeStatus, QualityLevel, ResearchTaskInput, WorkforceOutcome
from workforce.runtime_tools import review_with_quality_council
from workforce.verdicts import terminal_verdict

logger = logging.getLogger(__name__)


def _task(step_input: StepInput) -> ResearchTaskInput:
    value = step_input.input
    if isinstance(value, ResearchTaskInput):
        return value
    if isinstance(value, dict):
        return ResearchTaskInput.model_validate(value)
    if isinstance(value, str):
        try:
            return ResearchTaskInput.model_validate_json(value)
        except (ValueError, json.JSONDecodeError):
            return ResearchTaskInput(question=value)
    raise ValueError("research-pipeline requires a question")


async def _ask(agent, prompt: str) -> str:
    result = await agent.arun(prompt, stream=False)
    return result.get_content_as_string()


def _passed(report: str) -> bool:
    return terminal_verdict(report) == "PASS"


async def research_pipeline_step(step_input: StepInput) -> StepOutput:
    task = _task(step_input)
    capabilities = capability_registry.evaluate(("research.web_search", "research.deep_fetch"))
    if capabilities.status == CapabilityStatus.UNAVAILABLE:
        outcome = WorkforceOutcome(
            status=OutcomeStatus.CAPABILITY_UNAVAILABLE,
            summary="Required research operations are unavailable.",
            unavailable_capabilities=capabilities.unavailable,
        )
        return StepOutput(content=outcome.model_dump(mode="json"), success=False)

    try:
        search_plan = await _ask(
            search_agent,
            f"Question: {task.question}\nRun up to {task.max_search_rounds} query rounds. "
            "Return deduplicated URLs and why each matters.",
        )
        evidence = await _ask(
            web_research_agent,
            f"Question: {task.question}\nRead and compare the sources discovered below. Preserve URLs and dates.\n"
            f"{search_plan}",
        )
        verification = await _ask(
            source_verifier,
            f"Question: {task.question}\nVerify every material claim against these sources. "
            f"State VERDICT: PASS or VERDICT: INSUFFICIENT_EVIDENCE.\n{evidence}",
        )
        verified = _passed(verification)
        synthesis = await _ask(
            synthesis_agent,
            f"Question: {task.question}\nSynthesize only verified claims, with source links near claims.\n"
            f"EVIDENCE:\n{evidence}\nVERIFICATION:\n{verification}",
        )
        findings = [verification]
        quality_blocked = False
        if verified and task.quality == QualityLevel.STRICT:
            quality_review = await review_with_quality_council(synthesis, "strict")
            findings.append(quality_review)
            if not _passed(quality_review):
                synthesis = await _ask(
                    synthesis_agent,
                    f"Question: {task.question}\n"
                    "Revise the draft using only verified evidence and this council review. "
                    f"Keep source links near claims.\nDRAFT:\n{synthesis}\nCOUNCIL:\n{quality_review}",
                )
                final_quality_review = await review_with_quality_council(synthesis, "strict")
                findings.append(final_quality_review)
                quality_blocked = not _passed(final_quality_review)
    except Exception:
        logger.exception("Research pipeline failed")
        outcome = WorkforceOutcome(
            status=OutcomeStatus.FAILED,
            summary="Research pipeline failed. Inspect the server trace with the run id for diagnostic details.",
        )
        return StepOutput(content=outcome.model_dump(mode="json"), success=False)
    outcome = WorkforceOutcome(
        status=(
            OutcomeStatus.INSUFFICIENT_EVIDENCE
            if not verified
            else OutcomeStatus.BLOCKED
            if quality_blocked
            else OutcomeStatus.COMPLETED
        ),
        summary=synthesis,
        delegated_to=("search-agent", "web-research-agent", "source-verifier", "synthesis-agent"),
        findings=tuple(findings),
        degraded_capabilities=capabilities.degraded,
    )
    return StepOutput(content=outcome.model_dump(mode="json"), success=verified and not quality_blocked)


research_pipeline = Workflow(
    id="research-pipeline",
    name="Research Pipeline",
    description="Iterative web research with source verification before synthesis.",
    db=get_postgres_db(),
    input_schema=ResearchTaskInput,
    steps=[Step(name="research-pipeline", executor=research_pipeline_step, max_retries=0)],
)
