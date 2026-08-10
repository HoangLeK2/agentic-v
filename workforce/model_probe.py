"""Explicit deployment probe for role-specific OpenAI-compatible models."""

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from os import getenv
from pathlib import Path
from typing import Any

import httpx

from app.settings import ModelRole, model_for


@dataclass(frozen=True)
class ModelProbeResult:
    model_id: str
    reachable: bool
    structured_output: bool
    tool_calling: bool
    streaming: bool
    expected_context: bool
    within_timeout: bool
    detail: str

    @property
    def passed(self) -> bool:
        return all(
            (
                self.reachable,
                self.structured_output,
                self.tool_calling,
                self.streaming,
                self.expected_context,
                self.within_timeout,
            )
        )


def required_model_ids() -> tuple[str, ...]:
    return tuple(sorted({model_for(role).id for role in ModelRole}))


def _chat_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/chat/completions"


def _headers() -> dict[str, str]:
    key = getenv("OPENAI_API_KEY")
    return {"Authorization": f"Bearer {key}"} if key else {}


def _model_context(model: dict[str, Any]) -> int | None:
    for key in ("context_length", "context_window", "max_context_length"):
        value = model.get(key)
        if isinstance(value, int):
            return value
    return None


def probe_model(client: httpx.Client, base_url: str, model: dict[str, Any], timeout_seconds: float) -> ModelProbeResult:
    model_id = model["id"]
    started = time.monotonic()
    errors: list[str] = []
    reachable = False
    structured = False
    tools = False
    streaming = False

    try:
        response = client.post(
            _chat_url(base_url),
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": "Reply with ok."}],
                "max_tokens": 16,
            },
        )
        response.raise_for_status()
        reachable = bool(response.json().get("choices"))
    except Exception as exc:
        errors.append(f"reachable:{type(exc).__name__}")

    try:
        response = client.post(
            _chat_url(base_url),
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": 'Return {"ok": true}.'}],
                "max_tokens": 64,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "model_probe",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {"ok": {"type": "boolean"}},
                            "required": ["ok"],
                            "additionalProperties": False,
                        },
                    },
                },
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        structured = json.loads(content).get("ok") is True
    except Exception as exc:
        errors.append(f"structured_output:{type(exc).__name__}")

    try:
        response = client.post(
            _chat_url(base_url),
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": "Call probe with value ok."}],
                "max_tokens": 64,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "probe",
                            "description": "Deployment capability probe",
                            "parameters": {
                                "type": "object",
                                "properties": {"value": {"type": "string"}},
                                "required": ["value"],
                            },
                        },
                    }
                ],
                "tool_choice": {"type": "function", "function": {"name": "probe"}},
            },
        )
        response.raise_for_status()
        tools = bool(response.json()["choices"][0]["message"].get("tool_calls"))
    except Exception as exc:
        errors.append(f"tool_calling:{type(exc).__name__}")

    try:
        with client.stream(
            "POST",
            _chat_url(base_url),
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": "Reply ok."}],
                "max_tokens": 16,
                "stream": True,
            },
        ) as response:
            response.raise_for_status()
            streaming = any(line.startswith("data:") and "[DONE]" not in line for line in response.iter_lines())
    except Exception as exc:
        errors.append(f"streaming:{type(exc).__name__}")

    elapsed = time.monotonic() - started
    expected_context = int(getenv("WORKFORCE_MODEL_EXPECTED_CONTEXT", "128000"))
    reported_context = _model_context(model)
    context_ok = reported_context is not None and reported_context >= expected_context
    if reported_context is None:
        errors.append("expected_context:not_reported")
    elif not context_ok:
        errors.append(f"expected_context:{reported_context}<{expected_context}")
    if elapsed > timeout_seconds:
        errors.append(f"timeout:{elapsed:.2f}s>{timeout_seconds:.2f}s")

    return ModelProbeResult(
        model_id=model_id,
        reachable=reachable,
        structured_output=structured,
        tool_calling=tools,
        streaming=streaming,
        expected_context=context_ok,
        within_timeout=elapsed <= timeout_seconds,
        detail=", ".join(errors) or "all capabilities verified",
    )


def run_probe() -> dict[str, Any]:
    base_url = getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    timeout_seconds = float(getenv("WORKFORCE_MODEL_PROBE_TIMEOUT_SECONDS", "60"))
    required = required_model_ids()
    with httpx.Client(headers=_headers(), timeout=httpx.Timeout(timeout_seconds, connect=10)) as client:
        response = client.get(f"{base_url.rstrip('/')}/models")
        response.raise_for_status()
        models = {item["id"]: item for item in response.json().get("data", [])}
        results = []
        for model_id in required:
            if model_id not in models:
                results.append(
                    ModelProbeResult(
                        model_id=model_id,
                        reachable=False,
                        structured_output=False,
                        tool_calling=False,
                        streaming=False,
                        expected_context=False,
                        within_timeout=False,
                        detail="model id is not exposed by the endpoint",
                    )
                )
            else:
                results.append(probe_model(client, base_url, models[model_id], timeout_seconds))
    return {
        "passed": all(result.passed for result in results),
        "generated_at": datetime.now(UTC).isoformat(),
        "base_url": base_url.rstrip("/"),
        "expected_context": int(getenv("WORKFORCE_MODEL_EXPECTED_CONTEXT", "128000")),
        "models": [asdict(result) for result in results],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_probe()
    rendered = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
