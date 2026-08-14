"""Small typed client used by deterministic workflows."""

from os import getenv
from typing import Any

from fastmcp import Client


def _result_data(result: Any) -> Any:
    data = getattr(result, "data", None)
    if data is not None:
        return data
    structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    content = getattr(result, "content", None)
    if content and hasattr(content[0], "text"):
        return content[0].text
    return content


class WorkspaceExecutorClient:
    def __init__(self, url: str | None = None, token: str | None = None):
        self.url = url or getenv("WORKSPACE_EXECUTOR_MCP_URL")
        if not self.url:
            raise RuntimeError("WORKSPACE_EXECUTOR_MCP_URL is not configured")
        self._client = Client(self.url, auth=token or getenv("WORKSPACE_EXECUTOR_TOKEN"), timeout=180)

    async def __aenter__(self) -> "WorkspaceExecutorClient":
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self._client.__aexit__(exc_type, exc, traceback)

    async def call(self, name: str, **arguments: Any) -> Any:
        return _result_data(await self._client.call_tool(name, arguments, timeout=180))

    async def create_workspace(self, repo_id: str) -> str:
        payload = await self.call("create_workspace", repo_id=repo_id)
        return payload["workspace_id"]

    async def list_repositories(self) -> tuple[dict[str, Any], ...]:
        return tuple(await self.call("list_repositories"))

    async def list_checks(self, workspace_id: str) -> tuple[str, ...]:
        return tuple(await self.call("list_checks", workspace_id=workspace_id))

    async def run_check(self, workspace_id: str, check_id: str) -> dict:
        return await self.call("run_check", workspace_id=workspace_id, check_id=check_id)

    async def git_diff(self, workspace_id: str) -> str:
        return await self.call("git_diff", workspace_id=workspace_id)

    async def grant_publish(self, workspace_id: str, verdict: str) -> dict:
        return await self.call("grant_publish", workspace_id=workspace_id, verdict=verdict)

    async def publish_changes(self, workspace_id: str) -> dict:
        return await self.call("publish_changes", workspace_id=workspace_id)

    async def close_workspace(self, workspace_id: str) -> None:
        await self.call("close_workspace", workspace_id=workspace_id)
