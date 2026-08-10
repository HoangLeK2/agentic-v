"""Allowlisted disposable workspace lifecycle."""

import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from services.workspace_executor.config import RepoProfile
from services.workspace_executor.runner import CheckResult, WorkspaceRunner


@dataclass(frozen=True)
class Workspace:
    id: str
    repo_id: str
    path: Path
    source_fingerprint: str


class WorkspaceManager:
    def __init__(
        self,
        profiles: dict[str, RepoProfile],
        root: Path | None = None,
        sandbox_host_root: Path | None = None,
    ):
        self._profiles = profiles
        self._root = (root or Path(tempfile.gettempdir()) / "workforce-workspaces").resolve()
        if self._root == Path(self._root.anchor):
            raise ValueError("Workspace root cannot be a filesystem root")
        self._sandbox_host_root = sandbox_host_root.resolve() if sandbox_host_root else None
        self._root.mkdir(parents=True, exist_ok=True)
        for orphan in self._root.iterdir():
            if orphan.is_dir() and not orphan.is_symlink():
                shutil.rmtree(orphan)
        self._workspaces: dict[str, Workspace] = {}
        self._successful_checks: dict[str, dict[str, str]] = {}
        self._review_grants: dict[str, str] = {}
        self._approved_digests: dict[str, str] = {}
        self._used_approval_nonces: dict[str, int] = {}
        self._operation_locks: dict[str, threading.RLock] = {}
        self._repo_locks = {repo_id: threading.RLock() for repo_id in profiles}
        self._lock = threading.RLock()

    def list_repositories(self) -> list[dict[str, object]]:
        """Return the repository catalog without exposing host paths."""
        return [
            {
                "repo_id": profile.repo_id,
                "aliases": sorted(
                    {
                        profile.repo_id,
                        profile.repo_id.replace("-", "_"),
                        profile.repo_id.replace("-", " "),
                    }
                ),
                "check_ids": sorted(profile.checks),
                "write_policy": profile.write_policy,
            }
            for profile in sorted(self._profiles.values(), key=lambda item: item.repo_id)
        ]

    def create(self, repo_id: str) -> Workspace:
        profile = self._profiles.get(repo_id)
        if profile is None:
            raise ValueError(f"Unknown repo_id: {repo_id}")
        if not profile.sandbox_image:
            raise ValueError(f"Allowlisted repository {repo_id} must pin a sandbox_image")
        if not (profile.source_path / ".git").exists():
            raise ValueError(f"Allowlisted source for {repo_id} is not a Git repository")

        source_runner = WorkspaceRunner(profile=profile, workspace=profile.source_path)
        source_fingerprint = source_runner.repository_fingerprint()
        workspace_id = uuid.uuid4().hex
        destination = self._root / workspace_id
        completed = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "uploadpack.packObjectsHook=",
                "-c",
                "protocol.ext.allow=never",
                "clone",
                "--no-local",
                "--no-hardlinks",
                str(profile.source_path),
                str(destination),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            shell=False,
        )
        if completed.returncode != 0:
            shutil.rmtree(destination, ignore_errors=True)
            raise RuntimeError(completed.stderr.strip() or "Could not create workspace")

        try:
            self._copy_worktree_changes(profile.source_path, destination)
            self._commit_snapshot_baseline(destination)
            if source_runner.repository_fingerprint() != source_fingerprint:
                raise RuntimeError("Source repository changed while the workspace was opening; retry")
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise

        if profile.sandbox_image and os.geteuid() == 0:
            for path in (destination, *destination.rglob("*")):
                if not path.is_symlink():
                    os.chown(path, 65532, 65532)

        workspace = Workspace(
            id=workspace_id,
            repo_id=repo_id,
            path=destination,
            source_fingerprint=source_fingerprint,
        )
        with self._lock:
            self._workspaces[workspace_id] = workspace
            self._successful_checks[workspace_id] = {}
            self._operation_locks[workspace_id] = threading.RLock()
        return workspace

    def get(self, workspace_id: str) -> tuple[Workspace, WorkspaceRunner]:
        if not workspace_id or not workspace_id.isalnum():
            raise ValueError("Invalid workspace_id")
        with self._lock:
            workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            raise ValueError("Unknown or closed workspace_id")
        profile = self._profiles[workspace.repo_id]
        sandbox_source_path = (
            self._sandbox_host_root / workspace.id if self._sandbox_host_root is not None else workspace.path
        )
        return workspace, WorkspaceRunner(
            profile=profile,
            workspace=workspace.path,
            sandbox_source_path=sandbox_source_path,
        )

    @contextmanager
    def operation(self, workspace_id: str) -> Iterator[tuple[Workspace, WorkspaceRunner]]:
        """Serialize every stateful operation for one disposable workspace."""
        if not workspace_id or not workspace_id.isalnum():
            raise ValueError("Invalid workspace_id")
        with self._lock:
            operation_lock = self._operation_locks.get(workspace_id)
        if operation_lock is None:
            raise ValueError("Unknown or closed workspace_id")
        with operation_lock:
            yield self.get(workspace_id)

    def close(self, workspace_id: str) -> None:
        with self._lock:
            operation_lock = self._operation_locks.get(workspace_id)
        if operation_lock is None:
            raise ValueError("Unknown or already closed workspace_id")
        with operation_lock:
            with self._lock:
                workspace = self._workspaces.pop(workspace_id, None)
                self._successful_checks.pop(workspace_id, None)
                self._review_grants.pop(workspace_id, None)
                self._approved_digests.pop(workspace_id, None)
                self._operation_locks.pop(workspace_id, None)
            if workspace is None:
                raise ValueError("Unknown or already closed workspace_id")
            shutil.rmtree(workspace.path, ignore_errors=False)

    def record_check(self, workspace_id: str, check_id: str, *, success: bool, digest: str) -> None:
        workspace, _ = self.get(workspace_id)
        if check_id not in self._profiles[workspace.repo_id].checks:
            raise ValueError(f"Unknown check_id: {check_id}")
        with self._lock:
            passed = self._successful_checks.setdefault(workspace_id, {})
            if success:
                passed[check_id] = digest
            else:
                passed.pop(check_id, None)
            self._review_grants.pop(workspace_id, None)

    def invalidate_checks(self, workspace_id: str) -> None:
        self.get(workspace_id)
        with self._lock:
            self._successful_checks[workspace_id] = {}
            self._review_grants.pop(workspace_id, None)
            self._approved_digests.pop(workspace_id, None)

    def apply_workspace_patch(
        self,
        workspace_id: str,
        patch_text: str,
        *,
        trusted: bool,
        approval_nonce: str | None = None,
        approval_expires_at: int | None = None,
    ) -> CheckResult:
        with self.operation(workspace_id) as (workspace, runner):
            if trusted and runner.profile.write_policy != "trusted":
                raise PermissionError(f"Repository {workspace.repo_id!r} requires approval before writes")
            if not trusted:
                if not approval_nonce or approval_expires_at is None:
                    raise PermissionError("Repository patch requires proof of human approval")
                now = int(time.time())
                with self._lock:
                    self._used_approval_nonces = {
                        nonce: expiry for nonce, expiry in self._used_approval_nonces.items() if expiry >= now
                    }
                    if approval_expires_at < now:
                        raise PermissionError("Repository patch approval has expired")
                    if approval_nonce in self._used_approval_nonces:
                        raise PermissionError("Repository patch approval has already been used")
                    self._used_approval_nonces[approval_nonce] = approval_expires_at
            result = runner.apply_patch(patch_text)
            if result.success:
                self.invalidate_checks(workspace_id)
                if not trusted:
                    with self._lock:
                        self._approved_digests[workspace_id] = runner.workspace_digest()
            return result

    def run_check(self, workspace_id: str, check_id: str) -> CheckResult:
        with self.operation(workspace_id) as (_workspace, runner):
            before_state = runner.workspace_state_digest()
            result = runner.run_check(check_id)
            after_state = runner.workspace_state_digest()
            stable = before_state == after_state
            publishable_digest = runner.workspace_digest()
            self.record_check(
                workspace_id,
                check_id,
                success=result.success and stable,
                digest=publishable_digest,
            )
            if result.success and not stable:
                return CheckResult(
                    check_id=check_id,
                    success=False,
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    error="Check modified the publishable workspace; rerun all checks on a stable diff",
                )
            return result

    def grant_publish(self, workspace_id: str, verdict: str) -> dict[str, object]:
        with self.operation(workspace_id) as (workspace, runner):
            if verdict.strip() != "VERDICT: PASS":
                raise ValueError("Reviewer grant requires the exact verdict: VERDICT: PASS")
            digest = runner.workspace_digest()
            with self._lock:
                self._review_grants[workspace_id] = digest
            return {"granted": True, "repo_id": workspace.repo_id, "diff_digest": digest}

    def publish_changes(self, workspace_id: str) -> dict[str, object]:
        with self.operation(workspace_id) as (workspace, runner):
            with self._repo_locks[workspace.repo_id]:
                profile = runner.profile
                diff = runner.git_diff()
                if not diff:
                    return {"published": False, "repo_id": workspace.repo_id, "reason": "Workspace diff is empty"}
                digest = runner.workspace_digest()
                with self._lock:
                    passed = self._successful_checks.get(workspace_id, {}).copy()
                    review_digest = self._review_grants.get(workspace_id)
                    approved_digest = self._approved_digests.get(workspace_id)
                missing = sorted(check_id for check_id in profile.checks if passed.get(check_id) != digest)
                if missing:
                    raise RuntimeError(
                        f"Publish requires passing checks on the current diff: {', '.join(missing)}"
                    )
                if review_digest != digest:
                    raise RuntimeError("Publish requires an independent reviewer grant for the current diff")
                if profile.write_policy == "approval_required" and approved_digest != digest:
                    raise RuntimeError("Publishing this repository requires human approval for the current diff")
                source_runner = WorkspaceRunner(profile=profile, workspace=profile.source_path)
                if source_runner.repository_fingerprint() != workspace.source_fingerprint:
                    raise RuntimeError("Source repository changed after this workspace opened; reopen and revalidate")
                result = source_runner.apply_patch(diff)
                if not result.success:
                    raise RuntimeError(result.stderr.strip() or result.error or "Trusted patch could not be published")
                return {"published": True, "repo_id": workspace.repo_id, "diff": diff, "diff_digest": digest}

    @staticmethod
    def _copy_worktree_changes(source: Path, destination: Path) -> None:
        diff = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "diff",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
                "HEAD",
                "--",
            ],
            cwd=source,
            capture_output=True,
            timeout=60,
            shell=False,
        )
        if diff.returncode != 0:
            raise RuntimeError(diff.stderr.decode(errors="replace").strip() or "Could not snapshot tracked changes")
        if diff.stdout:
            applied = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", "-"],
                cwd=destination,
                input=diff.stdout,
                capture_output=True,
                timeout=60,
                shell=False,
            )
            if applied.returncode != 0:
                raise RuntimeError(applied.stderr.decode(errors="replace").strip() or "Could not apply tracked changes")

        untracked = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            cwd=source,
            capture_output=True,
            timeout=30,
            shell=False,
        )
        if untracked.returncode != 0:
            raise RuntimeError(untracked.stderr.decode(errors="replace").strip() or "Could not list untracked files")
        for encoded_path in untracked.stdout.split(b"\0"):
            if not encoded_path:
                continue
            relative_path = Path(os.fsdecode(encoded_path))
            source_path = source / relative_path
            destination_path = destination / relative_path
            if source_path.is_file() and not source_path.is_symlink():
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination_path)

    @staticmethod
    def _commit_snapshot_baseline(destination: Path) -> None:
        commands = (
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "add",
                "--all",
                "--",
                ".",
            ],
            [
                "git",
                "-c",
                "user.name=Workforce Snapshot",
                "-c",
                "user.email=workforce@localhost.invalid",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "--no-verify",
                "--allow-empty",
                "-m",
                "workforce snapshot baseline",
            ],
        )
        for argv in commands:
            completed = subprocess.run(
                argv,
                cwd=destination,
                capture_output=True,
                text=True,
                timeout=60,
                shell=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr.strip() or "Could not create the workspace baseline")
