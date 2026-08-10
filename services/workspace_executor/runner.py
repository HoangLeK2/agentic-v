"""Fixed-operation runner used inside a disposable workspace."""

import os
import re
import selectors
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, BinaryIO, cast

from services.workspace_executor.config import RepoProfile


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    success: bool
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


class WorkspaceRunner:
    MAX_DIFF_BYTES = 5_000_000
    MAX_CHECK_OUTPUT_BYTES = 100_000

    def __init__(self, profile: RepoProfile, workspace: Path, sandbox_source_path: Path | None = None):
        self.profile = profile
        self.workspace = workspace.resolve()
        self.sandbox_source_path = (sandbox_source_path or self.workspace).resolve()

    def _safe_path(self, relative_path: str) -> Path:
        if not relative_path or Path(relative_path).is_absolute():
            raise ValueError("Path must be relative to the workspace")
        if ".git" in Path(relative_path).parts:
            raise ValueError("Git metadata is not exposed")
        candidate = (self.workspace / relative_path).resolve()
        try:
            candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise ValueError("Path escapes the workspace") from exc
        return candidate

    def read_file(self, relative_path: str, max_bytes: int = 50_000) -> str:
        path = self._safe_path(relative_path)
        data = path.read_bytes()
        if len(data) > max_bytes:
            data = data[:max_bytes]
        return data.decode("utf-8", errors="replace")

    def list_files(self, pattern: str = "**/*", limit: int = 500) -> list[str]:
        paths = []
        for path in self.workspace.glob(pattern):
            relative = path.relative_to(self.workspace)
            if path.is_file() and not path.is_symlink() and ".git" not in relative.parts:
                paths.append(str(relative))
                if len(paths) >= limit:
                    break
        return sorted(paths)

    def apply_patch(self, patch_text: str) -> CheckResult:
        if not patch_text.strip():
            return CheckResult(check_id="apply_patch", success=False, error="Patch is empty")
        if len(patch_text.encode("utf-8")) > 1_000_000:
            return CheckResult(check_id="apply_patch", success=False, error="Patch exceeds the 1 MB limit")
        completed = subprocess.run(
            [*self._git_prefix(), "apply", "--whitespace=nowarn", "-"],
            cwd=self.workspace,
            input=patch_text,
            text=True,
            capture_output=True,
            timeout=30,
            shell=False,
            env=self._safe_env(),
        )
        return CheckResult(
            check_id="apply_patch",
            success=completed.returncode == 0,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            error=None if completed.returncode == 0 else "Patch could not be applied",
        )

    def run_check(self, check_id: str) -> CheckResult:
        definition = self.profile.checks.get(check_id)
        if definition is None:
            return CheckResult(check_id=check_id, success=False, error=f"Unknown check_id: {check_id}")
        cid_directory = tempfile.TemporaryDirectory(prefix="workforce-check-")
        cidfile = Path(cid_directory.name) / "container.cid"
        try:
            returncode, stdout, stderr = self._run_bounded_check(
                self._check_argv(definition.argv, cidfile=cidfile),
                definition.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            self._remove_timed_out_container(cidfile)
            return CheckResult(check_id=check_id, success=False, error=f"Check timed out after {exc.timeout}s")
        except OverflowError:
            self._remove_timed_out_container(cidfile)
            return CheckResult(
                check_id=check_id,
                success=False,
                error=f"Check output exceeded the {self.MAX_CHECK_OUTPUT_BYTES} byte limit",
            )
        finally:
            cid_directory.cleanup()
        return CheckResult(
            check_id=check_id,
            success=returncode == 0,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def _run_bounded_check(self, argv: list[str], timeout_seconds: int) -> tuple[int, str, str]:
        return self._run_bounded_process(argv, timeout_seconds, self.MAX_CHECK_OUTPUT_BYTES)

    def _run_bounded_process(
        self,
        argv: list[str],
        timeout_seconds: int,
        max_output_bytes: int,
    ) -> tuple[int, str, str]:
        process = subprocess.Popen(
            argv,
            cwd=self.workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=self._safe_env(),
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_stream = cast(BinaryIO, process.stdout)
        stderr_stream = cast(BinaryIO, process.stderr)
        output: dict[BinaryIO, bytearray] = {
            stdout_stream: bytearray(),
            stderr_stream: bytearray(),
        }
        selector = selectors.DefaultSelector()
        deadline = time.monotonic() + timeout_seconds
        for stream in output:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout_seconds)
                for key, _events in selector.select(timeout=min(remaining, 0.1)):
                    stream = cast(BinaryIO, key.fileobj)
                    try:
                        chunk = os.read(stream.fileno(), 65_536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    output[stream].extend(chunk)
                    if sum(len(value) for value in output.values()) > max_output_bytes:
                        raise OverflowError
            returncode = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except Exception:
            process.kill()
            process.wait()
            raise
        finally:
            selector.close()
            stdout_stream.close()
            stderr_stream.close()
        return (
            returncode,
            output[stdout_stream].decode("utf-8", errors="replace"),
            output[stderr_stream].decode("utf-8", errors="replace"),
        )

    def _check_argv(self, argv: tuple[str, ...], cidfile: Path | None = None) -> list[str]:
        command = [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=256",
            "--memory=1g",
            "--cpus=1",
            "--ulimit=fsize=8388608:8388608",
            "--user=65532:65532",
            f"--volume={self.sandbox_source_path}:/workspace:rw",
            f"--volume={self.sandbox_source_path / '.git'}:/workspace/.git:ro",
            "--workdir=/workspace",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
            "--env=HOME=/tmp",
            "--env=PYTHONDONTWRITEBYTECODE=1",
        ]
        if cidfile is not None:
            command.append(f"--cidfile={cidfile}")
        return [*command, self.profile.sandbox_image, *argv]

    def _remove_timed_out_container(self, cidfile: Path) -> None:
        try:
            container_id = cidfile.read_text(encoding="ascii").strip()
        except OSError:
            return
        if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            return
        try:
            subprocess.run(
                ["docker", "rm", "--force", container_id],
                capture_output=True,
                text=True,
                timeout=15,
                shell=False,
                env=self._safe_env(),
            )
        except (OSError, subprocess.SubprocessError):
            return

    def git_status(self) -> str:
        return self._git_output(("status", "--short"))

    def git_diff(self) -> str:
        completed = subprocess.run(
            [*self._git_prefix(), "add", "--intent-to-add", "--", "."],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
            env=self._safe_env(),
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "Could not include untracked files in diff")
        return self._git_output(
            ("diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--"),
            max_bytes=self.MAX_DIFF_BYTES,
        )

    def workspace_digest(self) -> str:
        """Bind approvals and checks to the exact publishable diff."""
        return sha256(self.git_diff().encode("utf-8")).hexdigest()

    def workspace_state_digest(self) -> str:
        """Detect any check mutation, including ignored files not present in Git diff."""
        digest = sha256()
        paths = sorted(
            (path for path in self.workspace.rglob("*") if ".git" not in path.relative_to(self.workspace).parts),
            key=lambda path: os.fsencode(str(path.relative_to(self.workspace))),
        )
        for path in paths:
            relative = os.fsencode(str(path.relative_to(self.workspace)))
            metadata = path.lstat()
            mode = stat.S_IMODE(metadata.st_mode).to_bytes(4, "big")
            if path.is_symlink():
                self._update_digest_record(digest, b"symlink", relative, mode, os.fsencode(os.readlink(path)))
            elif path.is_file():
                content_digest = sha256()
                with path.open("rb") as source:
                    while chunk := source.read(65_536):
                        content_digest.update(chunk)
                self._update_digest_record(
                    digest,
                    b"file",
                    relative,
                    mode,
                    metadata.st_size.to_bytes(8, "big"),
                    content_digest.digest(),
                )
            elif path.is_dir():
                self._update_digest_record(digest, b"directory", relative, mode)
        return digest.hexdigest()

    def repository_fingerprint(self) -> str:
        """Fingerprint the source state captured by a disposable workspace."""
        digest = sha256()
        head = self._git_output(("rev-parse", "HEAD"), max_bytes=1_024)
        tracked = self._git_output(
            ("diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--"),
            max_bytes=self.MAX_DIFF_BYTES,
        )
        untracked = self._git_output(
            ("ls-files", "--others", "--exclude-standard", "-z"),
            max_bytes=1_000_000,
        )
        self._update_digest_record(digest, b"head", head.encode("utf-8"))
        self._update_digest_record(digest, b"tracked", tracked.encode("utf-8"))
        for relative_path in sorted(path for path in untracked.split("\0") if path):
            path = self._safe_path(relative_path)
            if not path.is_file() or path.is_symlink():
                continue
            metadata = path.stat()
            content_digest = sha256()
            with path.open("rb") as source:
                while chunk := source.read(65_536):
                    content_digest.update(chunk)
            self._update_digest_record(
                digest,
                b"untracked-file",
                os.fsencode(relative_path),
                stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"),
                metadata.st_size.to_bytes(8, "big"),
                content_digest.digest(),
            )
        return digest.hexdigest()

    @staticmethod
    def _update_digest_record(digest: Any, *parts: bytes) -> None:
        for part in parts:
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)

    def _git_output(self, args: tuple[str, ...], max_bytes: int = 100_000) -> str:
        try:
            returncode, output, error = self._run_bounded_process(
                [*self._git_prefix(), *args],
                30,
                max_bytes,
            )
        except OverflowError as exc:
            raise RuntimeError(f"git output exceeds the {max_bytes} byte limit") from exc
        if returncode != 0:
            raise RuntimeError(error.strip() or "git operation failed")
        return output

    def _git_prefix(self) -> list[str]:
        return [
            "git",
            "-c",
            f"safe.directory={self.workspace}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
        ]

    @staticmethod
    def _safe_env() -> dict[str, str]:
        allowed = ("PATH", "LANG", "LC_ALL", "TMPDIR")
        return {name: value for name in allowed if (value := os.environ.get(name)) is not None}
