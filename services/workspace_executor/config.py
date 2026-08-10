"""Configuration contracts for allowlisted repositories and named checks."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

WritePolicy = Literal["trusted", "approval_required"]


@dataclass(frozen=True)
class CheckDefinition:
    argv: tuple[str, ...]
    timeout_seconds: int = 120

    def __post_init__(self) -> None:
        if not self.argv or any(not argument for argument in self.argv):
            raise ValueError("Check argv must contain non-empty arguments")
        if self.timeout_seconds <= 0:
            raise ValueError("Check timeout_seconds must be positive")


@dataclass(frozen=True)
class RepoProfile:
    repo_id: str
    source_path: Path
    checks: dict[str, CheckDefinition]
    sandbox_image: str
    write_policy: WritePolicy = "approval_required"

    def __post_init__(self) -> None:
        if not self.sandbox_image.strip():
            raise ValueError(f"Repository profile {self.repo_id!r} must pin a sandbox_image")
        if self.write_policy not in {"trusted", "approval_required"}:
            raise ValueError(f"Repository profile {self.repo_id!r} has an invalid write_policy")
        if self.write_policy == "trusted" and not self.checks:
            raise ValueError(f"Trusted repository profile {self.repo_id!r} must define at least one check")
        object.__setattr__(self, "source_path", Path(self.source_path).resolve())


def load_repo_profiles(path: Path) -> dict[str, RepoProfile]:
    payload = json.loads(path.read_text())
    profiles: dict[str, RepoProfile] = {}
    for repo_id, raw in payload.get("repos", {}).items():
        checks = {
            check_id: CheckDefinition(
                argv=tuple(definition["argv"]),
                timeout_seconds=int(definition.get("timeout_seconds", 120)),
            )
            for check_id, definition in raw.get("checks", {}).items()
        }
        profiles[repo_id] = RepoProfile(
            repo_id=repo_id,
            source_path=Path(raw["source_path"]),
            checks=checks,
            sandbox_image=str(raw.get("sandbox_image") or ""),
            write_policy=str(raw.get("write_policy") or "approval_required"),  # type: ignore[arg-type]
        )
    return profiles
