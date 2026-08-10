import subprocess
import tempfile
from importlib import import_module
from os import environ
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from services.workspace_executor.config import CheckDefinition, RepoProfile
from services.workspace_executor.manager import WorkspaceManager
from services.workspace_executor.runner import CheckResult, WorkspaceRunner


class WorkspaceRunnerTest(TestCase):
    def test_run_check_uses_fixed_argv_and_shell_false(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            profile = RepoProfile(
                repo_id="fixture",
                source_path=workspace,
                checks={"unit": CheckDefinition(argv=("python", "-m", "unittest"), timeout_seconds=30)},
                sandbox_image="fixture-tests:sha256-deadbeef",
            )
            runner = WorkspaceRunner(profile=profile, workspace=workspace)

            with patch.object(runner, "_run_bounded_check", return_value=(0, "", "")) as run:
                result = runner.run_check("unit")

        self.assertTrue(result.success)
        run.assert_called_once()
        args = run.call_args.args
        self.assertEqual(args[0][:2], ["docker", "run"])
        self.assertEqual(
            args[0][-4:],
            ["fixture-tests:sha256-deadbeef", "python", "-m", "unittest"],
        )
        self.assertEqual(args[1], 30)

    def test_unknown_check_id_is_rejected_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            profile = RepoProfile(
                repo_id="fixture",
                source_path=workspace,
                checks={},
                sandbox_image="fixture-tests:sha256-deadbeef",
            )
            runner = WorkspaceRunner(profile=profile, workspace=workspace)

            with patch.object(runner, "_run_bounded_check") as run:
                result = runner.run_check("pytest; git push")

        self.assertFalse(result.success)
        self.assertIn("Unknown check_id", result.error or "")
        run.assert_not_called()

    def test_sandbox_profile_wraps_check_with_fixed_container_security_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            profile = RepoProfile(
                repo_id="fixture",
                source_path=workspace,
                checks={"unit": CheckDefinition(argv=("pytest", "-q"))},
                sandbox_image="fixture-tests:sha256-deadbeef",
            )
            runner = WorkspaceRunner(profile=profile, workspace=workspace)

            with patch.object(runner, "_run_bounded_check", return_value=(0, "", "")) as run:
                runner.run_check("unit")

        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["docker", "run"])
        self.assertIn("--network=none", argv)
        self.assertIn("--read-only", argv)
        self.assertIn("--cap-drop=ALL", argv)
        self.assertIn("--security-opt=no-new-privileges", argv)
        self.assertIn("--ulimit=fsize=8388608:8388608", argv)
        self.assertIn(f"--volume={workspace.resolve() / '.git'}:/workspace/.git:ro", argv)
        self.assertTrue(any(argument.startswith("--cidfile=") for argument in argv))
        self.assertEqual(argv[-3:], ["fixture-tests:sha256-deadbeef", "pytest", "-q"])

    def test_patch_size_is_bounded_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            profile = RepoProfile(
                repo_id="fixture",
                source_path=workspace,
                checks={},
                sandbox_image="fixture-tests:sha256-deadbeef",
            )
            runner = WorkspaceRunner(profile=profile, workspace=workspace)

            with patch("services.workspace_executor.runner.subprocess.run") as run:
                result = runner.apply_patch("x" * 1_000_001)

        self.assertFalse(result.success)
        self.assertIn("1 MB", result.error or "")
        run.assert_not_called()

    def test_timeout_removes_started_sandbox_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            profile = RepoProfile(
                repo_id="fixture",
                source_path=workspace,
                checks={"unit": CheckDefinition(argv=("pytest", "-q"), timeout_seconds=1)},
                sandbox_image="fixture-tests:sha256-deadbeef",
            )
            runner = WorkspaceRunner(profile=profile, workspace=workspace)

            def run_side_effect(argv, _timeout):
                cidfile = next(
                    Path(argument.removeprefix("--cidfile=")) for argument in argv if argument.startswith("--cidfile=")
                )
                cidfile.write_text("a" * 64, encoding="ascii")
                raise subprocess.TimeoutExpired(argv, 1)

            with (
                patch.object(runner, "_run_bounded_check", side_effect=run_side_effect),
                patch("services.workspace_executor.runner.subprocess.run") as cleanup,
            ):
                result = runner.run_check("unit")

        self.assertFalse(result.success)
        self.assertIn("timed out", result.error or "")
        cleanup_argv = cleanup.call_args.args[0]
        self.assertEqual(cleanup_argv, ["docker", "rm", "--force", "a" * 64])

    def test_output_limit_removes_started_sandbox_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            profile = RepoProfile(
                repo_id="fixture",
                source_path=workspace,
                checks={"unit": CheckDefinition(argv=("pytest", "-q"))},
                sandbox_image="fixture-tests:sha256-deadbeef",
            )
            runner = WorkspaceRunner(profile=profile, workspace=workspace)

            def run_side_effect(argv, _timeout):
                cidfile = next(
                    Path(argument.removeprefix("--cidfile=")) for argument in argv if argument.startswith("--cidfile=")
                )
                cidfile.write_text("b" * 64, encoding="ascii")
                raise OverflowError

            with (
                patch.object(runner, "_run_bounded_check", side_effect=run_side_effect),
                patch("services.workspace_executor.runner.subprocess.run") as cleanup,
            ):
                result = runner.run_check("unit")

        self.assertFalse(result.success)
        self.assertIn("output exceeded", result.error or "")
        self.assertEqual(cleanup.call_args.args[0], ["docker", "rm", "--force", "b" * 64])

    def test_bounded_process_stops_untrusted_output_at_hard_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            profile = RepoProfile(
                repo_id="fixture",
                source_path=workspace,
                checks={},
                sandbox_image="fixture-tests:sha256-deadbeef",
            )
            runner = WorkspaceRunner(profile=profile, workspace=workspace)

            with self.assertRaises(OverflowError):
                runner._run_bounded_check(["python", "-c", "print('x' * 100001)"], 5)

    def test_file_access_cannot_escape_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            profile = RepoProfile(
                repo_id="fixture",
                source_path=workspace,
                checks={},
                sandbox_image="fixture-tests:sha256-deadbeef",
            )
            runner = WorkspaceRunner(profile=profile, workspace=workspace)

            with self.assertRaises(ValueError):
                runner.read_file("../secret")
            with self.assertRaisesRegex(ValueError, "Git metadata"):
                runner.read_file(".git/config")

    def test_git_reads_scope_safe_directory_to_current_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            profile = RepoProfile(
                repo_id="fixture",
                source_path=workspace,
                checks={},
                sandbox_image="fixture-tests:sha256-deadbeef",
            )
            runner = WorkspaceRunner(profile=profile, workspace=workspace)

            with patch.object(runner, "_run_bounded_process", return_value=(0, "", "")) as run:
                runner.git_status()

        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["git", "-c", f"safe.directory={workspace.resolve()}"])
        self.assertIn("core.fsmonitor=false", argv)
        self.assertIn("core.hooksPath=/dev/null", argv)
        self.assertEqual(argv[-2:], ["status", "--short"])

    def test_large_diff_fails_explicitly_instead_of_returning_a_truncated_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            (workspace / "base.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "base.txt"], cwd=workspace, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Workforce Test",
                    "-c",
                    "user.email=workforce@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                cwd=workspace,
                check=True,
            )
            (workspace / "large.txt").write_text("x" * 5_000_001, encoding="utf-8")
            profile = RepoProfile(
                repo_id="fixture",
                source_path=workspace,
                checks={},
                sandbox_image="fixture-tests:sha256-deadbeef",
            )
            runner = WorkspaceRunner(profile=profile, workspace=workspace)

            with self.assertRaisesRegex(RuntimeError, "5000000 byte limit"):
                runner.git_diff()

    def test_repository_fingerprint_uses_canonical_untracked_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            (workspace / "base.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "base.txt"], cwd=workspace, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Workforce Test",
                    "-c",
                    "user.email=workforce@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                cwd=workspace,
                check=True,
            )
            profile = RepoProfile(
                repo_id="fixture",
                source_path=workspace,
                checks={},
                sandbox_image="fixture-tests:sha256-deadbeef",
            )
            runner = WorkspaceRunner(profile=profile, workspace=workspace)
            (workspace / "a").write_text("bc", encoding="utf-8")
            first = runner.repository_fingerprint()
            (workspace / "a").unlink()
            (workspace / "ab").write_text("c", encoding="utf-8")
            second = runner.repository_fingerprint()

            self.assertNotEqual(first, second)


class WorkspaceManagerTest(TestCase):
    def test_startup_removes_orphaned_workspace_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspaces"
            orphan = root / "orphaned-workspace"
            orphan.mkdir(parents=True)
            (orphan / "leftover.py").write_text("leftover", encoding="utf-8")

            WorkspaceManager({}, root=root)

            self.assertFalse(orphan.exists())

    def test_workspace_root_cannot_be_filesystem_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "filesystem root"):
            WorkspaceManager({}, root=Path("/"))

    def test_repository_catalog_does_not_expose_source_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            profile = RepoProfile(
                repo_id="device-farm",
                source_path=source,
                checks={"repository-structure": CheckDefinition(argv=("python", "-V"))},
                sandbox_image="agentos:latest",
            )
            manager = WorkspaceManager({"device-farm": profile}, root=source / "workspaces")

            self.assertEqual(
                manager.list_repositories(),
                [
                    {
                        "repo_id": "device-farm",
                        "aliases": ["device farm", "device-farm", "device_farm"],
                        "check_ids": ["repository-structure"],
                        "write_policy": "approval_required",
                    }
                ],
            )

    def test_create_snapshots_tracked_and_untracked_worktree_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            workspace_root = base / "workspaces"
            host_root = base / "host-workspaces"
            source.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=source, check=True)
            (source / "tracked.txt").write_text("committed\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=source, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Workforce Test",
                    "-c",
                    "user.email=workforce@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                cwd=source,
                check=True,
            )
            (source / "tracked.txt").write_text("modified\n", encoding="utf-8")
            (source / "untracked.txt").write_text("new\n", encoding="utf-8")
            profile = RepoProfile(
                repo_id="fixture",
                source_path=source,
                checks={},
                sandbox_image="fixture-tests:sha256-deadbeef",
            )
            manager = WorkspaceManager(
                {"fixture": profile},
                root=workspace_root,
                sandbox_host_root=host_root,
            )

            workspace = manager.create("fixture")
            _, runner = manager.get(workspace.id)

            self.assertEqual((workspace.path / "tracked.txt").read_text(), "modified\n")
            self.assertEqual((workspace.path / "untracked.txt").read_text(), "new\n")
            self.assertEqual(runner.git_status(), "")
            self.assertEqual(runner.sandbox_source_path, (host_root / workspace.id).resolve())
            (workspace.path / "tracked.txt").write_text("agent edit\n", encoding="utf-8")
            (workspace.path / "agent-created.txt").write_text("created\n", encoding="utf-8")
            diff = runner.git_diff()
            self.assertIn("-modified", diff)
            self.assertIn("+agent edit", diff)
            self.assertIn("agent-created.txt", diff)
            manager.close(workspace.id)
            self.assertFalse(workspace.path.exists())

    def test_trusted_publish_requires_all_checks_and_updates_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=source, check=True)
            (source / "tracked.txt").write_text("original\n", encoding="utf-8")
            (source / ".gitignore").write_text("*.cache\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt", ".gitignore"], cwd=source, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Workforce Test",
                    "-c",
                    "user.email=workforce@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                cwd=source,
                check=True,
            )
            profile = RepoProfile(
                repo_id="agentos-railway",
                source_path=source,
                checks={"unit": CheckDefinition(argv=("python", "-V"))},
                sandbox_image="fixture-tests:sha256-deadbeef",
                write_policy="trusted",
            )
            manager = WorkspaceManager({profile.repo_id: profile}, root=base / "workspaces")
            workspace = manager.create(profile.repo_id)
            (workspace.path / "tracked.txt").write_text("published\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "unit"):
                manager.publish_changes(workspace.id)

            _, runner = manager.get(workspace.id)
            digest = runner.workspace_digest()
            manager.record_check(workspace.id, "unit", success=True, digest=digest)
            with self.assertRaisesRegex(RuntimeError, "reviewer grant"):
                manager.publish_changes(workspace.id)

            with self.assertRaisesRegex(ValueError, "exact verdict"):
                manager.grant_publish(workspace.id, "Looks good")
            manager.grant_publish(workspace.id, "VERDICT: PASS")
            result = manager.publish_changes(workspace.id)

            self.assertTrue(result["published"])
            self.assertEqual((source / "tracked.txt").read_text(encoding="utf-8"), "published\n")

    def test_check_that_mutates_workspace_cannot_satisfy_publish_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=source, check=True)
            (source / "tracked.txt").write_text("original\n", encoding="utf-8")
            (source / ".gitignore").write_text("*.cache\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt", ".gitignore"], cwd=source, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Workforce Test",
                    "-c",
                    "user.email=workforce@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                cwd=source,
                check=True,
            )
            profile = RepoProfile(
                repo_id="agentos-railway",
                source_path=source,
                checks={"unit": CheckDefinition(argv=("python", "-V"))},
                sandbox_image="fixture-tests:sha256-deadbeef",
                write_policy="trusted",
            )
            manager = WorkspaceManager({profile.repo_id: profile}, root=base / "workspaces")
            workspace = manager.create(profile.repo_id)
            (workspace.path / "tracked.txt").write_text("agent change\n", encoding="utf-8")

            def mutate_during_check(runner: WorkspaceRunner, _check_id: str) -> CheckResult:
                (runner.workspace / "test-artifact.cache").write_text("ignored mutation\n", encoding="utf-8")
                return CheckResult(check_id="unit", success=True, returncode=0)

            with patch.object(WorkspaceRunner, "run_check", autospec=True, side_effect=mutate_during_check):
                result = manager.run_check(workspace.id, "unit")

            self.assertFalse(result.success)
            self.assertIn("modified the publishable workspace", result.error or "")
            manager.grant_publish(workspace.id, "VERDICT: PASS")
            with self.assertRaisesRegex(RuntimeError, "unit"):
                manager.publish_changes(workspace.id)

    def test_trusted_publish_rejects_source_drift_after_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=source, check=True)
            (source / "tracked.txt").write_text("original\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=source, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Workforce Test",
                    "-c",
                    "user.email=workforce@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                cwd=source,
                check=True,
            )
            profile = RepoProfile(
                repo_id="agentos-railway",
                source_path=source,
                checks={"unit": CheckDefinition(argv=("python", "-V"))},
                sandbox_image="fixture-tests:sha256-deadbeef",
                write_policy="trusted",
            )
            manager = WorkspaceManager({profile.repo_id: profile}, root=base / "workspaces")
            workspace = manager.create(profile.repo_id)
            (workspace.path / "tracked.txt").write_text("agent change\n", encoding="utf-8")
            _, runner = manager.get(workspace.id)
            digest = runner.workspace_digest()
            manager.record_check(workspace.id, "unit", success=True, digest=digest)
            manager.grant_publish(workspace.id, "VERDICT: PASS")
            (source / "concurrent.txt").write_text("user change\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "Source repository changed"):
                manager.publish_changes(workspace.id)

            self.assertEqual((source / "tracked.txt").read_text(encoding="utf-8"), "original\n")

    def test_approval_required_repository_publishes_only_the_human_approved_diff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=source, check=True)
            (source / "tracked.txt").write_text("original\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=source, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Workforce Test",
                    "-c",
                    "user.email=workforce@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                cwd=source,
                check=True,
            )
            profile = RepoProfile(
                repo_id="device-farm",
                source_path=source,
                checks={"unit": CheckDefinition(argv=("python", "-V"))},
                sandbox_image="fixture-tests:sha256-deadbeef",
            )
            manager = WorkspaceManager({profile.repo_id: profile}, root=base / "workspaces")
            workspace = manager.create(profile.repo_id)
            (workspace.path / "tracked.txt").write_text("blocked\n", encoding="utf-8")
            _, runner = manager.get(workspace.id)
            digest = runner.workspace_digest()
            manager.record_check(workspace.id, "unit", success=True, digest=digest)
            manager.grant_publish(workspace.id, "VERDICT: PASS")

            with self.assertRaisesRegex(RuntimeError, "human approval"):
                manager.publish_changes(workspace.id)

            self.assertEqual((source / "tracked.txt").read_text(encoding="utf-8"), "original\n")
            (workspace.path / "tracked.txt").write_text("original\n", encoding="utf-8")
            patch_text = """diff --git a/tracked.txt b/tracked.txt
index 4b48dee..35a7c52 100644
--- a/tracked.txt
+++ b/tracked.txt
@@ -1 +1 @@
-original
+approved
"""
            result = manager.apply_workspace_patch(
                workspace.id,
                patch_text,
                trusted=False,
                approval_nonce="human-approval-1",
                approval_expires_at=4_102_444_800,
            )
            self.assertTrue(result.success)
            with self.assertRaisesRegex(PermissionError, "already been used"):
                manager.apply_workspace_patch(
                    workspace.id,
                    patch_text,
                    trusted=False,
                    approval_nonce="human-approval-1",
                    approval_expires_at=4_102_444_800,
                )
            digest = runner.workspace_digest()
            manager.record_check(workspace.id, "unit", success=True, digest=digest)
            manager.grant_publish(workspace.id, "VERDICT: PASS")

            published = manager.publish_changes(workspace.id)

            self.assertTrue(published["published"])
            self.assertEqual((source / "tracked.txt").read_text(encoding="utf-8"), "approved\n")

    def test_profile_rejects_empty_sandbox_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / ".git").mkdir()
            with self.assertRaisesRegex(ValueError, "must pin a sandbox_image"):
                RepoProfile(repo_id="fixture", source_path=source, checks={}, sandbox_image="")

    def test_profile_rejects_unknown_write_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "invalid write_policy"):
                RepoProfile(
                    repo_id="fixture",
                    source_path=Path(directory),
                    checks={},
                    sandbox_image="fixture-tests:sha256-deadbeef",
                    write_policy="unrestricted",  # type: ignore[arg-type]
                )

    def test_trusted_profile_requires_a_named_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "must define at least one check"):
                RepoProfile(
                    repo_id="agentos-railway",
                    source_path=Path(directory),
                    checks={},
                    sandbox_image="fixture-tests:sha256-deadbeef",
                    write_policy="trusted",
                )


class WorkspaceExecutorServerTest(TestCase):
    def test_apply_patch_requires_and_consumes_signed_approval(self) -> None:
        fake_manager = MagicMock()
        fake_manager.apply_workspace_patch.return_value = SimpleNamespace(success=True, error=None)
        verifier = MagicMock()
        verifier.verify.return_value = SimpleNamespace(nonce="approval-1", expires_at=4_102_444_800)

        with patch.dict(environ, {"WORKSPACE_EXECUTOR_TOKEN": "test-token"}):
            server = import_module("services.workspace_executor.server")
        with self.assertRaisesRegex(PermissionError, "signed human-approval token"):
            server.apply_patch("workspace-123", "approved patch")
        with (
            patch.object(server, "manager", return_value=fake_manager),
            patch.object(server, "approval_verifier", return_value=verifier),
        ):
            result = server.apply_patch("workspace-123", "approved patch", "signed-token")

        self.assertTrue(result["success"])
        verifier.verify.assert_called_once_with(
            "signed-token",
            workspace_id="workspace-123",
            patch_text="approved patch",
        )
        fake_manager.apply_workspace_patch.assert_called_once_with(
            "workspace-123",
            "approved patch",
            trusted=False,
            approval_nonce="approval-1",
            approval_expires_at=4_102_444_800,
        )

    def test_open_repository_returns_workspace_handle_and_policy(self) -> None:
        fake_manager = MagicMock()
        fake_manager.create.return_value = SimpleNamespace(id="workspace-123", repo_id="agentos-railway")
        runner = MagicMock()
        runner.profile.write_policy = "trusted"
        runner.profile.checks = {"unit-isolated": object()}
        runner.list_files.return_value = ["README.md", "app/main.py"]
        fake_manager.get.return_value = (fake_manager.create.return_value, runner)

        with patch.dict(environ, {"WORKSPACE_EXECUTOR_TOKEN": "test-token"}):
            server = import_module("services.workspace_executor.server")
        with patch.object(server, "manager", return_value=fake_manager):
            result = server.open_repository("agentos-railway")

        self.assertEqual(result["workspace_id"], "workspace-123")
        self.assertEqual(result["write_policy"], "trusted")
        self.assertTrue(result["source_write_enabled"])
        self.assertFalse(result["write_requires_approval"])
        self.assertEqual(result["check_ids"], ["unit-isolated"])
        self.assertEqual(result["files"], ["README.md", "app/main.py"])

    def test_trusted_patch_is_enforced_by_repository_policy(self) -> None:
        fake_manager = MagicMock()
        fake_manager.apply_workspace_patch.side_effect = PermissionError(
            "Repository 'device-farm' requires approval before writes"
        )

        with patch.dict(environ, {"WORKSPACE_EXECUTOR_TOKEN": "test-token"}):
            server = import_module("services.workspace_executor.server")
        with (
            patch.object(server, "manager", return_value=fake_manager),
            self.assertRaisesRegex(PermissionError, "requires approval"),
        ):
            server.apply_trusted_patch("workspace-123", "*** Begin Patch")

        fake_manager.apply_workspace_patch.assert_called_once_with(
            "workspace-123",
            "*** Begin Patch",
            trusted=True,
        )

        fake_manager.reset_mock()
        fake_manager.apply_workspace_patch.side_effect = None
        fake_manager.apply_workspace_patch.return_value = SimpleNamespace(success=True, error=None)
        with patch.object(server, "manager", return_value=fake_manager):
            result = server.apply_trusted_patch("workspace-123", "*** Begin Patch")

        self.assertTrue(result["success"])
        fake_manager.apply_workspace_patch.assert_called_once_with(
            "workspace-123",
            "*** Begin Patch",
            trusted=True,
        )

    def test_server_keeps_host_protection_with_explicit_allowlist(self) -> None:
        environment = {
            "WORKSPACE_EXECUTOR_TOKEN": "test-token",
            "WORKSPACE_EXECUTOR_ALLOWED_HOSTS": "workspace-executor:8100,executor.internal:8100",
        }
        with patch.dict(environ, environment):
            server = import_module("services.workspace_executor.server")
            with patch.object(server.mcp, "run") as run:
                server.main()

        self.assertTrue(run.call_args.kwargs["host_origin_protection"])
        self.assertEqual(
            run.call_args.kwargs["allowed_hosts"],
            ["workspace-executor:8100", "executor.internal:8100"],
        )
