"""Minimal OpenAI chat-completions compatibility models."""

import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: Any = ""


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    user: str | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)


@dataclass(frozen=True)
class BuzzEvent:
    event_id: str
    channel_id: str
    kind: int
    content: str


_EVENT_ID = re.compile(r"(?m)^Event ID: ([0-9a-f]{64})$")
_CHANNEL_ID = re.compile(r"(?m)^Channel: .*\(#([0-9a-f-]{36})\)$")
_EVENT_KIND = re.compile(r"(?m)^Kind: (\d+)$")
_EVENT_CONTENT = re.compile(r"(?ms)^Content: ?(.*?)^Tags:")


def _text_content(message: ChatMessage) -> str:
    content = message.content
    if isinstance(content, list):
        text_parts = [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
        return "\n".join(part for part in text_parts if part)
    return content if isinstance(content, str) else str(content or "")


def extract_buzz_event(messages: list[ChatMessage] | list[dict[str, Any]]) -> BuzzEvent | None:
    parsed_messages = [
        message if isinstance(message, ChatMessage) else ChatMessage.model_validate(message) for message in messages
    ]
    for message in reversed(parsed_messages):
        if message.role != "user":
            continue
        content = _text_content(message)
        marker = content.rfind("[Buzz event:")
        if marker < 0:
            continue
        event_block = content[marker:]
        event_id = _EVENT_ID.search(event_block)
        channel_id = _CHANNEL_ID.search(event_block)
        kind = _EVENT_KIND.search(event_block)
        event_content = _EVENT_CONTENT.search(event_block)
        if event_id is None or channel_id is None or kind is None or event_content is None:
            return None
        channel = channel_id.group(1)
        try:
            UUID(channel)
        except ValueError:
            return None
        return BuzzEvent(
            event_id=event_id.group(1),
            channel_id=channel,
            kind=int(kind.group(1)),
            content=event_content.group(1).strip(),
        )
    return None


def render_messages(messages: list[ChatMessage]) -> str:
    rendered: list[str] = []
    for message in messages:
        content = _text_content(message)
        rendered.append(f"[{message.role}]\n{content}")
    prompt = "\n\n".join(rendered)
    if len(prompt) > 250_000:
        raise ValueError("Conversation exceeds the adapter input limit")
    return prompt
