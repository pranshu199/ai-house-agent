import asyncio
import json
import unittest
from types import SimpleNamespace

from app.api.twilio_webhooks import (
    _CallTranscriptBuffer,
    _forward_nova_audio_to_twilio,
)


class FakeNovaStream:
    def __init__(self, events):
        self.events = events
        self.index = 0

    async def await_output(self):
        return None, self

    async def receive(self):
        event = self.events[self.index]
        self.index += 1
        if isinstance(event, bytes):
            payload = event
        elif event is None:
            payload = None
        else:
            payload = json.dumps({"event": event}).encode("utf-8")
        return SimpleNamespace(
            value=SimpleNamespace(bytes_=payload),
        )


class FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


def text_content_start(content_id, role, stage):
    return {
        "contentId": content_id,
        "role": role,
        "type": "TEXT",
        "additionalModelFields": json.dumps({"generationStage": stage}),
    }


class NovaTranscriptTests(unittest.IsolatedAsyncioTestCase):
    async def receive_events(self, events):
        stream = FakeNovaStream(events)
        websocket = FakeWebSocket()
        transcript_buffer = _CallTranscriptBuffer()
        session_stopping = asyncio.Event()
        session_stopping.set()

        await _forward_nova_audio_to_twilio(
            stream,
            websocket,
            "stream-1",
            session_stopping,
            transcript_buffer,
        )
        return transcript_buffer.entries(), websocket

    async def test_captures_final_user_and_assistant_turns_separately(self):
        entries, _ = await self.receive_events([
            {"contentStart": text_content_start("user-1", "USER", "FINAL")},
            {"textOutput": {"contentId": "user-1", "content": "I like "}},
            {"textOutput": {"contentId": "user-1", "content": "the kitchen."}},
            {"contentEnd": {"contentId": "user-1", "stopReason": "END_TURN"}},
            {"contentStart": text_content_start("assistant-preview", "ASSISTANT", "SPECULATIVE")},
            {"textOutput": {"contentId": "assistant-preview", "content": "Preview text."}},
            {"contentEnd": {"contentId": "assistant-preview", "stopReason": "END_TURN"}},
            {"contentStart": text_content_start("assistant-1", "ASSISTANT", "FINAL")},
            {"textOutput": {"contentId": "assistant-1", "content": "The kitchen "}},
            {"textOutput": {"contentId": "assistant-1", "content": "was renovated."}},
            {"contentEnd": {"contentId": "assistant-1", "stopReason": "END_TURN"}},
            {"contentStart": text_content_start("assistant-2", "ASSISTANT", "FINAL")},
            {"textOutput": {"contentId": "assistant-2", "content": "Would you like a tour?"}},
            {"contentEnd": {"contentId": "assistant-2", "stopReason": "END_TURN"}},
            {"completionEnd": {}},
        ])

        self.assertEqual(
            entries,
            [
                {
                    "role": "USER",
                    "text": "I like the kitchen.",
                    "timestamp": entries[0]["timestamp"],
                },
                {
                    "role": "ASSISTANT",
                    "text": "The kitchen was renovated.",
                    "timestamp": entries[1]["timestamp"],
                },
                {
                    "role": "ASSISTANT",
                    "text": "Would you like a tour?",
                    "timestamp": entries[2]["timestamp"],
                },
            ],
        )
        self.assertTrue(all(entry["timestamp"] for entry in entries))

    async def test_interruption_keeps_final_spoken_text_and_clears_audio(self):
        entries, websocket = await self.receive_events([
            {
                "contentStart": {
                    "contentId": "assistant-audio",
                    "role": "ASSISTANT",
                    "type": "AUDIO",
                }
            },
            {"userSpeechStart": {}},
            {"contentStart": text_content_start("assistant-final", "ASSISTANT", "FINAL")},
            {"textOutput": {"contentId": "assistant-final", "content": "Let me explain."}},
            {"contentEnd": {"contentId": "assistant-final", "stopReason": "INTERRUPTED"}},
            {"contentStart": text_content_start("user-final", "USER", "FINAL")},
            {"textOutput": {"contentId": "user-final", "content": "Actually, one question."}},
            {"contentEnd": {"contentId": "user-final", "stopReason": "END_TURN"}},
            {"completionEnd": {}},
        ])

        self.assertEqual(
            entries,
            [
                {
                    "role": "ASSISTANT",
                    "text": "Let me explain.",
                    "timestamp": entries[0]["timestamp"],
                },
                {
                    "role": "USER",
                    "text": "Actually, one question.",
                    "timestamp": entries[1]["timestamp"],
                },
            ],
        )
        self.assertEqual(
            websocket.messages,
            [{"event": "clear", "streamSid": "stream-1"}],
        )

    async def test_unclosed_final_content_is_retained_and_duplicate_end_is_ignored(self):
        buffer = _CallTranscriptBuffer()
        content_start = text_content_start("user-1", "USER", "FINAL")
        buffer.start_content(content_start)
        buffer.add_text({"contentId": "user-1", "content": "Please call me"})
        buffer.end_content({"contentId": "user-1"})
        buffer.end_content({"contentId": "user-1"})
        buffer.start_content(content_start)
        buffer.add_text({"contentId": "user-1", "content": "Please call me"})

        self.assertEqual(
            buffer.entries(),
            [
                {
                    "role": "USER",
                    "text": "Please call me",
                    "timestamp": buffer.entries()[0]["timestamp"],
                }
            ],
        )

    async def test_final_text_received_without_content_end_is_preserved(self):
        entries, _ = await self.receive_events([
            {"contentStart": text_content_start("user-1", "USER", "FINAL")},
            {"textOutput": {"contentId": "user-1", "content": "I need "}},
            {"textOutput": {"contentId": "user-1", "content": "more details."}},
            {"completionEnd": {}},
        ])

        self.assertEqual(
            entries,
            [
                {
                    "role": "USER",
                    "text": "I need more details.",
                    "timestamp": entries[0]["timestamp"],
                }
            ],
        )

    async def test_missing_ids_and_malformed_events_are_ignored(self):
        entries, _ = await self.receive_events([
            None,
            b"not-json",
            {},
            {"contentStart": []},
            {"textOutput": None},
            {"contentEnd": None},
            {
                "contentStart": {
                    "role": "USER",
                    "type": "TEXT",
                    "additionalModelFields": '{"generationStage":"FINAL"}',
                }
            },
            {"textOutput": {"content": "Not attributable to a content ID"}},
            {"completionEnd": {}},
        ])

        self.assertEqual(entries, [])

    async def test_exact_repeated_text_in_distinct_turns_is_preserved(self):
        entries, _ = await self.receive_events([
            {"contentStart": text_content_start("user-1", "USER", "FINAL")},
            {"textOutput": {"contentId": "user-1", "content": "Yes, yes."}},
            {"contentEnd": {"contentId": "user-1", "stopReason": "END_TURN"}},
            {"contentStart": text_content_start("assistant-1", "ASSISTANT", "FINAL")},
            {"textOutput": {"contentId": "assistant-1", "content": "Yes, yes."}},
            {"contentEnd": {"contentId": "assistant-1", "stopReason": "END_TURN"}},
            {"completionEnd": {}},
        ])

        self.assertEqual(
            [(entry["role"], entry["text"]) for entry in entries],
            [("USER", "Yes, yes."), ("ASSISTANT", "Yes, yes.")],
        )

    async def test_speculative_text_is_not_duplicated_with_final_text(self):
        entries, _ = await self.receive_events([
            {"contentStart": text_content_start("assistant-1", "ASSISTANT", "SPECULATIVE")},
            {"textOutput": {"contentId": "assistant-1", "content": "The home is lovely."}},
            {"contentEnd": {"contentId": "assistant-1", "stopReason": "PARTIAL_TURN"}},
            {"contentStart": text_content_start("assistant-1", "ASSISTANT", "FINAL")},
            {"textOutput": {"contentId": "assistant-1", "content": "The home is lovely."}},
            {"contentEnd": {"contentId": "assistant-1", "stopReason": "END_TURN"}},
            {"completionEnd": {}},
        ])

        self.assertEqual(
            [(entry["role"], entry["text"]) for entry in entries],
            [("ASSISTANT", "The home is lovely.")],
        )

    async def test_stale_assistant_audio_is_not_forwarded_after_barge_in(self):
        entries, websocket = await self.receive_events([
            {
                "contentStart": {
                    "contentId": "assistant-audio",
                    "role": "ASSISTANT",
                    "type": "AUDIO",
                }
            },
            {"userSpeechStart": {}},
            {
                "audioOutput": {
                    "contentId": "assistant-audio",
                    "content": "AA==",
                }
            },
            {"completionEnd": {}},
        ])

        self.assertEqual(entries, [])
        self.assertEqual(websocket.messages, [{"event": "clear", "streamSid": "stream-1"}])


if __name__ == "__main__":
    unittest.main()
