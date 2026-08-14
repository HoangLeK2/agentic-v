from types import ModuleType
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from workforce.runtime_tools import run_research_pipeline


class RuntimeToolsTest(IsolatedAsyncioTestCase):
    async def test_research_tool_clamps_model_requested_budget(self) -> None:
        workflow = AsyncMock()
        workflow.arun = AsyncMock(return_value=type("Output", (), {"to_dict": lambda self: {"ok": True}})())
        module = ModuleType("workflows.research_pipeline")
        setattr(module, "research_pipeline", workflow)

        with patch.dict("sys.modules", {"workflows.research_pipeline": module}):
            result = await run_research_pipeline(
                "Research this",
                execution_mode="deep",
                max_search_rounds=4,
                max_sources=12,
            )

        self.assertEqual(result, {"ok": True})
        workflow.arun.assert_awaited_once_with(
            input={
                "question": "Research this",
                "quality": "auto",
                "execution_mode": "deep",
                "max_search_rounds": 3,
                "max_sources": 8,
            }
        )
