"""Environment-backed settings for the standalone Buzz adapter."""

from dataclasses import dataclass
from os import getenv
from pathlib import Path

DEFAULT_REQUEST_TIMEOUT_SECONDS = 900
DEFAULT_AGENTOS_RUN_TIMEOUT_SECONDS = 3600
DEFAULT_DEDUPE_GRACE_SECONDS = 300
DEFAULT_EVENT_DEDUPE_SECONDS = 3900


def _env_bool(name: str, default: bool) -> bool:
    value = getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes"}


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
    agentos_run_timeout_seconds: float
    log_reply_preview: bool
    reply_preview_chars: int
    event_dedupe_seconds: int

    @classmethod
    def from_env(cls) -> "BuzzAdapterSettings":
        request_timeout_seconds = max(
            1.0,
            float(getenv("BUZZ_REQUEST_TIMEOUT_SECONDS", str(DEFAULT_REQUEST_TIMEOUT_SECONDS))),
        )
        agentos_run_timeout_seconds = max(
            request_timeout_seconds,
            float(getenv("BUZZ_AGENTOS_RUN_TIMEOUT_SECONDS", str(DEFAULT_AGENTOS_RUN_TIMEOUT_SECONDS))),
        )
        event_dedupe_seconds = max(
            1,
            int(getenv("BUZZ_EVENT_DEDUPE_SECONDS", str(DEFAULT_EVENT_DEDUPE_SECONDS))),
            int(agentos_run_timeout_seconds) + DEFAULT_DEDUPE_GRACE_SECONDS,
        )
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
            request_timeout_seconds=request_timeout_seconds,
            agentos_run_timeout_seconds=agentos_run_timeout_seconds,
            log_reply_preview=_env_bool("BUZZ_LOG_REPLY_PREVIEW", False),
            reply_preview_chars=max(0, int(getenv("BUZZ_REPLY_PREVIEW_CHARS", "800"))),
            event_dedupe_seconds=event_dedupe_seconds,
        )
