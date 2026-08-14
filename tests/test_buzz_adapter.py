import asyncio
import shlex
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from services.buzz_adapter.auth import BuzzIdentity
from services.buzz_adapter.client import extract_content, parse_sse_event, present_content
from services.buzz_adapter.models import ChatCompletionRequest, extract_buzz_event, latest_user_message, render_messages
from services.buzz_adapter.server import (
    _buzz_event_dedupe,
    _buzz_event_results,
    _buzz_event_tasks,
    _log_buzz_reply,
    _pending_approvals,
    _session_id,
    app,
)
from services.buzz_adapter.settings import BuzzAdapterSettings

CHANNEL_ID = "39624acb-09ba-4806-8751-e72bd8f38edf"
EVENT_ID = "e9c04169169d8371a22df0adb7b30b509cf5d3f4e3d3814a016e52e8464d25a5"


def buzz_event_prompt(content: str, *, kind: int = 9) -> str:
    return (
        "[Context]\n"
        "Scope: dm\n"
        f"Channel: DM (#{CHANNEL_ID})\n"
        "Conversation context included below.\n"
        "[Buzz event: all]\n"
        f"Event ID: {EVENT_ID}\n"
        f"Channel: DM (#{CHANNEL_ID})\n"
        f"Kind: {kind}\n"
        "From: Hoang Le (owner)\n"
        "Time: 2026-08-09T14:56:07+00:00\n"
        f"Content: {content}\n"
        f'Tags: [["h","{CHANNEL_ID}"]]'
    )


class BuzzAdapterTest(TestCase):
    def setUp(self) -> None:
        _buzz_event_dedupe.clear()
        _buzz_event_results.clear()
        _buzz_event_tasks.clear()
        _pending_approvals.clear()

    def test_renders_openai_messages_with_roles(self) -> None:
        request = ChatCompletionRequest(
            model="buzz-agent",
            messages=[
                {"role": "system", "content": "Be precise"},
                {"role": "user", "content": [{"type": "text", "text": "Review this"}]},
            ],
        )

        self.assertEqual(render_messages(request.messages), "[system]\nBe precise\n\n[user]\nReview this")

    def test_buzz_settings_default_to_long_coding_timeout(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "BUZZ_IDENTITIES_FILE": "/tmp/buzz-identities.json",
                "BUZZ_TOKEN_PEPPER": "pepper",
                "BUZZ_JWT_PRIVATE_KEY_FILE": "/tmp/buzz-jwt-private.pem",
            },
            clear=True,
        ):
            settings = BuzzAdapterSettings.from_env()

        self.assertEqual(settings.request_timeout_seconds, 900)
        self.assertEqual(settings.agentos_run_timeout_seconds, 3600)
        self.assertEqual(settings.event_dedupe_seconds, 3900)

    def test_buzz_dedupe_outlives_custom_request_timeout(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "BUZZ_IDENTITIES_FILE": "/tmp/buzz-identities.json",
                "BUZZ_TOKEN_PEPPER": "pepper",
                "BUZZ_JWT_PRIVATE_KEY_FILE": "/tmp/buzz-jwt-private.pem",
                "BUZZ_REQUEST_TIMEOUT_SECONDS": "1200",
                "BUZZ_EVENT_DEDUPE_SECONDS": "60",
            },
            clear=True,
        ):
            settings = BuzzAdapterSettings.from_env()

        self.assertEqual(settings.request_timeout_seconds, 1200)
        self.assertEqual(settings.agentos_run_timeout_seconds, 3600)
        self.assertEqual(settings.event_dedupe_seconds, 3900)

    def test_buzz_background_timeout_can_exceed_bridge_timeout(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "BUZZ_IDENTITIES_FILE": "/tmp/buzz-identities.json",
                "BUZZ_TOKEN_PEPPER": "pepper",
                "BUZZ_JWT_PRIVATE_KEY_FILE": "/tmp/buzz-jwt-private.pem",
                "BUZZ_REQUEST_TIMEOUT_SECONDS": "900",
                "BUZZ_AGENTOS_RUN_TIMEOUT_SECONDS": "5400",
                "BUZZ_EVENT_DEDUPE_SECONDS": "60",
            },
            clear=True,
        ):
            settings = BuzzAdapterSettings.from_env()

        self.assertEqual(settings.request_timeout_seconds, 900)
        self.assertEqual(settings.agentos_run_timeout_seconds, 5400)
        self.assertEqual(settings.event_dedupe_seconds, 5700)

    def test_extracts_team_run_content(self) -> None:
        self.assertEqual(extract_content({"content": "done", "run_id": "run-1"}), "done")

    def test_extracts_structured_team_run_content(self) -> None:
        content = extract_content({"content": {"status": "completed", "summary": "done"}})

        self.assertEqual(content, '{"status": "completed", "summary": "done"}')

    def test_presents_structured_outcome_as_summary_only(self) -> None:
        content = present_content({"content": {"status": "completed", "summary": "Đã đọc repository."}})

        self.assertEqual(content, "Đã đọc repository.")

    def test_removes_workforce_outcome_footer_from_visible_answer(self) -> None:
        content = present_content(
            {
                "content": (
                    "Agno là framework dùng để xây agent.\n\n"
                    "**WorkforceOutcome**\n"
                    "- **Trạng thái:** Hoàn tất có giới hạn\n"
                    "- **Kết quả:** Đã giải thích Agno\n"
                    "- **Bằng chứng:** Đối chiếu ngữ cảnh\n"
                    "- **Giới hạn:** Chưa đọc source\n"
                    "- **Bước tiếp theo:** Gửi thêm context"
                )
            }
        )

        self.assertEqual(content, "Agno là framework dùng để xây agent.")

    def test_contract_only_response_uses_result_without_metadata(self) -> None:
        content = present_content(
            {
                "content": (
                    "WorkforceOutcome\n"
                    "Trạng thái: Hoàn tất có giới hạn\n"
                    "Kết quả: Đã giải thích các khả năng phổ biến của Agono.\n"
                    "Bằng chứng: Đối chiếu ngữ cảnh Device Farm.\n"
                    "Giới hạn: Chưa có code.\n"
                    "Bước tiếp theo: Gửi thêm context."
                )
            }
        )

        self.assertEqual(content, "Đã giải thích các khả năng phổ biến của Agono.")

    def test_parses_agentos_sse(self) -> None:
        event = parse_sse_event(["event: RunContent", 'data: {"content":"partial"}'])

        self.assertEqual(event, ("RunContent", {"content": "partial"}))

    def test_session_id_is_scoped_to_authenticated_subject(self) -> None:
        self.assertNotEqual(_session_id("buzz:alice", "thread-1"), _session_id("buzz:bob", "thread-1"))

    def test_extracts_only_current_buzz_event_from_conversation_context(self) -> None:
        event = extract_buzz_event(
            [
                {
                    "role": "user",
                    "content": buzz_event_prompt(
                        "Harden bearer auth\nwith regression tests",
                    ),
                }
            ]
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.channel_id, CHANNEL_ID)
        self.assertEqual(event.event_id, EVENT_ID)
        self.assertEqual(event.kind, 9)
        self.assertEqual(event.content, "Harden bearer auth\nwith regression tests")

    def test_extracts_empty_control_event_without_reusing_conversation_history(self) -> None:
        event = extract_buzz_event(
            [
                {
                    "role": "user",
                    "content": buzz_event_prompt("", kind=20002),
                }
            ]
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.kind, 20002)
        self.assertEqual(event.content, "")

    def test_latest_user_message_avoids_replaying_upstream_history(self) -> None:
        request = ChatCompletionRequest.model_validate(
            {
                "model": "buzz-agent",
                "messages": [
                    {"role": "user", "content": "old question"},
                    {"role": "assistant", "content": "old answer"},
                    {"role": "user", "content": "new question"},
                ],
            }
        )

        self.assertEqual(latest_user_message(request.messages), "new question")

    def test_logs_buzz_reply_preview_when_enabled(self) -> None:
        event = extract_buzz_event([{"role": "user", "content": buzz_event_prompt("Review auth")}])
        self.assertIsNotNone(event)
        assert event is not None
        runtime = MagicMock()
        runtime.settings = SimpleNamespace(log_reply_preview=True, reply_preview_chars=11)

        with (
            patch("services.buzz_adapter.server.runtime", return_value=runtime),
            self.assertLogs("uvicorn.error", level="INFO") as logs,
        ):
            _log_buzz_reply(event, "hello\nworld with more text", delivery="tool_call")

        line = logs.output[0]
        self.assertIn("delivery=tool_call", line)
        self.assertIn(f"channel={CHANNEL_ID}", line)
        self.assertIn("chars=26", line)
        self.assertIn("preview=hello\\nworld...[truncated]", line)

    def test_chat_completion_authenticates_user_and_calls_only_virtual_model(self) -> None:
        runtime = MagicMock()
        runtime.identities.get.return_value.authenticate.return_value = BuzzIdentity(
            subject="buzz:alice", token_hash="unused"
        )
        runtime.issuer.issue.return_value = "scoped-jwt"
        runtime.client.run = AsyncMock(return_value={"content": "done"})

        with patch("services.buzz_adapter.server.runtime", return_value=runtime):
            response = TestClient(app).post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer user-token"},
                json={"model": "buzz-agent", "messages": [{"role": "user", "content": "ship it"}]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "done")
        runtime.client.run.assert_awaited_once()
        self.assertEqual(runtime.client.run.await_args.kwargs["subject"], "buzz:alice")
        self.assertEqual(runtime.client.run.await_args.kwargs["token"], "scoped-jwt")

    def test_buzz_event_returns_fixed_publish_tool_call_and_channel_session(self) -> None:
        runtime = MagicMock()
        runtime.identities.get.return_value.authenticate.return_value = BuzzIdentity(
            subject="buzz:alice", token_hash="unused"
        )
        runtime.issuer.issue.return_value = "scoped-jwt"
        runtime.client.run = AsyncMock(return_value={"content": "It's safe; $(touch /tmp/nope)"})

        with patch("services.buzz_adapter.server.runtime", return_value=runtime):
            response = TestClient(app).post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer user-token"},
                json={
                    "model": "buzz-agent",
                    "messages": [{"role": "user", "content": buzz_event_prompt("Review auth")}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {"name": "buzz-dev-mcp__shell", "parameters": {"type": "object"}},
                        }
                    ],
                },
            )

        self.assertEqual(response.status_code, 200)
        choice = response.json()["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        tool_call = choice["message"]["tool_calls"][0]
        self.assertEqual(tool_call["function"]["name"], "buzz-dev-mcp__shell")
        arguments = __import__("json").loads(tool_call["function"]["arguments"])
        self.assertIn(f"buzz messages send --channel {CHANNEL_ID}", arguments["command"])
        self.assertIn(f"--reply-to {EVENT_ID}", arguments["command"])
        quoted_content = shlex.quote("It's safe; $(touch /tmp/nope)")
        self.assertTrue(arguments["command"].startswith(f"printf '%s' {quoted_content} |"))
        self.assertEqual(runtime.client.run.await_args.kwargs["prompt"], "Review auth")
        self.assertEqual(
            runtime.client.run.await_args.kwargs["session_id"],
            _session_id("buzz:alice", CHANNEL_ID),
        )

    def test_duplicate_buzz_event_is_suppressed_without_agentos_call(self) -> None:
        runtime = MagicMock()
        runtime.identities.get.return_value.authenticate.return_value = BuzzIdentity(
            subject="buzz:alice", token_hash="unused"
        )
        runtime.issuer.issue.return_value = "scoped-jwt"
        runtime.settings.event_dedupe_seconds = 900
        runtime.client.run = AsyncMock()
        _buzz_event_dedupe[("buzz:alice", EVENT_ID)] = 9_999_999_999.0

        with (
            patch("services.buzz_adapter.server.runtime", return_value=runtime),
            self.assertLogs("uvicorn.error", level="INFO") as logs,
        ):
            response = TestClient(app).post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer user-token"},
                json={
                    "model": "buzz-agent",
                    "messages": [{"role": "user", "content": buzz_event_prompt("Review auth")}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {"name": "buzz-dev-mcp__shell", "parameters": {"type": "object"}},
                        }
                    ],
                },
            )

        self.assertEqual(response.status_code, 200)
        choice = response.json()["choices"][0]
        self.assertEqual(choice["message"]["content"], "")
        self.assertNotIn("tool_calls", choice["message"])
        runtime.client.run.assert_not_awaited()
        self.assertIn("Buzz duplicate event suppressed", logs.output[0])

    def test_duplicate_buzz_event_joins_inflight_run_without_starting_another_job(self) -> None:
        runtime = MagicMock()
        runtime.identities.get.return_value.authenticate.return_value = BuzzIdentity(
            subject="buzz:alice", token_hash="unused"
        )
        runtime.issuer.issue.return_value = "scoped-jwt"
        runtime.settings.event_dedupe_seconds = 900
        runtime.settings.log_reply_preview = False
        started = threading.Event()

        async def slow_run(**_kwargs):
            started.set()
            await asyncio.sleep(0.2)
            return {"content": "joined result"}

        runtime.client.run = AsyncMock(side_effect=slow_run)
        request = {
            "model": "buzz-agent",
            "messages": [{"role": "user", "content": buzz_event_prompt("Implement a large feature")}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "buzz-dev-mcp__shell", "parameters": {"type": "object"}},
                }
            ],
        }

        def post_event():
            return TestClient(app).post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer user-token"},
                json=request,
            )

        with (
            patch("services.buzz_adapter.server.runtime", return_value=runtime),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first_future = executor.submit(post_event)
            self.assertTrue(started.wait(timeout=2))
            second_future = executor.submit(post_event)
            responses = [first_future.result(timeout=5), second_future.result(timeout=5)]

        self.assertEqual(runtime.client.run.await_count, 1)
        self.assertTrue(all(response.status_code == 200 for response in responses))
        choices = [response.json()["choices"][0] for response in responses]
        tool_call_choices = [choice for choice in choices if choice["finish_reason"] == "tool_calls"]
        empty_choices = [choice for choice in choices if choice["message"].get("content") == ""]
        self.assertEqual(len(tool_call_choices), 1)
        self.assertEqual(len(empty_choices), 1)
        arguments = __import__("json").loads(
            tool_call_choices[0]["message"]["tool_calls"][0]["function"]["arguments"]
        )
        self.assertIn("joined result", arguments["command"])

    def test_buzz_event_timeout_returns_notice_and_keeps_dedupe(self) -> None:
        runtime = MagicMock()
        runtime.identities.get.return_value.authenticate.return_value = BuzzIdentity(
            subject="buzz:alice", token_hash="unused"
        )
        runtime.issuer.issue.return_value = "scoped-jwt"
        runtime.settings.event_dedupe_seconds = 900
        runtime.settings.request_timeout_seconds = 120
        runtime.settings.log_reply_preview = False
        runtime.client.run = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))
        request = {
            "model": "buzz-agent",
            "messages": [{"role": "user", "content": buzz_event_prompt("Implement a large feature")}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "buzz-dev-mcp__shell", "parameters": {"type": "object"}},
                }
            ],
        }

        with patch("services.buzz_adapter.server.runtime", return_value=runtime):
            first = TestClient(app).post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer user-token"},
                json=request,
            )
            second = TestClient(app).post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer user-token"},
                json=request,
            )

        self.assertEqual(first.status_code, 200)
        first_choice = first.json()["choices"][0]
        self.assertEqual(first_choice["finish_reason"], "tool_calls")
        arguments = __import__("json").loads(first_choice["message"]["tool_calls"][0]["function"]["arguments"])
        self.assertIn("chạy quá 120s", arguments["command"])
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["choices"][0]["message"]["content"], "")
        runtime.client.run.assert_awaited_once()

    def test_buzz_confirmation_continues_paused_apply_patch_run(self) -> None:
        runtime = MagicMock()
        runtime.identities.get.return_value.authenticate.return_value = BuzzIdentity(
            subject="buzz:alice", token_hash="unused"
        )
        runtime.issuer.issue.return_value = "scoped-jwt"
        runtime.issuer.issue_workspace_approval.return_value = "signed-approval"
        runtime.settings.workspace_approval_audience = "workspace-executor"
        runtime.client.run = AsyncMock(
            return_value={
                "run_id": "run-approval",
                "status": "PAUSED",
                "requirements": [
                    {
                        "id": "requirement-1",
                        "tool_execution": {
                            "tool_name": "apply_patch",
                            "tool_args": {
                                "workspace_id": "workspace-1",
                                "patch_text": "--- a/file.py\n+++ b/file.py\n@@\n-old\n+new",
                            },
                            "requires_confirmation": True,
                        },
                    }
                ],
            }
        )
        runtime.client.continue_run = AsyncMock(return_value={"status": "COMPLETED", "content": "Đã áp dụng patch."})
        request = {
            "model": "buzz-agent",
            "messages": [{"role": "user", "content": buzz_event_prompt("Cập nhật file.py")}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "buzz-dev-mcp__shell", "parameters": {"type": "object"}},
                }
            ],
        }

        with (
            patch("services.buzz_adapter.server.runtime", return_value=runtime),
            patch.dict(_pending_approvals, {}, clear=True),
        ):
            first = TestClient(app).post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer user-token"},
                json=request,
            )
            request["messages"] = [{"role": "user", "content": buzz_event_prompt("đồng ý cập nhật")}]
            second = TestClient(app).post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer user-token"},
                json=request,
            )

        self.assertEqual(first.status_code, 200)
        first_arguments = __import__("json").loads(
            first.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
        )
        self.assertIn("đồng ý cập nhật", first_arguments["command"])
        self.assertIn("--- a/file.py", first_arguments["command"])
        self.assertEqual(second.status_code, 200)
        runtime.client.run.assert_awaited_once()
        runtime.client.continue_run.assert_awaited_once()
        requirements = runtime.client.continue_run.await_args.kwargs["requirements"]
        self.assertTrue(requirements[0]["confirmation"])
        self.assertTrue(requirements[0]["tool_execution"]["confirmed"])
        self.assertEqual(
            requirements[0]["tool_execution"]["tool_args"]["approval_token"],
            "signed-approval",
        )
        runtime.issuer.issue_workspace_approval.assert_called_once_with(
            subject="buzz:alice",
            session_id=_session_id("buzz:alice", CHANNEL_ID),
            workspace_id="workspace-1",
            patch_text="--- a/file.py\n+++ b/file.py\n@@\n-old\n+new",
            audience="workspace-executor",
        )
        second_arguments = __import__("json").loads(
            second.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
        )
        self.assertIn("Đã áp dụng patch.", second_arguments["command"])

    def test_oversized_patch_is_not_stored_for_blind_approval(self) -> None:
        runtime = MagicMock()
        runtime.identities.get.return_value.authenticate.return_value = BuzzIdentity(
            subject="buzz:alice", token_hash="unused"
        )
        runtime.issuer.issue.return_value = "scoped-jwt"
        runtime.client.run = AsyncMock(
            return_value={
                "run_id": "run-approval",
                "status": "PAUSED",
                "requirements": [
                    {
                        "tool_execution": {
                            "tool_name": "apply_patch",
                            "tool_args": {"workspace_id": "workspace-1", "patch_text": "x" * 8_001},
                        }
                    }
                ],
            }
        )
        with (
            patch("services.buzz_adapter.server.runtime", return_value=runtime),
            patch.dict(_pending_approvals, {}, clear=True),
        ):
            response = TestClient(app).post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer user-token"},
                json={
                    "model": "buzz-agent",
                    "messages": [{"role": "user", "content": buzz_event_prompt("Cập nhật lớn")}],
                },
            )

            self.assertEqual(response.status_code, 200)
            self.assertIn("vượt quá 8.000", response.json()["choices"][0]["message"]["content"])
            self.assertEqual(_pending_approvals, {})

    def test_empty_buzz_control_event_is_ignored_without_agentos_call(self) -> None:
        runtime = MagicMock()
        runtime.identities.get.return_value.authenticate.return_value = BuzzIdentity(
            subject="buzz:alice", token_hash="unused"
        )
        runtime.client.run = AsyncMock()

        with patch("services.buzz_adapter.server.runtime", return_value=runtime):
            response = TestClient(app).post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer user-token"},
                json={
                    "model": "buzz-agent",
                    "messages": [{"role": "user", "content": buzz_event_prompt("", kind=20002)}],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "")
        runtime.client.run.assert_not_awaited()

    def test_nonempty_buzz_control_event_is_ignored_without_agentos_call(self) -> None:
        runtime = MagicMock()
        runtime.identities.get.return_value.authenticate.return_value = BuzzIdentity(
            subject="buzz:alice", token_hash="unused"
        )
        runtime.client.run = AsyncMock()

        with patch("services.buzz_adapter.server.runtime", return_value=runtime):
            response = TestClient(app).post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer user-token"},
                json={
                    "model": "buzz-agent",
                    "messages": [{"role": "user", "content": buzz_event_prompt("typing", kind=20002)}],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "")
        runtime.client.run.assert_not_awaited()

    def test_publish_tool_result_finishes_without_running_agentos_again(self) -> None:
        runtime = MagicMock()
        runtime.identities.get.return_value.authenticate.return_value = BuzzIdentity(
            subject="buzz:alice", token_hash="unused"
        )
        runtime.client.run = AsyncMock()

        with patch("services.buzz_adapter.server.runtime", return_value=runtime):
            response = TestClient(app).post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer user-token"},
                json={
                    "model": "buzz-agent",
                    "messages": [
                        {"role": "user", "content": buzz_event_prompt("Review auth")},
                        {"role": "assistant", "content": None, "tool_calls": []},
                        {"role": "tool", "content": '{"event_id":"published"}', "tool_call_id": "call-1"},
                    ],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["finish_reason"], "stop")
        runtime.client.run.assert_not_awaited()

    def test_chat_completion_rejects_missing_bearer(self) -> None:
        response = TestClient(app).post(
            "/v1/chat/completions",
            json={"model": "buzz-agent", "messages": [{"role": "user", "content": "ship it"}]},
        )

        self.assertEqual(response.status_code, 401)
