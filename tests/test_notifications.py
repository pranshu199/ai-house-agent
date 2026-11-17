import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.integrations import email_client
from app.services import lead_capture, notifications


CALL_ID = "00000000-0000-4000-8000-000000000001"
REALTOR_ID = "00000000-0000-4000-8000-000000000002"
PROPERTY_ID = "00000000-0000-4000-8000-000000000003"
LEAD_ID = "00000000-0000-4000-8000-000000000004"
DELIVERY_ID = "00000000-0000-4000-8000-000000000005"


def delivery(**overrides):
    row = {
        "id": DELIVERY_ID,
        "realtor_id": REALTOR_ID,
        "property_id": PROPERTY_ID,
        "call_id": CALL_ID,
        "lead_id": LEAD_ID,
        "event_type": "new_lead_captured",
        "channel": "sms",
        "delivery_status": "sending",
        "retry_count": 0,
    }
    row.update(overrides)
    return row


def context(**overrides):
    result = {
        "realtor": {
            "name": "Agent",
            "email": "agent@example.test",
            "phone": "+14165550100",
        },
        "property": {"address": "12 Main Street", "city": "Toronto"},
        "call": {"caller_phone": "+14165550199"},
        "lead": {
            "name": "Sam",
            "phone": "+14165550199",
            "summary": "Asked to view the home.",
            "intent": "viewing_request",
            "timeline": None,
            "budget_min": None,
            "budget_max": None,
        },
    }
    result.update(overrides)
    return result


class NotificationEnqueueTests(unittest.TestCase):
    def test_incoming_call_uses_idempotent_sms_delivery(self):
        with (
            patch.object(
                notifications.supabase_client,
                "read_notification_preferences",
                return_value={
                    "sms_enabled": True,
                    "sms_incoming_calls": True,
                },
            ),
            patch.object(
                notifications.supabase_client,
                "create_notification_delivery",
                return_value=True,
            ) as create,
        ):
            created = notifications.enqueue_incoming_call_started(
                CALL_ID,
                REALTOR_ID,
                PROPERTY_ID,
            )

        self.assertEqual(created, 1)
        row = create.call_args.args[0]
        self.assertEqual(row["event_type"], "incoming_call_started")
        self.assertEqual(row["channel"], "sms")
        self.assertEqual(
            row["idempotency_key"],
            f"incoming_call_started:sms:{CALL_ID}",
        )

    def test_preferences_are_independent_for_sms_and_email(self):
        prefs = {
            "sms_enabled": True,
            "email_enabled": True,
            "sms_new_leads": False,
            "email_new_leads": True,
            "sms_viewing_requests": True,
            "email_viewing_requests": False,
        }
        with (
            patch.object(
                notifications.supabase_client,
                "read_notification_preferences",
                return_value=prefs,
            ),
            patch.object(
                notifications.supabase_client,
                "create_notification_delivery",
                return_value=True,
            ) as create,
        ):
            count = notifications.enqueue_lead_notifications(
                CALL_ID,
                LEAD_ID,
                REALTOR_ID,
                PROPERTY_ID,
                "viewing_request",
            )

        self.assertEqual(count, 2)
        keys = {
            call.args[0]["idempotency_key"]
            for call in create.call_args_list
        }
        self.assertEqual(
            keys,
            {
                f"new_lead_captured:email:{CALL_ID}",
                f"viewing_requested:sms:{CALL_ID}",
            },
        )

    def test_disabled_channel_skips_delivery_creation(self):
        with (
            patch.object(
                notifications.supabase_client,
                "read_notification_preferences",
                return_value={
                    "sms_enabled": False,
                    "sms_incoming_calls": True,
                },
            ),
            patch.object(
                notifications.supabase_client,
                "create_notification_delivery",
            ) as create,
        ):
            count = notifications.enqueue_incoming_call_started(
                CALL_ID,
                REALTOR_ID,
                PROPERTY_ID,
            )

        self.assertEqual(count, 0)
        create.assert_not_called()

    def test_idempotent_duplicate_is_reported_as_not_created(self):
        with (
            patch.object(
                notifications.supabase_client,
                "read_notification_preferences",
                return_value={
                    "sms_enabled": True,
                    "sms_incoming_calls": True,
                },
            ),
            patch.object(
                notifications.supabase_client,
                "create_notification_delivery",
                return_value=False,
            ),
        ):
            self.assertEqual(
                notifications.enqueue_incoming_call_started(
                    CALL_ID,
                    REALTOR_ID,
                    PROPERTY_ID,
                ),
                0,
            )


class NotificationDeliveryTests(unittest.TestCase):
    def test_sms_uses_mocked_twilio_and_sender_number(self):
        twilio = Mock()
        twilio.messages.create.return_value.sid = "SM123"
        with (
            patch.dict(os.environ, {"TWILIO_FROM_NUMBER": "+14165550101"}),
            patch.object(notifications, "get_twilio_client", return_value=twilio),
        ):
            message_id = notifications._send_sms(
                "+14165550100",
                "Incoming call started.",
            )

        self.assertEqual(message_id, "SM123")
        twilio.messages.create.assert_called_once_with(
            to="+14165550100",
            body="Incoming call started.",
            from_="+14165550101",
        )

    def test_sms_uses_mocked_twilio_messaging_service(self):
        twilio = Mock()
        twilio.messages.create.return_value.sid = "SM456"
        with (
            patch.dict(
                os.environ,
                {"TWILIO_MESSAGING_SERVICE_SID": "MG" + "a" * 32},
                clear=True,
            ),
            patch.object(notifications, "get_twilio_client", return_value=twilio),
        ):
            self.assertEqual(
                notifications._send_sms("+14165550100", "New lead."),
                "SM456",
            )
        self.assertEqual(
            twilio.messages.create.call_args.kwargs["messaging_service_sid"],
            "MG" + "a" * 32,
        )

    def test_smtp_email_is_plain_text_and_uses_configured_transport(self):
        with (
            patch.dict(
                os.environ,
                {
                    "SMTP_HOST": "smtp.example.test",
                    "SMTP_PORT": "587",
                    "SMTP_FROM_EMAIL": "notifications@example.test",
                    "SMTP_USE_TLS": "true",
                },
                clear=True,
            ),
            patch.object(email_client.smtplib, "SMTP") as smtp_factory,
        ):
            smtp = smtp_factory.return_value.__enter__.return_value
            message_id = email_client.send_notification_email(
                "agent@example.test",
                "New lead captured",
                "Name: Sam",
            )

        self.assertTrue(message_id.startswith("<"))
        smtp.starttls.assert_called_once()
        email_message = smtp.send_message.call_args.args[0]
        self.assertEqual(email_message["To"], "agent@example.test")
        self.assertEqual(email_message.get_content_type(), "text/plain")
        self.assertIn("Name: Sam", email_message.get_content())

    def test_delivery_success_persists_provider_message_id(self):
        updates = []
        with (
            patch.object(
                notifications.supabase_client,
                "read_notification_delivery_context",
                return_value=context(),
            ),
            patch.object(
                notifications,
                "_send_sms",
                return_value="SM123",
            ),
            patch.object(
                notifications.supabase_client,
                "update_notification_delivery",
                side_effect=lambda _delivery_id, values: updates.append(values) or True,
            ),
        ):
            notifications._process_delivery(delivery())

        self.assertEqual(updates[0]["delivery_status"], "sent")
        self.assertEqual(updates[0]["provider_message_id"], "SM123")
        self.assertIsNotNone(updates[0]["sent_at"])

    def test_transient_failure_is_scheduled_and_error_is_sanitized(self):
        updates = []
        with (
            patch.object(
                notifications.supabase_client,
                "read_notification_delivery_context",
                return_value=context(),
            ),
            patch.object(
                notifications,
                "_send_sms",
                side_effect=RuntimeError("sensitive provider payload"),
            ),
            patch.object(
                notifications.supabase_client,
                "update_notification_delivery",
                side_effect=lambda _delivery_id, values: updates.append(values) or True,
            ),
        ):
            notifications._process_delivery(delivery())

        self.assertEqual(updates[0]["delivery_status"], "pending")
        self.assertEqual(updates[0]["retry_count"], 1)
        self.assertTrue(updates[0]["next_attempt_at"])
        self.assertEqual(updates[0]["error_message"], "RuntimeError")
        self.assertNotIn("sensitive", updates[0]["error_message"])

    def test_permanent_configuration_failure_does_not_retry(self):
        updates = []
        with (
            patch.object(
                notifications.supabase_client,
                "read_notification_delivery_context",
                return_value=context(
                    realtor={"name": "Agent", "email": None, "phone": None}
                ),
            ),
            patch.object(
                notifications.supabase_client,
                "update_notification_delivery",
                side_effect=lambda _delivery_id, values: updates.append(values) or True,
            ),
        ):
            notifications._process_delivery(delivery())

        self.assertEqual(updates[0]["delivery_status"], "failed")
        self.assertEqual(updates[0]["error_message"], "NotificationConfigurationError")
        self.assertNotIn("recipient", updates[0]["error_message"])

    def test_one_delivery_failure_does_not_prevent_later_delivery(self):
        rows = [delivery(id=DELIVERY_ID), delivery(id=CALL_ID)]
        claims = [row for row in rows]
        with (
            patch.object(
                notifications.supabase_client,
                "read_due_notification_deliveries",
                return_value=rows,
            ),
            patch.object(
                notifications.supabase_client,
                "claim_notification_delivery",
                side_effect=claims,
            ),
            patch.object(
                notifications,
                "_process_delivery",
                side_effect=[RuntimeError("failed"), None],
            ) as process,
        ):
            self.assertEqual(notifications.process_due_notifications(), 1)
        self.assertEqual(process.call_count, 2)


class LeadCaptureNotificationIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_notification_enqueue_failure_does_not_fail_lead_capture(self):
        extraction = {
            "name": None,
            "email": None,
            "lead_type": "unknown",
            "intent": "unknown",
            "budget_min": None,
            "budget_max": None,
            "timeline": None,
            "interested_property": None,
            "summary": "Caller asked about the property.",
            "evidence": [],
        }
        with (
            patch.object(
                lead_capture,
                "extract_call_lead",
                new=AsyncMock(return_value=extraction),
            ),
            patch.object(
                lead_capture,
                "persist_call_lead",
                return_value=LEAD_ID,
            ),
            patch.object(
                lead_capture.supabase_client,
                "read_call_notification_attribution",
                side_effect=RuntimeError("notification database unavailable"),
            ),
            patch.object(lead_capture, "enqueue_lead_notifications") as enqueue,
        ):
            result = await lead_capture.capture_call_lead(
                CALL_ID,
                [{"role": "USER", "text": "Tell me about the property."}],
            )

        self.assertEqual(result, LEAD_ID)
        enqueue.assert_not_called()
