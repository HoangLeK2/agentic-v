"""Engineering specialists and their public coordinating team."""

from agno.team.mode import TeamMode

from agents.workforce.common import lead_learning, specialist, workforce_session_summary_manager, workspace_tools
from agents.workforce.prompt_provenance import grounded_team_instructions
from app.settings import ModelRole, model_for
from db import get_postgres_db
from workforce.capabilities import list_operation_capabilities
from workforce.delegation import DomainBoundaryTeam
from workforce.learning import propose_learning_candidate
from workforce.runtime_tools import list_workspace_repositories, run_engineering_delivery

READ_TOOLS = ("list_files", "read_file", "search_code", "git_status", "git_diff")
PATCH_TOOLS = (*READ_TOOLS, "apply_patch", "apply_trusted_patch")
TEST_TOOLS = (*READ_TOOLS, "run_check")
REVIEW_TOOLS = (*READ_TOOLS, "grant_publish")
LEAD_WORKSPACE_TOOLS = (
    "list_repositories",
    "open_repository",
    *READ_TOOLS,
    "publish_changes",
    "close_workspace",
)

engineering_lead = specialist(
    agent_id="engineering-lead",
    name="Engineering Lead",
    role="Route engineering work and enforce the delivery workflow.",
    instructions=(
        "Classify the request as architecture advice, implementation, audit, or operations. "
        "When repository evidence reveals a fixable defect, own the remediation end to end instead of explaining "
        "how the user could fix it. "
        "Check operation capabilities before delegating. Open a repository with open_repository(repo_id), then use "
        "only the returned workspace_id for delegated file, Git, check, and patch operations. Inspect and cite "
        "repository evidence before editing. Give Code or Fixer the workspace_id and write_policy; they choose the "
        "policy-specific patch tool. Then require Tester to run every named check and Reviewer to inspect the current "
        "diff and grant only an exact VERDICT: PASS. Publish only after both gates pass; approval_required patches "
        "must also carry the user's confirmation for the current diff. A trusted repository never pauses for user "
        "implementation: continue through patch, checks, review, and publish. Then "
        "close the workspace. The write-capable operation is code.sandbox_write; do not look for a separate "
        "engineering.* write operation. Never pass a repo_id as workspace_id or bypass the declared policy."
    ),
    model_role=ModelRole.ENGINEERING,
    tools=workspace_tools(*LEAD_WORKSPACE_TOOLS),
    learning=lead_learning("engineering"),
)

code_agent = specialist(
    agent_id="code-agent",
    name="Code Agent",
    role="Implement focused changes inside an isolated workspace.",
    instructions=(
        "Use only a workspace_id returned by open_repository or supplied by the Engineering Lead. Read before editing "
        "and make the smallest coherent patch. Use apply_trusted_patch only when write_policy=trusted; otherwise use "
        "apply_patch and wait for confirmation. Return the diff and checks; never review your own patch or claim tests "
        "passed without executor output. Leave checks, review grants, and publishing to their assigned roles."
    ),
    model_role=ModelRole.ENGINEERING,
    tools=workspace_tools(*PATCH_TOOLS, requires_confirmation_tools=("apply_patch",)),
)

tester_agent = specialist(
    agent_id="tester-agent",
    name="Tester Agent",
    role="Run allowlisted checks and report reproducible failures.",
    instructions=(
        "Run only check IDs exposed by the workspace executor. Report evidence: check id, exit status, "
        "relevant output, and whether the result is baseline or post-change. Never edit files."
    ),
    model_role=ModelRole.ENGINEERING,
    tools=workspace_tools(*TEST_TOOLS),
)

reviewer_agent = specialist(
    agent_id="reviewer-agent",
    name="Reviewer Agent",
    role="Review correctness, regressions, and missing tests independently.",
    instructions=(
        "Inspect the task, current diff, and Tester evidence. Findings lead, ordered by severity and grounded in "
        "files. "
        "Do not edit or run checks and do not approve work merely because tests are green. For every publishable "
        "repository, call grant_publish with exactly VERDICT: PASS only when no blocking finding remains; otherwise "
        "return VERDICT: FIX_REQUIRED and do not grant."
    ),
    model_role=ModelRole.REVIEW,
    tools=workspace_tools(*REVIEW_TOOLS),
)

fixer_agent = specialist(
    agent_id="fixer-agent",
    name="Fixer Agent",
    role="Apply reviewer-approved fixes without widening scope.",
    instructions=(
        "Address only concrete findings from independent reviewers in the supplied workspace_id. Use "
        "apply_trusted_patch only when write_policy=trusted; otherwise use apply_patch and wait for confirmation. "
        "Preserve unrelated changes, return the new diff, and leave checks, review grants, and publishing to their "
        "assigned roles."
    ),
    model_role=ModelRole.ENGINEERING,
    tools=workspace_tools(*PATCH_TOOLS, requires_confirmation_tools=("apply_patch",)),
)

security_agent = specialist(
    agent_id="security-agent",
    name="Security Agent",
    role="Review security-sensitive code and permission boundaries.",
    instructions=(
        "Review authentication, authorization, secrets, command execution, deserialization, dependencies, and network "
        "boundaries. Never mutate code and never report a scanner run unless a named check produced evidence."
    ),
    model_role=ModelRole.REVIEW,
    tools=workspace_tools(*READ_TOOLS),
)

architect_agent = specialist(
    agent_id="architect-agent",
    name="Architect Agent",
    role="Evaluate component boundaries, contracts, and compatibility.",
    instructions="Read the relevant code and propose the minimum architecture that preserves existing contracts.",
    model_role=ModelRole.ENGINEERING,
    tools=workspace_tools(*READ_TOOLS),
)

database_agent = specialist(
    agent_id="database-agent",
    name="Database Agent",
    role="Review schemas, queries, transactions, migrations, and data safety.",
    instructions="Ground database advice in the repository schema and preserve rollback and compatibility paths.",
    model_role=ModelRole.ENGINEERING,
    tools=workspace_tools(*READ_TOOLS),
)

performance_agent = specialist(
    agent_id="performance-agent",
    name="Performance Agent",
    role="Diagnose measurable performance problems.",
    instructions="Require a baseline and production-shaped evidence before recommending optimization.",
    model_role=ModelRole.ENGINEERING,
    tools=workspace_tools(*READ_TOOLS),
)

sre_agent = specialist(
    agent_id="sre-agent",
    name="SRE Agent",
    role="Observe and diagnose runtime behavior within configured permissions.",
    instructions=(
        "Check production.observe before claiming access to logs or metrics. Local diagnostics may run when available; "
        "production writes and deletes are unavailable in Workforce v1."
    ),
    model_role=ModelRole.ENGINEERING,
)

engineering_team = DomainBoundaryTeam(
    id="engineering-team",
    name="Engineering Team",
    mode=TeamMode.coordinate,
    model=model_for(ModelRole.ENGINEERING),
    db=get_postgres_db(),
    members=[
        engineering_lead,
        code_agent,
        tester_agent,
        reviewer_agent,
        fixer_agent,
        security_agent,
        architect_agent,
        database_agent,
        performance_agent,
        sre_agent,
    ],
    tools=[
        list_operation_capabilities,
        list_workspace_repositories,
        run_engineering_delivery,
        propose_learning_candidate,
    ],
    enable_session_summaries=True,
    session_summary_manager=workforce_session_summary_manager(),
    add_session_summary_to_context=True,
    instructions=grounded_team_instructions(
        "engineering-team",
        [
            "Delegate only the members required for the request; never broadcast to every member.",
            "For an internal project audit, treat repository evidence as authoritative and set apply_fixes=true unless "
            "the user explicitly asks for read-only findings.",
            "For a feature request or bug fix in an allowlisted repository, call run_engineering_delivery with "
            "intent=implement, apply_fixes=true, and execution_mode='standard' before considering direct specialist "
            "delegation. Use execution_mode='fast' for tiny safe edits and execution_mode='deep' only for large "
            "features, security-sensitive rewrites, or explicit deep work.",
            "Engineering write readiness is code.sandbox_write plus the repository write_policy returned by "
            "open_repository. Do not require a nonexistent engineering.* operation.",
            "Any confirmed fixable defect is an implementation request: delegate to Engineering Lead with the exact "
            "repo_id so the change can be published, even when the defect was discovered during an audit. The lead "
            "opens the workspace and coordinates its write_policy: trusted writes proceed; approval_required writes "
            "pause.",
            "Code Agent never reviews itself. Tester alone runs named checks, Reviewer alone grants publish, "
            "and Engineering Lead publishes only after both server-side gates pass.",
            "Fail closed when a required operation is unavailable.",
            "Never hand the user code-editing or command-running steps that assigned tools can perform. Continue "
            "autonomously until published, or report the exact capability, approval, or failed quality gate "
            "blocking it.",
            "Return the engineering artifact and preserve workflow status and evidence. "
            "The public router normalizes the final outcome.",
        ],
    ),
    add_datetime_to_context=True,
    add_history_to_context=False,
    show_members_responses=True,
    markdown=True,
)
