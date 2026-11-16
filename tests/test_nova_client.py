import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import awscrt.exceptions
from aws_sdk_bedrock_runtime.models import (
    ContentBlockText,
    ConverseOutputMessage,
    ConversationRole,
    Message,
)

from app.integrations import nova_client


class FakeBody:
    pass


class FakeInputStream:
    def __init__(self, body, send_error=None, close_gate=None):
        self._writer = body
        self.closed = False
        self.close_calls = 0
        self.send_error = send_error
        self.close_gate = close_gate
        self.close_started = asyncio.Event()
        self.close_finished = asyncio.Event()

    async def send(self, _event):
        if self.send_error is not None:
            raise self.send_error

    async def close(self):
        self.close_calls += 1
        self.close_started.set()
        try:
            if self.close_gate is not None:
                await self.close_gate.wait()
            self.closed = True
        finally:
            self.close_finished.set()


class FakeOutputStream:
    def __init__(self):
        self.close = AsyncMock()


class FakeDuplexStream:
    def __init__(
        self,
        body,
        *,
        send_error=None,
        receive_error=None,
        close_gate=None,
    ):
        self.input_stream = FakeInputStream(
            body,
            send_error=send_error,
            close_gate=close_gate,
        )
        self.output_stream = FakeOutputStream()
        self.receive_error = receive_error

    async def await_output(self):
        return None, self

    async def receive(self):
        if self.receive_error is not None:
            raise self.receive_error
        return None


class NovaCRTWriterTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_generator_binds_its_task_to_its_request_body(self):
        body = FakeBody()
        client = object.__new__(nova_client._NovaCRTHTTPClient)

        async def fake_parent_generator(_client, _body):
            yield b"request chunk"

        async def consume_generator():
            return [
                chunk
                async for chunk in client._create_body_generator(body)
            ]

        with patch.object(
            nova_client.AWSCRTHTTPClient,
            "_create_body_generator",
            fake_parent_generator,
        ):
            writer_task = asyncio.create_task(consume_generator())
            self.assertEqual(await writer_task, [b"request chunk"])

        self.assertIs(
            getattr(body, nova_client._CRT_WRITER_TASK_ATTRIBUTE),
            writer_task,
        )

    async def test_two_concurrent_streams_keep_independent_writer_tasks(self):
        body_one = FakeBody()
        body_two = FakeBody()
        stream_one = FakeDuplexStream(body_one)
        stream_two = FakeDuplexStream(body_two)
        client = object.__new__(nova_client._NovaCRTHTTPClient)
        started_one = asyncio.Event()
        started_two = asyncio.Event()
        release_one = asyncio.Event()
        release_two = asyncio.Event()
        release_by_body = {body_one: release_one, body_two: release_two}
        started_by_body = {body_one: started_one, body_two: started_two}

        async def blocked_parent_generator(_client, body):
            started_by_body[body].set()
            await release_by_body[body].wait()
            yield b"chunk"

        async def consume(body):
            async for _chunk in client._create_body_generator(body):
                pass

        with patch.object(
            nova_client.AWSCRTHTTPClient,
            "_create_body_generator",
            blocked_parent_generator,
        ):
            task_one = asyncio.create_task(consume(body_one))
            task_two = asyncio.create_task(consume(body_two))
            await asyncio.gather(started_one.wait(), started_two.wait())

            self.assertIs(
                getattr(body_one, nova_client._CRT_WRITER_TASK_ATTRIBUTE),
                task_one,
            )
            self.assertIs(
                getattr(body_two, nova_client._CRT_WRITER_TASK_ATTRIBUTE),
                task_two,
            )

            release_one.set()
            await nova_client._close_input_stream_and_wait(stream_one)
            self.assertTrue(task_one.done())
            self.assertFalse(task_two.done())

            release_two.set()
            await nova_client._close_input_stream_and_wait(stream_two)
            await asyncio.gather(task_one, task_two)

    async def test_writer_start_timeout_reports_cleanup_failure(self):
        stream = FakeDuplexStream(FakeBody())
        with patch.object(nova_client, "_CRT_WRITER_START_TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(
                nova_client.NovaWriterCleanupError,
                "writer task to start",
            ):
                await nova_client._close_input_stream_and_wait(stream)

        self.assertTrue(stream.input_stream.closed)

    async def test_writer_wait_timeout_does_not_cancel_writer(self):
        body = FakeBody()
        stream = FakeDuplexStream(body)
        writer_started = asyncio.Event()
        release_writer = asyncio.Event()

        async def blocked_writer():
            writer_started.set()
            await release_writer.wait()

        writer_task = asyncio.create_task(blocked_writer())
        nova_client._associate_crt_writer_task(body, writer_task)
        await writer_started.wait()

        with patch.object(nova_client, "_CRT_WRITER_WAIT_TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(
                nova_client.NovaWriterCleanupError,
                "writer task to stop",
            ):
                await nova_client._close_input_stream_and_wait(stream)

            self.assertFalse(writer_task.cancelled())
            self.assertFalse(writer_task.done())
            release_writer.set()
            await writer_task

    async def test_input_close_timeout_does_not_wait_for_cancellation(self):
        body = FakeBody()
        close_gate = asyncio.Event()
        stream = FakeDuplexStream(body, close_gate=close_gate)
        writer_task = asyncio.create_task(self._return_none())
        await writer_task
        nova_client._associate_crt_writer_task(body, writer_task)

        with patch.object(nova_client, "_CRT_WRITER_WAIT_TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(
                nova_client.NovaWriterCleanupError,
                "Timed out closing",
            ):
                await nova_client._close_input_stream_and_wait(stream)

        self.assertTrue(stream.input_stream.closed is False)

    async def test_http_stream_completed_error_is_not_suppressed(self):
        body = FakeBody()
        stream = FakeDuplexStream(body)
        writer_task = asyncio.create_task(
            self._raise(awscrt.exceptions.from_code(2080))
        )
        nova_client._associate_crt_writer_task(body, writer_task)
        await asyncio.sleep(0)

        with self.assertRaises(awscrt.exceptions.AwsCrtError) as raised:
            await nova_client._close_input_stream_and_wait(stream)

        self.assertEqual(raised.exception.code, 2080)
        self.assertEqual(
            raised.exception.name,
            "AWS_ERROR_HTTP_STREAM_HAS_COMPLETED",
        )

    async def test_unexpected_writer_error_propagates(self):
        body = FakeBody()
        stream = FakeDuplexStream(body)
        writer_task = asyncio.create_task(
            self._raise(RuntimeError("unexpected writer failure"))
        )
        nova_client._associate_crt_writer_task(body, writer_task)
        await asyncio.sleep(0)

        with self.assertRaisesRegex(RuntimeError, "unexpected writer failure"):
            await nova_client._close_input_stream_and_wait(stream)

    async def test_cleanup_after_send_failure_closes_writer_output_and_client(self):
        body = FakeBody()
        stream = FakeDuplexStream(
            body,
            send_error=OSError("send failed"),
        )
        writer_task = asyncio.create_task(self._return_none())
        await writer_task
        nova_client._associate_crt_writer_task(body, writer_task)
        client = AsyncMock()

        with self.assertRaisesRegex(OSError, "send failed"):
            await nova_client.send_event(stream, {"event": {"test": {}}})

        await nova_client.close_nova(client, stream)

        self.assertTrue(stream.input_stream.closed)
        stream.output_stream.close.assert_awaited_once()
        client.close.assert_awaited_once()

    async def test_cleanup_after_receive_failure_closes_all_streams(self):
        body = FakeBody()
        stream = FakeDuplexStream(
            body,
            receive_error=RuntimeError("receive failed"),
        )
        writer_task = asyncio.create_task(self._return_none())
        await writer_task
        nova_client._associate_crt_writer_task(body, writer_task)
        client = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "receive failed"):
            await nova_client.receive_events(stream)

        await nova_client.close_nova(client, stream)

        self.assertTrue(stream.input_stream.closed)
        stream.output_stream.close.assert_awaited_once()
        client.close.assert_awaited_once()

    async def test_input_cleanup_is_idempotent(self):
        body = FakeBody()
        stream = FakeDuplexStream(body)
        writer_task = asyncio.create_task(self._return_none())
        await writer_task
        nova_client._associate_crt_writer_task(body, writer_task)

        await nova_client._close_input_stream_and_wait(stream)
        await nova_client._close_input_stream_and_wait(stream)

        self.assertEqual(stream.input_stream.close_calls, 1)

    async def test_close_nova_attempts_all_cleanup_after_writer_start_timeout(self):
        body = FakeBody()
        stream = FakeDuplexStream(body)
        client = AsyncMock()
        stream.output_stream.close.side_effect = RuntimeError("output close failed")
        client.close.side_effect = RuntimeError("client close failed")

        with patch.object(nova_client, "_CRT_WRITER_START_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(nova_client.NovaWriterCleanupError) as raised:
                await nova_client.close_nova(client, stream)

            self.assertIn("writer task to start", str(raised.exception))

            late_writer = asyncio.create_task(self._return_none())
            nova_client._associate_crt_writer_task(body, late_writer)
            await late_writer
            with self.assertRaises(
                nova_client.NovaWriterCleanupError
            ) as repeated_error:
                await nova_client._close_input_stream_and_wait(stream)

        self.assertIs(repeated_error.exception, raised.exception)
        stream.output_stream.close.assert_awaited_once()
        client.close.assert_awaited_once()

    async def test_close_nova_attempts_all_cleanup_after_writer_wait_timeout(self):
        body = FakeBody()
        stream = FakeDuplexStream(body)
        writer_started = asyncio.Event()
        release_writer = asyncio.Event()

        async def blocked_writer():
            writer_started.set()
            await release_writer.wait()

        writer_task = asyncio.create_task(blocked_writer())
        nova_client._associate_crt_writer_task(body, writer_task)
        await writer_started.wait()

        client = AsyncMock()

        async def close_client():
            release_writer.set()

        client.close.side_effect = close_client
        with patch.object(nova_client, "_CRT_WRITER_WAIT_TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(
                nova_client.NovaWriterCleanupError,
                "writer task to stop",
            ):
                await nova_client.close_nova(client, stream)

        await writer_task
        self.assertFalse(writer_task.cancelled())
        stream.output_stream.close.assert_awaited_once()
        client.close.assert_awaited_once()

    async def test_close_nova_attempts_all_cleanup_after_input_close_timeout(self):
        body = FakeBody()
        close_gate = asyncio.Event()
        stream = FakeDuplexStream(body, close_gate=close_gate)
        writer_task = asyncio.create_task(self._return_none())
        await writer_task
        nova_client._associate_crt_writer_task(body, writer_task)
        client = AsyncMock()

        with patch.object(nova_client, "_CRT_WRITER_WAIT_TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(
                nova_client.NovaWriterCleanupError,
                "Timed out closing",
            ) as raised:
                await nova_client.close_nova(client, stream)

            with self.assertRaises(
                nova_client.NovaWriterCleanupError
            ) as repeated_error:
                await nova_client._close_input_stream_and_wait(stream)

        self.assertIs(repeated_error.exception, raised.exception)
        stream.output_stream.close.assert_awaited_once()
        client.close.assert_awaited_once()
        self.assertTrue(stream.input_stream.close_started.is_set())
        self.assertFalse(stream.input_stream.close_finished.is_set())
        close_gate.set()
        await asyncio.wait_for(stream.input_stream.close_finished.wait(), 0.1)
        self.assertTrue(stream.input_stream.closed)

    async def test_close_nova_preserves_input_error_over_close_errors(self):
        body = FakeBody()
        stream = FakeDuplexStream(body, close_gate=asyncio.Event())
        writer_task = asyncio.create_task(self._return_none())
        await writer_task
        nova_client._associate_crt_writer_task(body, writer_task)
        stream.output_stream.close.side_effect = RuntimeError("output failed")
        client = AsyncMock()
        client.close.side_effect = RuntimeError("client failed")

        with patch.object(nova_client, "_CRT_WRITER_WAIT_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(nova_client.NovaWriterCleanupError) as raised:
                await nova_client.close_nova(client, stream)

        self.assertEqual(
            str(raised.exception),
            "Timed out closing the Nova input publisher",
        )
        stream.output_stream.close.assert_awaited_once()
        client.close.assert_awaited_once()

    async def test_generate_call_summary_uses_transcript_and_closes_client(self):
        transcript = [
            {"role": "USER", "text": "My name is Sam.", "timestamp": "2026-11-15T17:00:00Z"},
            {"role": "ASSISTANT", "text": "Would you like a viewing?", "timestamp": "2026-11-15T17:00:02Z"},
        ]
        response = type(
            "ConverseResponse",
            (),
            {
                "output": ConverseOutputMessage(
                    value=Message(
                        role=ConversationRole.ASSISTANT,
                        content=[ContentBlockText(value="Caller Sam requested property details.")],
                    )
                )
            },
        )()
        http_client = AsyncMock()
        client = AsyncMock()
        client.converse.return_value = response
        config = object()

        with (
            patch.object(nova_client, "AWSCRTHTTPClient", return_value=http_client),
            patch.object(nova_client, "IMDSCredentialsResolver", return_value=object()),
            patch.object(
                nova_client.AsyncBedrockRuntimeConfig,
                "resolve",
                new_callable=AsyncMock,
                return_value=config,
            ) as resolve_config,
            patch.object(
                nova_client,
                "AsyncBedrockRuntimeClient",
                return_value=client,
            ),
        ):
            summary = await nova_client.generate_call_summary(transcript)

        self.assertEqual(summary, "Caller Sam requested property details.")
        client.converse.assert_awaited_once()
        request = client.converse.await_args.args[0]
        self.assertEqual(request.model_id, "us.amazon.nova-2-lite-v1:0")
        self.assertEqual(request.model_id, nova_client.SUMMARY_MODEL_ID)
        self.assertEqual(resolve_config.await_args.kwargs["region"], "us-east-1")
        self.assertIn("only facts explicitly", request.system[0].value)
        self.assertIn('"My name is Sam."', request.messages[0].content[0].value)
        client.close.assert_awaited_once()
        http_client.close.assert_not_awaited()

    async def test_generate_call_summary_closes_client_after_model_failure(self):
        http_client = AsyncMock()
        client = AsyncMock()
        client.converse.side_effect = RuntimeError("model unavailable")

        with (
            patch.object(nova_client, "AWSCRTHTTPClient", return_value=http_client),
            patch.object(nova_client, "IMDSCredentialsResolver", return_value=object()),
            patch.object(
                nova_client.AsyncBedrockRuntimeConfig,
                "resolve",
                new_callable=AsyncMock,
                return_value=object(),
            ),
            patch.object(
                nova_client,
                "AsyncBedrockRuntimeClient",
                return_value=client,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "model unavailable"):
                await nova_client.generate_call_summary(
                    [{"role": "USER", "text": "Tell me about the house."}]
                )
        client.close.assert_awaited_once()
        http_client.close.assert_not_awaited()

    async def _extract_lead_response(self, response_text):
        response = type(
            "ConverseResponse",
            (),
            {
                "output": ConverseOutputMessage(
                    value=Message(
                        role=ConversationRole.ASSISTANT,
                        content=[ContentBlockText(value=response_text)],
                    )
                )
            },
        )()
        http_client = AsyncMock()
        client = AsyncMock()
        client.converse.return_value = response
        with (
            patch.object(nova_client, "AWSCRTHTTPClient", return_value=http_client),
            patch.object(nova_client, "IMDSCredentialsResolver", return_value=object()),
            patch.object(
                nova_client.AsyncBedrockRuntimeConfig,
                "resolve",
                new_callable=AsyncMock,
                return_value=object(),
            ) as resolve_config,
            patch.object(nova_client, "AsyncBedrockRuntimeClient", return_value=client),
        ):
            result = await nova_client.extract_call_lead(
                [{"role": "USER", "text": "Could I view the house?"}]
            )

        request = client.converse.await_args.args[0]
        self.assertEqual(request.model_id, "us.amazon.nova-2-lite-v1:0")
        self.assertEqual(resolve_config.await_args.kwargs["region"], "us-east-1")
        self.assertIn("only use user messages", request.system[0].value.lower())
        client.close.assert_awaited_once()
        return result

    async def test_extract_call_lead_returns_valid_structured_data(self):
        extraction = await self._extract_lead_response(
            json.dumps(
                {
                    "name": "Sam",
                    "email": "sam@example.test",
                    "lead_type": "buyer",
                    "intent": "viewing_request",
                    "budget_min": 500000,
                    "budget_max": 650000,
                    "timeline": "within 2 weeks",
                    "interested_property": "12 Main Street",
                    "summary": "Caller requested a viewing of 12 Main Street.",
                    "evidence": ["Caller asked to view the house."],
                }
            )
        )
        self.assertEqual(extraction["name"], "Sam")
        self.assertEqual(extraction["intent"], "viewing_request")
        self.assertEqual(extraction["budget_max"], 650000)
        self.assertEqual(extraction["evidence"], ["Caller asked to view the house."])

    async def test_extract_call_lead_accepts_undisclosed_caller_information(self):
        extraction = await self._extract_lead_response(
            json.dumps(
                {
                    "name": None,
                    "email": None,
                    "lead_type": "unknown",
                    "intent": "unknown",
                    "budget_min": None,
                    "budget_max": None,
                    "timeline": None,
                    "interested_property": None,
                    "summary": "Caller information was not disclosed.",
                    "evidence": [],
                }
            )
        )
        self.assertIsNone(extraction["name"])
        self.assertIsNone(extraction["email"])
        self.assertEqual(extraction["lead_type"], "unknown")

    async def test_extract_call_lead_rejects_malformed_json(self):
        with self.assertRaisesRegex(ValueError, "malformed JSON"):
            await self._extract_lead_response("not json")

    async def test_extract_call_lead_rejects_invalid_fields(self):
        response = {
            "name": None,
            "email": None,
            "lead_type": "buyer",
            "intent": "made_up_intent",
            "budget_min": None,
            "budget_max": None,
            "timeline": None,
            "interested_property": None,
            "summary": "No caller details.",
            "evidence": [],
        }
        with self.assertRaisesRegex(ValueError, "intent is invalid"):
            await self._extract_lead_response(json.dumps(response))

    @staticmethod
    async def _raise(error):
        raise error

    @staticmethod
    async def _return_none():
        return None


if __name__ == "__main__":
    unittest.main()