"""Persistent, retryable realtor notifications for completed call events."""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from app.integrations import email_client, supabase_client
from app.integrations.twilio_client import get_twilio_client


logger = logging.getLogger(__name__)

_EVENT_CHANNEL_PREFERENCE = {
    ("incoming_call_started", "sms"): ("sms_enabled", "sms_incoming_calls"),
    ("new_lead_captured", "sms"): ("sms_enabled", "sms_new_leads"),
    ("new_lead_captured", "email"): ("email_enabled", "email_new_leads"),
    ("viewing_requested", "sms"): ("sms_enabled", "sms_viewing_requests"),
    ("viewing_requested", "email"): ("email_enabled", "email_viewing_requests"),
}
_E164_PATTERN = re.compile(r"\+[1-9][0-9]{7,14}\Z")
_MESSAGE_SERVICE_SID_PATTERN = re.compile(r"MG[0-9A-Fa-f]{32}\Z")
_MAX_ATTEMPTS = 5
_BASE_RETRY_SECONDS = 30
_MAX_RETRY_SECONDS = 3600


class NotificationConfigurationError(RuntimeError):
    """Raised when a destination or provider is not configured."""


def _preferences_allow(realtor_id: str, event_type: str, channel: str) -> bool:
    fields = _EVENT_CHANNEL_PREFERENCE.get((event_type, channel))
    if fields is None:
        raise ValueError("Unsupported notification event and channel")
    preferences = supabase_client.read_notification_preferences(realtor_id)
    return all(preferences.get(field, True) is True for field in fields)


def _enqueue(
    *,
    realtor_id: str,
    property_id: str | None,
    call_id: str | None,
    lead_id: str | None,
    event_type: str,
    channel: str,
) -> bool:
    if not _preferences_allow(realtor_id, event_type, channel):
        return False
    identity = call_id or lead_id
    if not identity:
        raise ValueError("Notification events require a call or lead ID")
    return supabase_client.create_notification_delivery(
        {
            "realtor_id": realtor_id,
            "property_id": property_id,
            "call_id": call_id,
            "lead_id": lead_id,
            "event_type": event_type,
            "channel": channel,
            "idempotency_key": f"{event_type}:{channel}:{identity}",
        }
    )


def enqueue_incoming_call_started(
    call_id: str,
    realtor_id: str,
    property_id: str,
) -> int:
    """Persist enabled SMS notifications for a newly-created incoming call."""

    return int(
        _enqueue(
            realtor_id=realtor_id,
            property_id=property_id,
            call_id=call_id,
            lead_id=None,
            event_type="incoming_call_started",
            channel="sms",
        )
    )


def enqueue_lead_notifications(
    call_id: str,
    lead_id: str,
    realtor_id: str,
    property_id: str | None,
    intent: str,
) -> int:
    """Persist enabled new-lead and viewing-request notifications."""

    events = [("new_lead_captured", ("sms", "email"))]
    if intent == "viewing_request":
        events.append(("viewing_requested", ("sms", "email")))

    created = 0
    for event_type, channels in events:
        for channel in channels:
            created += int(
                _enqueue(
                    realtor_id=realtor_id,
                    property_id=property_id,
                    call_id=call_id,
                    lead_id=lead_id,
                    event_type=event_type,
                    channel=channel,
                )
            )
    return created


def _notification_text(delivery: dict, context: dict) -> tuple[str, str]:
    property_record = context.get("property") or {}
    lead = context.get("lead") or {}
    location = ", ".join(
        part
        for part in (property_record.get("address"), property_record.get("city"))
        if isinstance(part, str) and part.strip()
    ) or "the assigned property"

    event_type = delivery.get("event_type")
    if event_type == "incoming_call_started":
        subject = "Incoming call started"
        body = f"A new incoming call has started for {location}."
    else:
        is_viewing = event_type == "viewing_requested"
        subject = "Viewing requested" if is_viewing else "New lead captured"
        details = [
            f"Name: {lead.get('name') or 'Not disclosed'}",
            f"Intent: {lead.get('intent') or 'unknown'}",
            f"Property: {location}",
        ]
        if lead.get("phone"):
            details.append(f"Caller phone: {lead['phone']}")
        if lead.get("timeline"):
            details.append(f"Timeline: {lead['timeline']}")
        if lead.get("budget_min") is not None or lead.get("budget_max") is not None:
            details.append(
                f"Budget: {lead.get('budget_min') or 'unspecified'}"
                f" - {lead.get('budget_max') or 'unspecified'}"
            )
        summary = lead.get("summary")
        if isinstance(summary, str) and summary.strip():
            details.append(f"Summary: {summary.strip()[:400]}")
        body = "\n".join(details)

    if delivery.get("channel") == "sms":
        body = body[:1400]
    return subject, body


def _send_sms(recipient: str, body: str) -> str:
    if not _E164_PATTERN.fullmatch(recipient):
        raise NotificationConfigurationError("Realtor SMS destination is invalid")

    messaging_service_sid = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()
    from_number = os.getenv("TWILIO_FROM_NUMBER", "").strip()
    if messaging_service_sid:
        if not _MESSAGE_SERVICE_SID_PATTERN.fullmatch(messaging_service_sid):
            raise NotificationConfigurationError(
                "Twilio messaging service configuration is invalid"
            )
        message = get_twilio_client().messages.create(
            to=recipient,
            body=body,
            messaging_service_sid=messaging_service_sid,
        )
    else:
        if not _E164_PATTERN.fullmatch(from_number):
            raise NotificationConfigurationError(
                "Twilio sender number or messaging service is required"
            )
        message = get_twilio_client().messages.create(
            to=recipient,
            body=body,
            from_=from_number,
        )
    sid = getattr(message, "sid", None)
    if not isinstance(sid, str) or not sid:
        raise RuntimeError("Twilio did not return a message ID")
    return sid


def _send_email(recipient: str, subject: str, body: str) -> str:
    return email_client.send_notification_email(recipient, subject, body)


def _safe_provider_error(exc: Exception) -> str:
    details = [type(exc).__name__]
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    if isinstance(code, int) and not isinstance(code, bool):
        details.append(f"code={code}")
    elif isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", code):
        details.append(f"code={code}")
    if isinstance(status, int) and not isinstance(status, bool):
        details.append(f"status={status}")
    return " ".join(details)[:500]


def _retry_delay(retry_count: int) -> int:
    return min(
        _BASE_RETRY_SECONDS * (2 ** max(0, retry_count - 1)),
        _MAX_RETRY_SECONDS,
    )


def _process_delivery(delivery: dict) -> None:
    delivery_id = delivery.get("id")
    try:
        context = supabase_client.read_notification_delivery_context(delivery)
        realtor = context.get("realtor") or {}
        channel = delivery.get("channel")
        recipient = realtor.get("phone") if channel == "sms" else realtor.get("email")
        if not isinstance(recipient, str) or not recipient.strip():
            raise NotificationConfigurationError(
                "Realtor notification destination is unavailable"
            )

        subject, body = _notification_text(delivery, context)
        if channel == "sms":
            provider_message_id = _send_sms(recipient.strip(), body)
        elif channel == "email":
            provider_message_id = _send_email(recipient.strip(), subject, body)
        else:
            raise NotificationConfigurationError("Notification channel is invalid")

        updated = supabase_client.update_notification_delivery(
            delivery_id,
            {
                "delivery_status": "sent",
                "provider_message_id": provider_message_id,
                "error_message": None,
                "sent_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        if not updated:
            raise RuntimeError("Notification sent state was not persisted")
    except NotificationConfigurationError as exc:
        supabase_client.update_notification_delivery(
            delivery_id,
            {
                "delivery_status": "failed",
                "error_message": _safe_provider_error(exc),
            },
        )
        logger.warning(
            "NOTIFICATION_DELIVERY_FAILED delivery_id=%s error=%s",
            delivery_id,
            _safe_provider_error(exc),
        )
    except Exception as exc:
        retry_count = delivery.get("retry_count", 0)
        retry_count = retry_count if isinstance(retry_count, int) else 0
        next_retry_count = retry_count + 1
        if next_retry_count >= _MAX_ATTEMPTS:
            update = {
                "delivery_status": "failed",
                "retry_count": next_retry_count,
                "error_message": _safe_provider_error(exc),
            }
        else:
            update = {
                "delivery_status": "pending",
                "retry_count": next_retry_count,
                "error_message": _safe_provider_error(exc),
                "next_attempt_at": (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=_retry_delay(next_retry_count))
                ).isoformat(),
            }
        supabase_client.update_notification_delivery(delivery_id, update)
        logger.warning(
            "NOTIFICATION_DELIVERY_RETRY delivery_id=%s channel=%s "
            "event_type=%s error=%s",
            delivery_id,
            delivery.get("channel"),
            delivery.get("event_type"),
            _safe_provider_error(exc),
        )


def process_due_notifications(limit: int = 20) -> int:
    """Claim and process a bounded batch of due persistent deliveries."""

    deliveries = supabase_client.read_due_notification_deliveries(limit)
    processed = 0
    for delivery in deliveries:
        try:
            claimed = supabase_client.claim_notification_delivery(delivery)
            if claimed is None:
                continue
            _process_delivery(claimed)
            processed += 1
        except Exception as exc:
            logger.warning(
                "NOTIFICATION_DELIVERY_PROCESS_FAILURE delivery_id=%s error_type=%s",
                delivery.get("id"),
                type(exc).__name__,
            )
    return processed


async def run_notification_worker(
    stop_event: asyncio.Event,
    poll_interval_seconds: float = 10,
) -> None:
    """Poll persistent deliveries; stop cleanly when the FastAPI app shuts down."""

    while True:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await asyncio.to_thread(process_due_notifications)
        except Exception as exc:
            logger.warning(
                "NOTIFICATION_WORKER_FAILURE error_type=%s",
                type(exc).__name__,
            )
