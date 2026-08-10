"""Standalone OpenAI-compatible adapter for Buzz."""

import hashlib
import json
import shlex
import time
import uuid
from collections.abc import AsyncIterator
from copy import deepcopy
from functools import cache
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from services.buzz_adapter.auth import BuzzIdentity, ScopedJwtIssuer
from services.buzz_adapter.client import AgentOSRouterClient, present_content
from services.buzz_adapter.identity import FileIdentityProvider
from services.buzz_adapter.models import BuzzEvent, ChatCompletionRequest, extract_buzz_event, render_messages
from services.buzz_adapter.settings import BuzzAdapterSettings

VIRTUAL_MODEL = "buzz-agent"
BUZZ_SHELL_TOOL = "buzz-dev-mcp__shell"
BUZZ_CONTROL_EVENT_KINDS = frozenset({20_002})
MAX_BUZZ_REPLY_CHARS = 100_000
MAX_APPROVAL_DIFF_CHARS = 8_000
APPROVE_UPDATE = "đồng ý cập nhật"
REJECT_UPDATE = "từ chối cập nhật"

_pending_approvals: dict[tuple[str, str], dict[str, Any]] = {}


class Runtime:
    def __init__(self, settings: BuzzAdapterSettings):
        self.settings = settings
        private_key = serialization.load_pem_private_key(settings.jwt_private_key_file.read_bytes(), password=None)
        self.identities = FileIdentityProvider(settings.identities_file, settings.token_pepper)
        self.issuer = ScopedJwtIssuer(
            private_key=private_key,
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
        )
        self.client = AgentOSRouterClient(settings.agentos_url, settings.request_timeout_seconds)


@cache
def runtime() -> Runtime:
    return Runtime(BuzzAdapterSettings.from_env())


def authenticated_identity(authorization: str | None = Header(default=None)) -> BuzzIdentity:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    identity = runtime().identities.get().authenticate(authorization.removeprefix("Bearer ").strip())
    if identity is None:
        raise HTTPException(status_code=401, detail="Invalid bearer token")
    return identity


def _session_id(subject: str, user: str | None) -> str | None:
    if not user:
        return None
    digest = hashlib.sha256(f"{subject}\0{user}".encode()).hexdigest()[:32]
    return f"buzz-{digest}"


def _chunk(completion_id: str, content: str | None = None, finish_reason: str | None = None) -> str:
    delta = {"content": content} if content is not None else {}
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": VIRTUAL_MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=True)}\n\n"


def _completion(completion_id: str, message: dict, finish_reason: str = "stop") -> dict:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": VIRTUAL_MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }


def _has_tool(request: ChatCompletionRequest, tool_name: str) -> bool:
    return any(tool.get("function", {}).get("name") == tool_name for tool in request.tools)


def _publish_tool_call(completion_id: str, event: BuzzEvent, content: str) -> dict:
    content = content.replace("\x00", "")
    if len(content) > MAX_BUZZ_REPLY_CHARS:
        content = content[:MAX_BUZZ_REPLY_CHARS] + "\n\n[Reply truncated by Buzz adapter]"
    command = (
        f"printf '%s' {shlex.quote(content)} | "
        f"buzz messages send --channel {event.channel_id} --reply-to {event.event_id} --content -"
    )
    tool_call = {
        "id": f"call_{uuid.uuid4().hex}",
        "type": "function",
        "function": {
            "name": BUZZ_SHELL_TOOL,
            "arguments": json.dumps({"command": command, "timeout_ms": 30_000}),
        },
    }
    return _completion(
        completion_id,
        {"role": "assistant", "content": None, "tool_calls": [tool_call]},
        finish_reason="tool_calls",
    )


def _approval_decision(content: str) -> bool | None:
    decision = content.strip().casefold().rstrip(".!?").strip()
    if decision == APPROVE_UPDATE:
        return True
    if decision == REJECT_UPDATE:
        return False
    return None


def _approval_requirements(result: dict[str, Any]) -> list[dict[str, Any]] | None:
    if str(result.get("status") or "").upper() != "PAUSED":
        return None
    requirements = result.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        return None
    if not all(isinstance(requirement, dict) for requirement in requirements):
        return None
    return requirements


def _approval_prompt(requirements: list[dict[str, Any]]) -> str:
    patches = _approval_patches(requirements)
    preview = "\n\n".join(patches)
    detail = f"\n\n```diff\n{preview}\n```" if preview else ""
    return (
        "Mình đã chuẩn bị thay đổi sau nhưng chưa áp dụng."
        f"{detail}\n\nTrả lời `{APPROVE_UPDATE}` để áp dụng hoặc `{REJECT_UPDATE}` để hủy."
    )


def _approval_patches(requirements: list[dict[str, Any]]) -> list[str]:
    patches: list[str] = []
    for requirement in requirements:
        tool_execution = requirement.get("tool_execution")
        if not isinstance(tool_execution, dict) or tool_execution.get("tool_name") != "apply_patch":
            continue
        tool_args = tool_execution.get("tool_args")
        if isinstance(tool_args, dict) and isinstance(tool_args.get("patch_text"), str):
            patches.append(tool_args["patch_text"])
    return patches


def _approval_is_previewable(requirements: list[dict[str, Any]]) -> bool:
    patches = _approval_patches(requirements)
    return bool(patches) and len("\n\n".join(patches)) <= MAX_APPROVAL_DIFF_CHARS


def _resolved_requirements(
    requirements: list[dict[str, Any]],
    confirmed: bool,
    *,
    issuer: ScopedJwtIssuer,
    subject: str,
    session_id: str,
    approval_audience: str,
) -> list[dict[str, Any]]:
    resolved = deepcopy(requirements)
    for requirement in resolved:
        requirement["confirmation"] = confirmed
        tool_execution = requirement.get("tool_execution")
        if isinstance(tool_execution, dict):
            tool_execution["confirmed"] = confirmed
            if not confirmed:
                tool_execution["confirmation_note"] = "Rejected by Buzz user"
            elif tool_execution.get("tool_name") == "apply_patch":
                tool_args = tool_execution.get("tool_args")
                if not isinstance(tool_args, dict):
                    raise HTTPException(status_code=502, detail="Paused apply_patch has invalid tool arguments")
                workspace_id = tool_args.get("workspace_id")
                patch_text = tool_args.get("patch_text")
                if not isinstance(workspace_id, str) or not isinstance(patch_text, str):
                    raise HTTPException(status_code=502, detail="Paused apply_patch is missing workspace or patch")
                tool_args["approval_token"] = issuer.issue_workspace_approval(
                    subject=subject,
                    session_id=session_id,
                    workspace_id=workspace_id,
                    patch_text=patch_text,
                    audience=approval_audience,
                )
    return resolved


app = FastAPI(title="Buzz Workforce Adapter", docs_url=None, redoc_url=None)


@app.exception_handler(httpx.HTTPStatusError)
async def upstream_error_handler(_request, exc: httpx.HTTPStatusError) -> JSONResponse:
    status = 503 if exc.response.status_code >= 500 else exc.response.status_code
    return JSONResponse(
        status_code=status,
        content={"error": {"message": "AgentOS request failed", "type": "upstream_error"}},
    )


@app.exception_handler(httpx.RequestError)
async def upstream_connection_handler(_request, _exc: httpx.RequestError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error": {"message": "AgentOS is unavailable", "type": "upstream_error"}},
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def models(_identity: BuzzIdentity = Depends(authenticated_identity)) -> dict:
    return {"object": "list", "data": [{"id": VIRTUAL_MODEL, "object": "model", "owned_by": "workforce"}]}


@app.post("/v1/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    identity: BuzzIdentity = Depends(authenticated_identity),
):
    if request.model != VIRTUAL_MODEL:
        raise HTTPException(status_code=404, detail=f"Unknown model: {request.model}")
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"

    if request.messages[-1].role == "tool":
        return _completion(completion_id, {"role": "assistant", "content": ""})

    buzz_event = extract_buzz_event(request.messages)
    if buzz_event is not None and (not buzz_event.content or buzz_event.kind in BUZZ_CONTROL_EVENT_KINDS):
        return _completion(completion_id, {"role": "assistant", "content": ""})

    try:
        prompt = buzz_event.content if buzz_event is not None else render_messages(request.messages)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    scoped_token = runtime().issuer.issue(identity.subject)
    session_key = buzz_event.channel_id if buzz_event is not None else request.user
    session_id = _session_id(identity.subject, session_key)
    approval_key = (identity.subject, session_id) if session_id is not None else None

    if request.stream and buzz_event is not None and _has_tool(request, BUZZ_SHELL_TOOL):
        raise HTTPException(status_code=400, detail="Buzz reply tool calls require non-streaming chat completions")

    pending = _pending_approvals.get(approval_key) if approval_key is not None else None
    if pending is not None and buzz_event is not None and approval_key is not None:
        assert session_id is not None
        decision = _approval_decision(buzz_event.content)
        if decision is None:
            content = (
                f"Một thay đổi đang chờ xác nhận. Trả lời `{APPROVE_UPDATE}` để áp dụng hoặc "
                f"`{REJECT_UPDATE}` để hủy."
            )
        else:
            adapter_runtime = runtime()
            result = await runtime().client.continue_run(
                run_id=pending["run_id"],
                requirements=_resolved_requirements(
                    pending["requirements"],
                    decision,
                    issuer=adapter_runtime.issuer,
                    subject=identity.subject,
                    session_id=session_id,
                    approval_audience=adapter_runtime.settings.workspace_approval_audience,
                ),
                token=scoped_token,
                subject=identity.subject,
                session_id=session_id,
            )
            _pending_approvals.pop(approval_key, None)
            followup_requirements = _approval_requirements(result)
            if followup_requirements is not None:
                if _approval_is_previewable(followup_requirements):
                    _pending_approvals[approval_key] = {
                        "run_id": result["run_id"],
                        "requirements": followup_requirements,
                    }
                    content = _approval_prompt(followup_requirements)
                else:
                    content = "Patch cần duyệt vượt quá 8.000 ký tự. Hãy chia thay đổi thành các patch nhỏ hơn."
            else:
                content = present_content(result) or ("Đã áp dụng thay đổi." if decision else "Đã hủy thay đổi.")
        if content and _has_tool(request, BUZZ_SHELL_TOOL):
            return _publish_tool_call(completion_id, buzz_event, content)
        return _completion(completion_id, {"role": "assistant", "content": content})

    if request.stream:

        async def chunks() -> AsyncIterator[str]:
            yield _chunk(completion_id)
            async for content in runtime().client.stream(
                prompt=prompt,
                token=scoped_token,
                subject=identity.subject,
                session_id=session_id,
            ):
                yield _chunk(completion_id, content=content)
            yield _chunk(completion_id, finish_reason="stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    result = await runtime().client.run(
        prompt=prompt,
        token=scoped_token,
        subject=identity.subject,
        session_id=session_id,
    )
    requirements = _approval_requirements(result)
    if requirements is not None and approval_key is not None:
        run_id = result.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise HTTPException(status_code=502, detail="Paused AgentOS run did not return a run_id")
        if _approval_is_previewable(requirements):
            _pending_approvals[approval_key] = {"run_id": run_id, "requirements": requirements}
            content = _approval_prompt(requirements)
        else:
            content = "Patch cần duyệt vượt quá 8.000 ký tự. Hãy chia thay đổi thành các patch nhỏ hơn."
    else:
        content = present_content(result)
    if buzz_event is not None and content and _has_tool(request, BUZZ_SHELL_TOOL):
        return _publish_tool_call(completion_id, buzz_event, content)
    return _completion(completion_id, {"role": "assistant", "content": content})


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8200)


if __name__ == "__main__":
    main()
