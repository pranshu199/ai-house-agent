import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import awscrt.exceptions

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

    @staticmethod
    async def _raise(error):
        raise error

    @staticmethod
    async def _return_none():
        return None


if __name__ == "__main__":
    unittest.main()