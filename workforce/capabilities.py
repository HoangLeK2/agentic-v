"""Operation-level capability inventory for routing and fail-closed behavior."""

from __future__ import annotations

import builtins
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from enum import StrEnum
from os import getenv

ENGINEERING_CAPABILITY_PREFIXES = ("code.", "security.")


class CapabilityStatus(StrEnum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class OperationCapability:
    id: str
    status: CapabilityStatus
    provider: str
    permissions: tuple[str, ...]
    reason: str | None = None
    required_config: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


@dataclass(frozen=True)
class CapabilityEvaluation:
    status: CapabilityStatus
    available: tuple[str, ...]
    degraded: tuple[str, ...]
    unavailable: tuple[str, ...]


class CapabilityRegistry:
    def __init__(self, capabilities: Iterable[OperationCapability]):
        self._capabilities = {capability.id: capability for capability in capabilities}

    def get(self, operation_id: str) -> OperationCapability | None:
        return self._capabilities.get(operation_id)

    def list(self, prefix: str | None = None) -> tuple[OperationCapability, ...]:
        capabilities: Iterable[OperationCapability] = self._capabilities.values()
        if prefix:
            normalized_prefixes = _normalize_prefix(prefix)
            capabilities = (
                item for item in capabilities if any(item.id.startswith(candidate) for candidate in normalized_prefixes)
            )
        return tuple(sorted(capabilities, key=lambda item: item.id))

    def evaluate(self, operation_ids: Iterable[str]) -> CapabilityEvaluation:
        available: list[str] = []
        degraded: list[str] = []
        unavailable: list[str] = []
        for operation_id in operation_ids:
            capability = self.get(operation_id)
            if capability is None or capability.status == CapabilityStatus.UNAVAILABLE:
                unavailable.append(operation_id)
            elif capability.status == CapabilityStatus.DEGRADED:
                degraded.append(operation_id)
            else:
                available.append(operation_id)
        status = (
            CapabilityStatus.UNAVAILABLE
            if unavailable
            else CapabilityStatus.DEGRADED
            if degraded
            else CapabilityStatus.AVAILABLE
        )
        return CapabilityEvaluation(
            status=status,
            available=tuple(available),
            degraded=tuple(degraded),
            unavailable=tuple(unavailable),
        )

    def as_dicts(self, prefix: str | None = None) -> builtins.list[dict]:
        return [capability.to_dict() for capability in self.list(prefix)]


def _configured(*names: str) -> bool:
    return all(bool(getenv(name)) for name in names)


def _normalize_prefix(prefix: str) -> tuple[str, ...]:
    normalized = prefix.strip().casefold()
    if normalized in {"engineering", "engineering.", "engineering.operation", "engineering.operations"}:
        return ENGINEERING_CAPABILITY_PREFIXES
    return (normalized,)


def build_capability_registry() -> CapabilityRegistry:
    executor_available = _configured("WORKSPACE_EXECUTOR_MCP_URL", "WORKSPACE_EXECUTOR_TOKEN")
    parallel_keyed = bool(getenv("PARALLEL_API_KEY"))
    web_status = CapabilityStatus.AVAILABLE if parallel_keyed else CapabilityStatus.DEGRADED
    web_reason = None if parallel_keyed else "Keyless web search has a lower rate ceiling"
    executor_status = CapabilityStatus.AVAILABLE if executor_available else CapabilityStatus.UNAVAILABLE
    executor_reason = (
        None
        if executor_available
        else "WORKSPACE_EXECUTOR_MCP_URL and WORKSPACE_EXECUTOR_TOKEN must both be configured"
    )

    capabilities = [
        OperationCapability("code.read", executor_status, "workspace-executor", ("read",), executor_reason),
        OperationCapability(
            "code.sandbox_write", executor_status, "workspace-executor", ("sandbox_write",), executor_reason
        ),
        OperationCapability("code.run_checks", executor_status, "workspace-executor", ("read",), executor_reason),
        OperationCapability(
            "security.static_review", executor_status, "workspace-executor", ("read",), executor_reason
        ),
        OperationCapability("seo.static_audit", web_status, "parallel", ("read",), web_reason),
        OperationCapability("seo.keyword_research", web_status, "parallel", ("read",), web_reason),
        OperationCapability(
            "seo.search_console",
            CapabilityStatus.UNAVAILABLE,
            "google-search-console",
            ("read",),
            "Google Search Console adapter is not implemented in Workforce v1",
            ("GOOGLE_SEARCH_CONSOLE_CREDENTIALS",),
        ),
        OperationCapability(
            "seo.analytics",
            CapabilityStatus.UNAVAILABLE,
            "google-analytics",
            ("read",),
            "Google Analytics adapter is not implemented in Workforce v1",
            ("GOOGLE_ANALYTICS_CREDENTIALS",),
        ),
        OperationCapability("research.web_search", web_status, "parallel", ("read",), web_reason),
        OperationCapability("research.deep_fetch", web_status, "parallel", ("read",), web_reason),
        OperationCapability(
            "production.observe",
            CapabilityStatus.UNAVAILABLE,
            "observability-mcp",
            ("read",),
            "Production observability adapter is not implemented in Workforce v1",
            ("OBSERVABILITY_MCP_URL",),
        ),
        OperationCapability(
            "production.write",
            CapabilityStatus.UNAVAILABLE,
            "none",
            ("approval_required",),
            "Production writes are not implemented in Workforce v1",
        ),
    ]
    return CapabilityRegistry(capabilities)


capability_registry = build_capability_registry()


def list_operation_capabilities(prefix: str | None = None) -> list[dict]:
    """List operation capabilities without exposing credential values."""
    return capability_registry.as_dicts(prefix)
