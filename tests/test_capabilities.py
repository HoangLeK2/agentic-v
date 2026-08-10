from os import environ
from unittest import TestCase
from unittest.mock import patch

from workforce.capabilities import CapabilityRegistry, CapabilityStatus, OperationCapability, build_capability_registry


class CapabilityRegistryTest(TestCase):
    def test_tracks_status_per_operation(self) -> None:
        registry = CapabilityRegistry(
            [
                OperationCapability(
                    id="seo.static_audit",
                    status=CapabilityStatus.AVAILABLE,
                    provider="workspace-executor",
                    permissions=("read",),
                ),
                OperationCapability(
                    id="seo.search_console",
                    status=CapabilityStatus.UNAVAILABLE,
                    provider="google-search-console",
                    permissions=("read",),
                    reason="GOOGLE_SEARCH_CONSOLE_CREDENTIALS is not configured",
                ),
            ]
        )

        static_audit = registry.get("seo.static_audit")
        search_console = registry.get("seo.search_console")
        self.assertIsNotNone(static_audit)
        self.assertIsNotNone(search_console)
        assert static_audit is not None
        assert search_console is not None
        self.assertEqual(static_audit.status, CapabilityStatus.AVAILABLE)
        self.assertEqual(search_console.status, CapabilityStatus.UNAVAILABLE)
        self.assertIsNone(registry.get("seo"))

    def test_summarizes_mixed_requirements_without_hiding_degradation(self) -> None:
        registry = CapabilityRegistry(
            [
                OperationCapability(
                    id="research.web_search",
                    status=CapabilityStatus.AVAILABLE,
                    provider="parallel",
                    permissions=("read",),
                ),
                OperationCapability(
                    id="research.deep_fetch",
                    status=CapabilityStatus.DEGRADED,
                    provider="parallel-keyless",
                    permissions=("read",),
                    reason="Keyless rate ceiling applies",
                ),
            ]
        )

        summary = registry.evaluate(("research.web_search", "research.deep_fetch"))

        self.assertEqual(summary.status, CapabilityStatus.DEGRADED)
        self.assertEqual(summary.degraded, ("research.deep_fetch",))
        self.assertEqual(summary.unavailable, ())

    def test_unknown_required_operation_fails_closed(self) -> None:
        registry = CapabilityRegistry([])

        summary = registry.evaluate(("production.restart",))

        self.assertEqual(summary.status, CapabilityStatus.UNAVAILABLE)
        self.assertEqual(summary.unavailable, ("production.restart",))

    def test_executor_requires_endpoint_and_token(self) -> None:
        with patch.dict(
            environ,
            {"WORKSPACE_EXECUTOR_MCP_URL": "http://executor/mcp"},
            clear=True,
        ):
            registry = build_capability_registry()

        capability = registry.get("code.run_checks")
        assert capability is not None
        self.assertEqual(capability.status, CapabilityStatus.UNAVAILABLE)
        self.assertIn("WORKSPACE_EXECUTOR_TOKEN", capability.reason or "")

    def test_credentials_do_not_create_unwired_capabilities(self) -> None:
        with patch.dict(
            environ,
            {
                "GOOGLE_SEARCH_CONSOLE_CREDENTIALS": "configured",
                "GOOGLE_ANALYTICS_CREDENTIALS": "configured",
                "OBSERVABILITY_MCP_URL": "http://observability/mcp",
            },
            clear=True,
        ):
            registry = build_capability_registry()

        for operation_id in ("seo.search_console", "seo.analytics", "production.observe"):
            capability = registry.get(operation_id)
            assert capability is not None
            self.assertEqual(capability.status, CapabilityStatus.UNAVAILABLE)
            self.assertIn("not implemented", capability.reason or "")
