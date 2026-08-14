"""Standalone OpenAI-compatible adapter for Buzz."""

import asyncio
import hashlib
import json
import logging
import shlex
import time
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from functools import cache
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from services.buzz_adapter.auth import BuzzIdentity, ScopedJwtIssuer
from services.buzz_adapter.client import AgentOSRouterClient, present_content
from services.buzz_adapter.identity import FileIdentityProvider
from services.buzz_adapter.models import (
    BuzzEvent,
    ChatCompletionRequest,
    extract_buzz_event,
    latest_user_message,
    render_messages,
)
from services.buzz_adapter.settings import BuzzAdapterSettings

VIRTUAL_MODEL = "buzz-agent"
BUZZ_SHELL_TOOL = "buzz-dev-mcp__shell"
BUZZ_CONTROL_EVENT_KINDS = frozenset({20_002})
MAX_BUZZ_REPLY_CHARS = 100_000
MAX_APPROVAL_DIFF_CHARS = 8_000
APPROVE_UPDATE = "đồng ý cập nhật"
REJECT_UPDATE = "từ chối cập nhật"

logger = logging.getLogger("uvicorn.error")
_pending_approvals: dict[tuple[str, str], dict[str, Any]] = {}
_buzz_event_dedupe: dict[tuple[str, str], float] = {}
_buzz_event_tasks: dict[tuple[str, str], Future[dict[str, Any]]] = {}
_buzz_event_results: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_buzz_event_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="buzz-event")


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


def _log_buzz_reply(event: BuzzEvent, content: str, *, delivery: str) -> None:
    settings = runtime().settings
    if getattr(settings, "log_reply_preview", False) is not True:
        return
    limit = int(getattr(settings, "reply_preview_chars", 800))
    preview = content[:limit].replace("\n", "\\n")
    if len(content) > limit:
        preview += "...[truncated]"
    logger.info(
        "Buzz reply prepared delivery=%s channel=%s event=%s kind=%s chars=%s preview=%s",
        delivery,
        event.channel_id,
        event.event_id,
        event.kind,
        len(content),
        preview,
    )


def _dedupe_key(subject: str, event: BuzzEvent | None) -> tuple[str, str] | None:
    if event is None:
        return None
    return subject, event.event_id


def _prune_event_dedupe(now: float) -> None:
    expired = [key for key, expires_at in _buzz_event_dedupe.items() if expires_at <= now]
    for key in expired:
        _buzz_event_dedupe.pop(key, None)
    expired_results = [key for key, (expires_at, _result) in _buzz_event_results.items() if expires_at <= now]
    for key in expired_results:
        _buzz_event_results.pop(key, None)


def _cache_buzz_event_result(key: tuple[str, str], task: Future[dict[str, Any]]) -> None:
    _buzz_event_tasks.pop(key, None)
    if task.cancelled():
        return
    try:
        result = task.result()
    except Exception:
        return
    ttl_seconds = int(getattr(runtime().settings, "event_dedupe_seconds", 900))
    _buzz_event_results[key] = (time.time() + ttl_seconds, result)


def _run_agentos_in_background(prompt: str, token: str, subject: str, session_id: str | None) -> dict[str, Any]:
    settings = runtime().settings
    return asyncio.run(
        runtime().client.run(
            prompt=prompt,
            token=token,
            subject=subject,
            session_id=session_id,
            timeout_seconds=settings.agentos_run_timeout_seconds,
        )
    )


def _cache_completed_buzz_event_result(key: tuple[str, str]):
    def _callback(task: Future[dict[str, Any]]) -> None:
        _cache_buzz_event_result(key, task)

    return _callback


async def _run_agentos_once_for_buzz_event(
    *,
    subject: str,
    event: BuzzEvent | None,
    prompt: str,
    token: str,
    session_id: str | None,
) -> dict[str, Any] | None:
    key = _dedupe_key(subject, event)
    if key is None:
        return await runtime().client.run(prompt=prompt, token=token, subject=subject, session_id=session_id)

    now = time.time()
    _prune_event_dedupe(now)
    if key in _buzz_event_dedupe:
        assert event is not None
        logger.info(
            "Buzz duplicate event suppressed subject=%s event=%s channel=%s kind=%s",
            subject,
            event.event_id,
            event.channel_id,
            event.kind,
        )
        return None

    cached = _buzz_event_results.get(key)
    if cached is not None:
        return cached[1]

    task = _buzz_event_tasks.get(key)
    if task is None:
        task = _buzz_event_executor.submit(
            _run_agentos_in_background,
            prompt,
            token,
            subject,
            session_id,
        )
        task.add_done_callback(_cache_completed_buzz_event_result(key))
        _buzz_event_tasks[key] = task
    else:
        assert event is not None
        logger.info(
            "Buzz duplicate event joined in-flight run subject=%s event=%s channel=%s kind=%s",
            subject,
            event.event_id,
            event.channel_id,
            event.kind,
        )
    return await asyncio.shield(asyncio.wrap_future(task))


async def _should_deliver_buzz_event_response(subject: str, event: BuzzEvent | None, request: Request) -> bool:
    key = _dedupe_key(subject, event)
    if key is None:
        return True
    assert event is not None
    if await request.is_disconnected():
        logger.info(
            "Buzz event response left for retry because client disconnected subject=%s event=%s channel=%s kind=%s",
            subject,
            event.event_id,
            event.channel_id,
            event.kind,
        )
        return False

    now = time.time()
    _prune_event_dedupe(now)
    if key in _buzz_event_dedupe:
        logger.info(
            "Buzz duplicate event suppressed subject=%s event=%s channel=%s kind=%s",
            subject,
            event.event_id,
            event.channel_id,
            event.kind,
        )
        return False
    ttl_seconds = int(getattr(runtime().settings, "event_dedupe_seconds", 900))
    _buzz_event_dedupe[key] = now + ttl_seconds
    return True


def _publish_tool_call(completion_id: str, event: BuzzEvent, content: str) -> dict:
    content = content.replace("\x00", "")
    if len(content) > MAX_BUZZ_REPLY_CHARS:
        content = content[:MAX_BUZZ_REPLY_CHARS] + "\n\n[Reply truncated by Buzz adapter]"
    _log_buzz_reply(event, content, delivery="tool_call")
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


def _timeout_message() -> str:
    timeout = int(runtime().settings.request_timeout_seconds)
    return (
        f"Tác vụ này chạy quá {timeout}s nên mình đã dừng reply bridge để tránh Buzz retry tạo thêm job trùng. "
        "Hãy chia nhỏ yêu cầu, hoặc chạy qua AgentOS UI/MCP cho tác vụ code dài."
    )


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
    http_request: Request,
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
        if buzz_event is not None:
            prompt = buzz_event.content
        elif request.user:
            prompt = latest_user_message(request.messages)
        else:
            prompt = render_messages(request.messages)
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
                f"Một thay đổi đang chờ xác nhận. Trả lời `{APPROVE_UPDATE}` để áp dụng hoặc `{REJECT_UPDATE}` để hủy."
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

    run_result: dict[str, Any] | None
    try:
        run_result = await _run_agentos_once_for_buzz_event(
            subject=identity.subject,
            event=buzz_event,
            prompt=prompt,
            token=scoped_token,
            session_id=session_id,
        )
    except httpx.TimeoutException:
        logger.warning(
            "Buzz AgentOS run timed out subject=%s channel=%s event=%s timeout_seconds=%s",
            identity.subject,
            buzz_event.channel_id if buzz_event is not None else None,
            buzz_event.event_id if buzz_event is not None else None,
            runtime().settings.request_timeout_seconds,
        )
        content = _timeout_message()
        if buzz_event is not None and not await _should_deliver_buzz_event_response(
            identity.subject,
            buzz_event,
            http_request,
        ):
            return _completion(completion_id, {"role": "assistant", "content": ""})
        if buzz_event is not None and _has_tool(request, BUZZ_SHELL_TOOL):
            return _publish_tool_call(completion_id, buzz_event, content)
        return _completion(completion_id, {"role": "assistant", "content": content})
    if run_result is None:
        return _completion(completion_id, {"role": "assistant", "content": ""})
    requirements = _approval_requirements(run_result)
    if requirements is not None and approval_key is not None:
        run_id = run_result.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise HTTPException(status_code=502, detail="Paused AgentOS run did not return a run_id")
        if _approval_is_previewable(requirements):
            _pending_approvals[approval_key] = {"run_id": run_id, "requirements": requirements}
            content = _approval_prompt(requirements)
        else:
            content = "Patch cần duyệt vượt quá 8.000 ký tự. Hãy chia thay đổi thành các patch nhỏ hơn."
    else:
        content = present_content(run_result)
    if buzz_event is not None and content and _has_tool(request, BUZZ_SHELL_TOOL):
        if not await _should_deliver_buzz_event_response(identity.subject, buzz_event, http_request):
            return _completion(completion_id, {"role": "assistant", "content": ""})
        return _publish_tool_call(completion_id, buzz_event, content)
    if buzz_event is not None and content:
        if not await _should_deliver_buzz_event_response(identity.subject, buzz_event, http_request):
            return _completion(completion_id, {"role": "assistant", "content": ""})
        _log_buzz_reply(buzz_event, content, delivery="direct_content_no_tool")
    return _completion(completion_id, {"role": "assistant", "content": content})


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8200)


if __name__ == "__main__":
    main()
