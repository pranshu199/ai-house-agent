import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from app import main, supabase_service


PROPERTY_ID = "property-1"
REALTOR_ID = "realtor-1"
CAMPAIGN_ID = "campaign-1"
CALLER_NUMBER = "+14165550199"
CALLED_NUMBER = "+14165550101"


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, client, table_name):
        self.client = client
        self.table_name = table_name
        self.filters = {}
        self.in_filters = {}
        self.row_limit = None
        self.inserted_data = None

    def select(self, fields):
        return self

    def eq(self, field, value):
        self.filters[field] = value
        return self

    def in_(self, field, values):
        self.in_filters[field] = values
        return self

    def limit(self, value):
        self.row_limit = value
        return self

    def insert(self, data):
        self.inserted_data = data
        return self

    def execute(self):
        if self.inserted_data is not None:
            self.client.inserted_rows.append(self.inserted_data)
            return FakeResponse([{"id": "call-1"}])

        rows = self.client.tables.get(self.table_name, [])
        rows = [
            row
            for row in rows
            if all(row.get(field) == value for field, value in self.filters.items())
            and all(row.get(field) in values for field, values in self.in_filters.items())
        ]
        if self.row_limit is not None:
            rows = rows[: self.row_limit]
        return FakeResponse(rows)


class FakeSupabaseClient:
    def __init__(self, tables):
        self.tables = tables
        self.inserted_rows = []
        self.queries = []

    def table(self, table_name):
        query = FakeQuery(self, table_name)
        self.queries.append(query)
        return query


class FakeWebSocket:
    def __init__(self, messages):
        self.headers = {"X-Twilio-Signature": "test-signature"}
        self.url = "wss://example.test/ws/twilio"
        self.messages = [json.dumps(message) for message in messages]
        self.close_codes = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        return self.messages.pop(0)

    async def close(self, code):
        self.close_codes.append(code)

    async def send_json(self, message):
        raise AssertionError("Unexpected media output in attribution test")


def phone_record(
    status="active",
    campaign_id=CAMPAIGN_ID,
    property_id=PROPERTY_ID,
    realtor_id=REALTOR_ID,
):
    return {
        "id": "phone-1",
        "phone_number": "4165550101",
        "status": status,
        "realtor_id": realtor_id,
        "property_id": property_id,
        "campaign_id": campaign_id,
    }


def property_context(campaigns=None, realtors=None, campaign_omitted=False):
    return {
        "property": {
            "id": PROPERTY_ID,
            "realtor_id": REALTOR_ID,
            "address": "12 Main Street",
            "city": "Toronto",
            "approved_ai_information": "Detached home",
        },
        "realtors": realtors if realtors is not None else [
            {"id": REALTOR_ID, "name": "Alex Realtor", "brokerage": "Example Realty"}
        ],
        "campaigns": campaigns if campaigns is not None else [
            {
                "id": CAMPAIGN_ID,
                "name": "Spring campaign",
                "campaign_type": "listing",
                "realtor_id": REALTOR_ID,
            }
        ],
        "campaign_omitted": campaign_omitted,
    }


def signed_parameters(
    *,
    caller_number=CALLER_NUMBER,
    called_number=CALLED_NUMBER,
    call_sid="CA-call",
    timestamp=None,
):
    timestamp = str(int(main.time.time())) if timestamp is None else str(timestamp)
    with patch.dict("os.environ", {"TWILIO_AUTH_TOKEN": "unit-test-handoff-token"}):
        signature = main._sign_call_handoff(
            caller_number,
            called_number,
            call_sid,
            timestamp,
        )
    return {
        "From": caller_number,
        "To": called_number,
        "CallSid": call_sid,
        "Timestamp": timestamp,
        "HandoffSignature": signature,
    }


def start_message(custom_parameters=None, call_sid="CA-call"):
    return {
        "event": "start",
        "streamSid": "MZ-stream",
        "start": {
            "streamSid": "MZ-stream",
            "callSid": call_sid,
            "customParameters": (
                signed_parameters(call_sid=call_sid)
                if custom_parameters is None
                else custom_parameters
            ),
        },
    }


class PhoneNumberLookupTests(unittest.TestCase):
    def test_exact_e164_and_confirmed_nanp_candidates_map_to_one_row(self):
        client = FakeSupabaseClient({"phone_numbers": [phone_record()]})
        with patch.object(supabase_service, "get_supabase_client", return_value=client):
            matches = supabase_service.read_phone_number_matches(CALLED_NUMBER)

        self.assertEqual(matches, [phone_record()])
        self.assertEqual(
            client.queries[0].in_filters["phone_number"],
            [CALLED_NUMBER, "4165550101"],
        )

    def test_unknown_number_has_no_mapping(self):
        client = FakeSupabaseClient({"phone_numbers": []})
        with patch.object(supabase_service, "get_supabase_client", return_value=client):
            self.assertEqual(
                supabase_service.read_phone_number_matches(CALLED_NUMBER),
                [],
            )

    def test_rejects_non_e164_input(self):
        client = FakeSupabaseClient({"phone_numbers": []})
        with patch.object(supabase_service, "get_supabase_client", return_value=client):
            with self.assertRaises(ValueError):
                supabase_service.read_phone_number_matches("416-555-0101")

    def test_database_error_diagnostics_redact_phone_numbers(self):
        error = RuntimeError(f"failed to persist {CALLER_NUMBER}")
        diagnostic = main._safe_supabase_error(error, (CALLER_NUMBER,))

        self.assertNotIn(CALLER_NUMBER, diagnostic)
        self.assertIn("[REDACTED_PHONE]", diagnostic)


class PropertyContextTests(unittest.TestCase):
    def test_campaign_is_loaded_only_when_owned_by_assigned_property_and_realtor(self):
        tables = {
            "properties": [{"id": PROPERTY_ID, "realtor_id": REALTOR_ID}],
            "realtors": [{"id": REALTOR_ID, "name": "Alex Realtor"}],
            "campaigns": [
                {
                    "id": CAMPAIGN_ID,
                    "name": "Spring campaign",
                    "campaign_type": "listing",
                    "status": "active",
                    "property_id": PROPERTY_ID,
                    "realtor_id": REALTOR_ID,
                }
            ],
        }
        client = FakeSupabaseClient(tables)
        with patch.object(supabase_service, "get_supabase_client", return_value=client):
            context = supabase_service.read_property_context(
                PROPERTY_ID,
                REALTOR_ID,
                CAMPAIGN_ID,
            )

        self.assertEqual(context["campaigns"][0]["id"], CAMPAIGN_ID)
        self.assertEqual(context["realtors"][0]["id"], REALTOR_ID)
        self.assertFalse(context["campaign_omitted"])

    def test_campaign_mismatch_is_omitted_and_logged(self):
        tables = {
            "properties": [{"id": PROPERTY_ID, "realtor_id": REALTOR_ID}],
            "realtors": [{"id": REALTOR_ID, "name": "Alex Realtor"}],
            "campaigns": [
                {
                    "id": CAMPAIGN_ID,
                    "property_id": "different-property",
                    "realtor_id": REALTOR_ID,
                }
            ],
        }
        client = FakeSupabaseClient(tables)
        with (
            patch.object(supabase_service, "get_supabase_client", return_value=client),
            self.assertLogs(supabase_service.logger, level="WARNING"),
        ):
            context = supabase_service.read_property_context(
                PROPERTY_ID,
                REALTOR_ID,
                CAMPAIGN_ID,
            )

        self.assertEqual(context["campaigns"], [])
        self.assertTrue(context["campaign_omitted"])

    def test_non_active_campaign_is_omitted(self):
        tables = {
            "properties": [{"id": PROPERTY_ID, "realtor_id": REALTOR_ID}],
            "realtors": [{"id": REALTOR_ID, "name": "Alex Realtor"}],
            "campaigns": [
                {
                    "id": CAMPAIGN_ID,
                    "property_id": PROPERTY_ID,
                    "realtor_id": REALTOR_ID,
                    "status": "paused",
                }
            ],
        }
        client = FakeSupabaseClient(tables)
        with (
            patch.object(supabase_service, "get_supabase_client", return_value=client),
            self.assertLogs(supabase_service.logger, level="INFO"),
        ):
            context = supabase_service.read_property_context(
                PROPERTY_ID,
                REALTOR_ID,
                CAMPAIGN_ID,
            )

        self.assertEqual(context["campaigns"], [])
        self.assertTrue(context["campaign_omitted"])

    def test_missing_property_or_realtor_fails_context_validation(self):
        missing_property = FakeSupabaseClient({"properties": []})
        with patch.object(
            supabase_service,
            "get_supabase_client",
            return_value=missing_property,
        ):
            self.assertIsNone(
                supabase_service.read_property_context(PROPERTY_ID, REALTOR_ID, None)
            )

        missing_realtor = FakeSupabaseClient(
            {
                "properties": [{"id": PROPERTY_ID, "realtor_id": REALTOR_ID}],
                "realtors": [],
            }
        )
        with patch.object(
            supabase_service,
            "get_supabase_client",
            return_value=missing_realtor,
        ):
            self.assertIsNone(
                supabase_service.read_property_context(PROPERTY_ID, REALTOR_ID, None)
            )

    def test_call_record_insert_includes_caller_phone(self):
        client = FakeSupabaseClient({})
        call_data = {
            "realtor_id": REALTOR_ID,
            "property_id": PROPERTY_ID,
            "caller_phone": CALLER_NUMBER,
        }
        with patch.object(supabase_service, "get_supabase_client", return_value=client):
            supabase_service.create_call_record(call_data)

        self.assertEqual(client.inserted_rows, [call_data])


class TwilioWebhookTests(unittest.TestCase):
    def test_invalid_signature_is_rejected(self):
        client = TestClient(main.app, base_url="https://testserver")
        with patch.dict("os.environ", {"TWILIO_AUTH_TOKEN": "unit-test-auth-token"}):
            response = client.post(
                "/twilio/voice",
                data={
                    "From": CALLER_NUMBER,
                    "To": CALLED_NUMBER,
                    "CallSid": "CA-call",
                },
                headers={"X-Twilio-Signature": "invalid"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Invalid Twilio signature")

    def test_validated_numbers_are_handed_to_stream_as_custom_parameters(self):
        client = TestClient(main.app, base_url="https://testserver")
        with (
            patch.object(main, "_validate_twilio_signature", return_value=True),
            patch.dict(
                "os.environ",
                {
                    "PUBLIC_BASE_URL": "https://voice.example.test",
                    "TWILIO_AUTH_TOKEN": "unit-test-handoff-token",
                },
            ),
        ):
            response = client.post(
                "/twilio/voice",
                data={
                    "From": CALLER_NUMBER,
                    "To": CALLED_NUMBER,
                    "CallSid": "CA-call",
                },
                headers={"X-Twilio-Signature": "valid"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(f'name="From" value="{CALLER_NUMBER}"', response.text)
        self.assertIn(f'name="To" value="{CALLED_NUMBER}"', response.text)
        self.assertIn('name="CallSid" value="CA-call"', response.text)
        self.assertIn('name="HandoffSignature" value="', response.text)
        self.assertIn('name="Timestamp" value="', response.text)


class WebSocketAttributionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with main._HANDOFF_REPLAY_LOCK:
            main._HANDOFF_REPLAY_CACHE.clear()

    async def run_stream(
        self,
        matches,
        context,
        *,
        context_error=None,
        insert_error=None,
        custom_parameters=None,
        start_call_sid="CA-call",
        websocket_signature_valid=True,
    ):
        websocket = FakeWebSocket(
            [
                start_message(
                    custom_parameters=custom_parameters,
                    call_sid=start_call_sid,
                ),
                {"event": "stop"},
            ]
        )
        client = object()
        stream = object()

        async def forward_audio(
            nova_stream,
            connected_websocket,
            stream_sid,
            session_stopping,
            assistant_text_chunks,
        ):
            await session_stopping.wait()

        nova_stream_mock = AsyncMock(return_value=(client, stream, "prompt-1"))
        start_session_mock = AsyncMock()
        system_prompt_mock = AsyncMock()
        audio_content_mock = AsyncMock(return_value="audio-1")
        create_call_mock = Mock(
            side_effect=insert_error if insert_error else lambda call_data: {"id": "call-1"}
        )

        property_reader = Mock(
            side_effect=context_error if context_error else lambda *_args: context
        )

        with (
            patch.object(
                main,
                "_validate_twilio_signature",
                return_value=websocket_signature_valid,
            ),
            patch.dict("os.environ", {"TWILIO_AUTH_TOKEN": "unit-test-handoff-token"}),
            patch.object(main, "read_phone_number_matches", return_value=matches),
            patch.object(main, "read_property_context", property_reader),
            patch.object(main, "create_call_record", create_call_mock),
            patch.object(main, "finalize_call_record"),
            patch.object(main, "create_nova_stream", nova_stream_mock),
            patch.object(main, "start_session", start_session_mock),
            patch.object(main, "send_system_prompt", system_prompt_mock),
            patch.object(main, "send_audio_content_start", audio_content_mock),
            patch.object(main, "_forward_nova_audio_to_twilio", forward_audio),
            patch.object(main, "end_audio_content", new_callable=AsyncMock),
            patch.object(main, "close_nova", new_callable=AsyncMock),
        ):
            await main.twilio_media_stream(websocket)

        return websocket, create_call_mock, nova_stream_mock, system_prompt_mock, property_reader

    async def test_valid_mapping_persists_ids_and_caller_phone_then_starts_nova(self):
        context = property_context()
        websocket, create_call, nova_stream, system_prompt, property_reader = (
            await self.run_stream([phone_record()], context)
        )

        call_data = create_call.call_args.args[0]
        self.assertEqual(call_data["property_id"], PROPERTY_ID)
        self.assertEqual(call_data["realtor_id"], REALTOR_ID)
        self.assertEqual(call_data["campaign_id"], CAMPAIGN_ID)
        self.assertEqual(call_data["caller_phone"], CALLER_NUMBER)
        property_reader.assert_called_once_with(PROPERTY_ID, REALTOR_ID, CAMPAIGN_ID)
        nova_stream.assert_awaited_once()
        self.assertIn("Spring campaign", system_prompt.await_args.args[2])
        self.assertIn("12 Main Street", system_prompt.await_args.args[2])
        self.assertTrue(websocket.accepted)

    async def test_unknown_number_is_rejected_before_nova(self):
        websocket, create_call, nova_stream, _, property_reader = (
            await self.run_stream([], None)
        )

        self.assertEqual(websocket.close_codes, [1008])
        create_call.assert_not_called()
        property_reader.assert_not_called()
        nova_stream.assert_not_awaited()

    async def test_duplicate_phone_number_mappings_are_rejected(self):
        websocket, create_call, nova_stream, _, property_reader = (
            await self.run_stream([phone_record(), phone_record()], None)
        )

        self.assertEqual(websocket.close_codes, [1008])
        property_reader.assert_not_called()
        create_call.assert_not_called()
        nova_stream.assert_not_awaited()

    async def test_missing_custom_parameters_are_rejected(self):
        websocket, create_call, nova_stream, _, property_reader = (
            await self.run_stream([], None, custom_parameters={})
        )

        self.assertEqual(websocket.close_codes, [1008])
        property_reader.assert_not_called()
        create_call.assert_not_called()
        nova_stream.assert_not_awaited()

    async def test_malformed_custom_parameters_are_rejected(self):
        parameters = signed_parameters(caller_number="416-555-0199")
        websocket, create_call, nova_stream, _, property_reader = (
            await self.run_stream(
                [phone_record()],
                property_context(),
                custom_parameters=parameters,
            )
        )

        self.assertEqual(websocket.close_codes, [1008])
        property_reader.assert_not_called()
        create_call.assert_not_called()
        nova_stream.assert_not_awaited()

    async def test_invalid_websocket_twilio_signature_is_rejected(self):
        websocket, create_call, nova_stream, _, property_reader = await self.run_stream(
            [phone_record()],
            property_context(),
            websocket_signature_valid=False,
        )

        self.assertEqual(websocket.close_codes, [1008])
        self.assertFalse(websocket.accepted)
        property_reader.assert_not_called()
        create_call.assert_not_called()
        nova_stream.assert_not_awaited()

    async def test_invalid_handoff_signature_is_rejected(self):
        parameters = signed_parameters()
        parameters["HandoffSignature"] = "0" * 64
        websocket, create_call, nova_stream, _, property_reader = (
            await self.run_stream(
                [phone_record()],
                property_context(),
                custom_parameters=parameters,
            )
        )

        self.assertEqual(websocket.close_codes, [1008])
        property_reader.assert_not_called()
        create_call.assert_not_called()
        nova_stream.assert_not_awaited()

    async def test_expired_handoff_signature_is_rejected(self):
        parameters = signed_parameters(
            timestamp=int(main.time.time()) - main._HANDOFF_MAX_AGE_SECONDS - 1
        )
        websocket, create_call, nova_stream, _, property_reader = (
            await self.run_stream(
                [phone_record()],
                property_context(),
                custom_parameters=parameters,
            )
        )

        self.assertEqual(websocket.close_codes, [1008])
        property_reader.assert_not_called()
        create_call.assert_not_called()
        nova_stream.assert_not_awaited()

    async def test_call_sid_mismatch_is_rejected(self):
        parameters = signed_parameters(call_sid="CA-signed")
        websocket, create_call, nova_stream, _, property_reader = (
            await self.run_stream(
                [phone_record()],
                property_context(),
                custom_parameters=parameters,
                start_call_sid="CA-different",
            )
        )

        self.assertEqual(websocket.close_codes, [1008])
        property_reader.assert_not_called()
        create_call.assert_not_called()
        nova_stream.assert_not_awaited()

    async def test_handoff_replay_is_rejected_in_same_process(self):
        first = await self.run_stream([phone_record()], property_context())
        second = await self.run_stream([phone_record()], property_context())

        self.assertEqual(first[0].close_codes, [1000])
        self.assertEqual(first[2].await_count, 1)
        self.assertEqual(second[0].close_codes, [1008])
        second[1].assert_not_called()
        second[2].assert_not_awaited()

    async def test_inactive_number_is_rejected_before_nova(self):
        websocket, create_call, nova_stream, _, property_reader = (
            await self.run_stream([phone_record(status="inactive")], None)
        )

        self.assertEqual(websocket.close_codes, [1008])
        create_call.assert_not_called()
        property_reader.assert_not_called()
        nova_stream.assert_not_awaited()

    async def test_missing_property_is_rejected_before_nova(self):
        websocket, create_call, nova_stream, _, _ = await self.run_stream(
            [phone_record()],
            None,
        )

        self.assertEqual(websocket.close_codes, [1008])
        create_call.assert_not_called()
        nova_stream.assert_not_awaited()

    async def test_missing_property_assignment_is_rejected_before_nova(self):
        websocket, create_call, nova_stream, _, property_reader = (
            await self.run_stream(
                [phone_record(property_id=None)],
                None,
            )
        )

        self.assertEqual(websocket.close_codes, [1008])
        property_reader.assert_not_called()
        create_call.assert_not_called()
        nova_stream.assert_not_awaited()

    async def test_missing_realtor_is_rejected_before_nova(self):
        context = property_context(realtors=[])
        websocket, create_call, nova_stream, _, _ = await self.run_stream(
            [phone_record()],
            context,
        )

        self.assertEqual(websocket.close_codes, [1008])
        create_call.assert_not_called()
        nova_stream.assert_not_awaited()

    async def test_invalid_campaign_is_omitted_but_call_continues(self):
        context = property_context(campaigns=[], campaign_omitted=True)
        _, create_call, nova_stream, _, _ = await self.run_stream(
            [phone_record()],
            context,
        )

        self.assertNotIn("campaign_id", create_call.call_args.args[0])
        nova_stream.assert_awaited_once()

    async def test_database_failure_does_not_start_nova(self):
        websocket, create_call, nova_stream, _, _ = await self.run_stream(
            [phone_record()],
            None,
            context_error=RuntimeError("database unavailable"),
        )

        self.assertEqual(websocket.close_codes, [1011])
        create_call.assert_not_called()
        nova_stream.assert_not_awaited()

    async def test_call_insert_failure_does_not_start_nova(self):
        websocket, _, nova_stream, _, _ = await self.run_stream(
            [phone_record()],
            property_context(),
            insert_error=RuntimeError("insert failed"),
        )

        self.assertEqual(websocket.close_codes, [1011])
        nova_stream.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
