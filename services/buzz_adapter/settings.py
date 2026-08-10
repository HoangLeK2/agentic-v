"""Environment-backed settings for the standalone Buzz adapter."""

from dataclasses import dataclass
from os import getenv
from pathlib import Path


def _required(name: str) -> str:
    value = getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


@dataclass(frozen=True)
class BuzzAdapterSettings:
    agentos_url: str
    identities_file: Path
    token_pepper: str
    jwt_private_key_file: Path
    jwt_audience: str
    jwt_issuer: str
    workspace_approval_audience: str
    request_timeout_seconds: float

    @classmethod
    def from_env(cls) -> "BuzzAdapterSettings":
        return cls(
            agentos_url=getenv("BUZZ_AGENTOS_URL", "http://agentos-api:8000").rstrip("/"),
            identities_file=Path(_required("BUZZ_IDENTITIES_FILE")),
            token_pepper=_required("BUZZ_TOKEN_PEPPER"),
            jwt_private_key_file=Path(_required("BUZZ_JWT_PRIVATE_KEY_FILE")),
            jwt_audience=getenv("BUZZ_JWT_AUDIENCE", "agentos"),
            jwt_issuer=getenv("BUZZ_JWT_ISSUER", "buzz-adapter"),
            workspace_approval_audience=getenv(
                "BUZZ_WORKSPACE_APPROVAL_AUDIENCE",
                "workspace-executor",
            ),
            request_timeout_seconds=float(getenv("BUZZ_REQUEST_TIMEOUT_SECONDS", "300")),
        )
