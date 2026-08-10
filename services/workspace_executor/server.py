"""Private MCP surface for fixed workspace operations."""

import os
from functools import cache
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier

from services.workspace_executor.approval import WorkspaceApprovalVerifier
from services.workspace_executor.config import load_repo_profiles
from services.workspace_executor.manager import WorkspaceManager


def _auth() -> StaticTokenVerifier | None:
    token = os.getenv("WORKSPACE_EXECUTOR_TOKEN")
    if not token:
        if os.getenv("WORKSPACE_EXECUTOR_ALLOW_INSECURE", "").lower() == "true":
            return None
        raise RuntimeError("WORKSPACE_EXECUTOR_TOKEN is required")
    return StaticTokenVerifier(
        tokens={token: {"client_id": "agentos-workforce", "scopes": ["workspace:execute"]}},
        required_scopes=["workspace:execute"],
    )


mcp = FastMCP(
    "Workspace Executor",
    instructions="Operate only allowlisted disposable workspaces. No raw shell or remote Git operations are available.",
    auth=_auth(),
    mask_error_details=True,
)


@cache
def manager() -> WorkspaceManager:
    config_path = os.getenv("WORKSPACE_REPOS_FILE")
    if not config_path:
        raise RuntimeError("WORKSPACE_REPOS_FILE is not configured")
    root = Path(os.getenv("WORKSPACE_ROOT", str(Path(os.getenv("TMPDIR", "/tmp")) / "workforce-workspaces")))
    host_root = os.getenv("WORKSPACE_HOST_ROOT")
    return WorkspaceManager(
        load_repo_profiles(Path(config_path)),
        root=root,
        sandbox_host_root=Path(host_root) if host_root else None,
    )


@cache
def approval_verifier() -> WorkspaceApprovalVerifier:
    public_key_file = os.getenv("WORKSPACE_APPROVAL_VERIFICATION_KEY_FILE")
    if not public_key_file:
        raise RuntimeError("WORKSPACE_APPROVAL_VERIFICATION_KEY_FILE is required")
    return WorkspaceApprovalVerifier(
        Path(public_key_file),
        audience=os.getenv("WORKSPACE_APPROVAL_AUDIENCE", "workspace-executor"),
        issuer=os.getenv("WORKSPACE_APPROVAL_ISSUER", "buzz-adapter"),
    )


@mcp.tool
def list_repositories() -> list[dict[str, object]]:
    """List repository ids and fixed checks without exposing source paths."""
    return manager().list_repositories()


@mcp.tool
def create_workspace(repo_id: str) -> dict:
    """Create a disposable clone. Pass the returned workspace_id to every workspace operation."""
    workspace = manager().create(repo_id)
    return {"workspace_id": workspace.id, "repo_id": workspace.repo_id}


@mcp.tool
def open_repository(repo_id: str, pattern: str = "**/*", limit: int = 200) -> dict:
    """Open a repository and return the workspace_id, policy, checks, and initial file listing."""
    workspace = manager().create(repo_id)
    _, runner = manager().get(workspace.id)
    return {
        "workspace_id": workspace.id,
        "repo_id": workspace.repo_id,
        "write_policy": runner.profile.write_policy,
        "source_write_enabled": True,
        "write_requires_approval": runner.profile.write_policy == "approval_required",
        "check_ids": sorted(runner.profile.checks),
        "files": runner.list_files(pattern=pattern, limit=min(max(limit, 1), 500)),
    }


@mcp.tool
def list_files(workspace_id: str, pattern: str = "**/*", limit: int = 500) -> list[str]:
    """List files in a disposable workspace."""
    with manager().operation(workspace_id) as (_workspace, runner):
        return runner.list_files(pattern=pattern, limit=min(max(limit, 1), 500))


@mcp.tool
def read_file(workspace_id: str, relative_path: str) -> str:
    """Read a UTF-8 file inside a disposable workspace."""
    with manager().operation(workspace_id) as (_workspace, runner):
        return runner.read_file(relative_path)


@mcp.tool
def search_code(workspace_id: str, pattern: str, file_pattern: str = "**/*", limit: int = 200) -> list[dict]:
    """Search for a bounded literal string without executing repository code."""
    if not pattern or len(pattern) > 500:
        raise ValueError("Search pattern must contain 1 to 500 characters")
    with manager().operation(workspace_id) as (_workspace, runner):
        results: list[dict] = []
        for relative_path in runner.list_files(pattern=file_pattern, limit=500):
            try:
                content = runner.read_file(relative_path)
            except (OSError, UnicodeError):
                continue
            for line_number, line in enumerate(content.splitlines(), 1):
                if pattern in line:
                    results.append({"path": relative_path, "line": line_number, "text": line[:500]})
                    if len(results) >= min(max(limit, 1), 200):
                        return results
        return results


@mcp.tool
def apply_patch(workspace_id: str, patch_text: str, approval_token: str | None = None) -> dict:
    """Apply a unified diff after the caller's human-confirmation policy is satisfied."""
    if not approval_token:
        raise PermissionError("apply_patch requires a signed human-approval token")
    approval = approval_verifier().verify(
        approval_token,
        workspace_id=workspace_id,
        patch_text=patch_text,
    )
    return manager().apply_workspace_patch(
        workspace_id,
        patch_text,
        trusted=False,
        approval_nonce=approval.nonce,
        approval_expires_at=approval.expires_at,
    ).__dict__


@mcp.tool
def apply_trusted_patch(workspace_id: str, patch_text: str) -> dict:
    """Apply a diff without confirmation only when the repository profile is trusted."""
    return manager().apply_workspace_patch(workspace_id, patch_text, trusted=True).__dict__


@mcp.tool
def run_check(workspace_id: str, check_id: str) -> dict:
    """Run one fixed argv check selected by id from the repository profile."""
    return manager().run_check(workspace_id, check_id).__dict__


@mcp.tool
def list_checks(workspace_id: str) -> list[str]:
    """List fixed check ids available for this repository profile."""
    with manager().operation(workspace_id) as (_workspace, runner):
        return sorted(runner.profile.checks)


@mcp.tool
def git_status(workspace_id: str) -> str:
    """Return read-only short Git status."""
    with manager().operation(workspace_id) as (_workspace, runner):
        return runner.git_status()


@mcp.tool
def git_diff(workspace_id: str) -> str:
    """Return the current uncommitted diff without invoking external diff drivers."""
    with manager().operation(workspace_id) as (_workspace, runner):
        return runner.git_diff()


@mcp.tool
def grant_publish(workspace_id: str, verdict: str) -> dict[str, object]:
    """Bind an independent reviewer's exact PASS verdict to the current diff."""
    return manager().grant_publish(workspace_id, verdict)


@mcp.tool
def publish_changes(workspace_id: str) -> dict[str, object]:
    """Publish a checked, reviewed, and policy-approved diff back to its registered source repository."""
    return manager().publish_changes(workspace_id)


@mcp.tool
def close_workspace(workspace_id: str) -> dict[str, bool]:
    """Delete a disposable workspace after its artifacts have been collected."""
    manager().close(workspace_id)
    return {"closed": True}


def main() -> None:
    allowed_hosts = [
        host.strip()
        for host in os.getenv(
            "WORKSPACE_EXECUTOR_ALLOWED_HOSTS",
            "workspace-executor:8100,localhost:8100,127.0.0.1:8100",
        ).split(",")
        if host.strip()
    ]
    mcp.run(
        transport="http",
        host=os.getenv("WORKSPACE_EXECUTOR_HOST", "127.0.0.1"),
        port=int(os.getenv("WORKSPACE_EXECUTOR_PORT", "8100")),
        host_origin_protection=True,
        allowed_hosts=allowed_hosts,
        show_banner=False,
    )


if __name__ == "__main__":
    main()
