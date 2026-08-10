"""AgentOS workforce-router REST client."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

_OUTCOME_LABELS = {
    "bằng chứng": "evidence",
    "bước tiếp theo": "next_step",
    "giới hạn": "limitations",
    "kết quả": "result",
    "phát hiện": "findings",
    "status": "status",
    "summary": "result",
    "evidence": "evidence",
    "limitations": "limitations",
    "next step": "next_step",
    "next steps": "next_step",
    "result": "result",
    "thay đổi": "changes",
    "trạng thái": "status",
    "yêu cầu platform": "platform_request",
    "đã làm": "work_done",
}


def extract_content(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("content", "response", "output"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=True)
        if isinstance(payload.get("messages"), list):
            for message in reversed(payload["messages"]):
                content = extract_content(message)
                if content:
                    return content
    return ""


def _plain_line(line: str) -> str:
    plain = line.strip()
    if plain.startswith("- ") or plain.startswith("* "):
        plain = plain[2:].lstrip()
    plain = plain.lstrip("#").strip()
    return plain.replace("**", "").replace("__", "").strip()


def _structured_outcome_summary(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("status"), str) and isinstance(payload.get("summary"), str):
        return payload["summary"].strip()
    for key in ("content", "response", "output"):
        summary = _structured_outcome_summary(payload.get(key))
        if summary is not None:
            return summary
    return None


def _remove_outcome_footer(content: str) -> str:
    lines = content.splitlines()
    heading_index: int | None = None
    for index, line in enumerate(lines):
        if _plain_line(line).rstrip(":").casefold() == "workforceoutcome":
            heading_index = index
    if heading_index is None:
        return content.strip()

    fields: dict[str, list[str]] = {}
    current_field: str | None = None
    for line in lines[heading_index + 1 :]:
        plain = _plain_line(line)
        key_text, separator, value = plain.partition(":")
        field = _OUTCOME_LABELS.get(key_text.strip().casefold()) if separator else None
        if field is not None:
            current_field = field
            fields.setdefault(field, []).append(value.strip())
        elif current_field is not None and plain:
            fields[current_field].append(plain)

    if len(fields) < 2:
        return content.strip()
    visible_answer = "\n".join(lines[:heading_index]).strip()
    if visible_answer:
        return visible_answer
    return "\n".join(part for part in fields.get("result", ()) if part).strip()


def present_content(payload: Any) -> str:
    """Render an AgentOS result for humans without exposing its transport contract."""
    structured_summary = _structured_outcome_summary(payload)
    if structured_summary is not None:
        return structured_summary
    return _remove_outcome_footer(extract_content(payload))


def parse_sse_event(lines: list[str]) -> tuple[str, Any] | None:
    event = "message"
    data_lines: list[str] = []
    for line in lines:
        if line.startswith("event:"):
            event = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").lstrip())
    if not data_lines:
        return None
    data = "\n".join(data_lines)
    try:
        return event, json.loads(data)
    except json.JSONDecodeError:
        return event, data


class AgentOSRouterClient:
    def __init__(self, base_url: str, timeout_seconds: float):
        self.base_url = base_url
        self.timeout = httpx.Timeout(timeout_seconds, connect=10)

    async def run(self, *, prompt: str, token: str, subject: str, session_id: str | None = None) -> dict:
        data = {"message": prompt, "stream": "false", "user_id": subject}
        if session_id:
            data["session_id"] = session_id
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/teams/workforce-router/runs",
                data=data,
                headers={"Authorization": f"Bearer {token}"},
            )
        response.raise_for_status()
        return response.json()

    async def continue_run(
        self,
        *,
        run_id: str,
        requirements: list[dict[str, Any]],
        token: str,
        subject: str,
        session_id: str | None = None,
    ) -> dict:
        data: dict[str, Any] = {
            "requirements": json.dumps(requirements),
            "stream": "false",
            "user_id": subject,
        }
        if session_id:
            data["session_id"] = session_id
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/teams/workforce-router/runs/{run_id}/continue",
                data=data,
                headers={"Authorization": f"Bearer {token}"},
            )
        response.raise_for_status()
        return response.json()

    async def stream(
        self, *, prompt: str, token: str, subject: str, session_id: str | None = None
    ) -> AsyncIterator[str]:
        data = {"message": prompt, "stream": "true", "user_id": subject}
        if session_id:
            data["session_id"] = session_id
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/teams/workforce-router/runs",
                data=data,
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                response.raise_for_status()
                event_lines: list[str] = []
                async for line in response.aiter_lines():
                    if line:
                        event_lines.append(line)
                        continue
                    parsed = parse_sse_event(event_lines)
                    event_lines = []
                    if parsed is None:
                        continue
                    event, payload = parsed
                    if event in {"RunContent", "TeamRunContent"}:
                        content = present_content(payload)
                        if content:
                            yield content
