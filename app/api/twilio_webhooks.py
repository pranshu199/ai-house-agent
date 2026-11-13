import asyncio
import base64
from collections.abc import Mapping
import hashlib
import hmac
import json
import logging
import os
import re
import struct
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape
from fastapi import (
    APIRouter,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from starlette.datastructures import FormData
from twilio.request_validator import RequestValidator

from app.integrations.nova_client import (
    create_nova_stream,
    start_session,
    send_system_prompt,
    send_audio_content_start,
    send_audio_chunk,
    end_audio_content,
    close_nova,
    SAMPLE_RATE,
    CHANNELS,
    SAMPLE_WIDTH,
)
from app.integrations.supabase_client import (
    create_call_record,
    finalize_call_record,
    read_property_context,
    read_phone_number_matches,
)


router = APIRouter()
logger = logging.getLogger(__name__)

_E164_PHONE_NUMBER = re.compile(r"\+[1-9][0-9]{0,14}\Z")
_ANONYMOUS_CALLER_ID = "anonymous"
_HANDOFF_MAX_AGE_SECONDS = 300
_HANDOFF_FUTURE_SKEW_SECONDS = 30
_HANDOFF_REPLAY_CACHE: dict[str, int] = {}
_HANDOFF_REPLAY_LOCK = threading.Lock()


SYSTEM_PROMPT = (
    "Introduce yourself as You are John, a friendly real estate voice assistant. "
    "Answer naturally and briefly. "
    "You are speaking with a potential home buyer."
)


def _validate_twilio_signature(
    url: str,
    params: Mapping[str, str] | FormData,
    signature: str | None,
) -> bool:
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if not auth_token or not signature:
        return False

    try:
        return RequestValidator(auth_token).validate(url, params, signature)
    except Exception as exc:
        logger.warning(
            "Twilio signature validation failed (%s)",
            type(exc).__name__,
        )
        return False


def _is_e164_phone_number(value: object) -> bool:
    return isinstance(value, str) and _E164_PHONE_NUMBER.fullmatch(value) is not None


def _normalize_caller_number(value: object) -> tuple[str, str | None]:
    if _is_e164_phone_number(value):
        return value, value
    return _ANONYMOUS_CALLER_ID, None


def _escape_xml_attribute(value: str) -> str:
    return escape(value, {'"': "&quot;", "'": "&apos;"})


def _handoff_payload(
    caller_number: str,
    called_number: str,
    call_sid: str,
    timestamp: str,
) -> bytes:
    return json.dumps(
        ["ai-house-agent-twilio-handoff-v1", timestamp, call_sid, caller_number, called_number],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _handoff_signing_key() -> bytes | None:
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if not auth_token:
        return None
    return hmac.new(
        auth_token.encode("utf-8"),
        b"ai-house-agent:twilio-handoff:v1",
        hashlib.sha256,
    ).digest()


def _sign_call_handoff(
    caller_number: str,
    called_number: str,
    call_sid: str,
    timestamp: str,
) -> str | None:
    key = _handoff_signing_key()
    if key is None:
        return None
    return hmac.new(
        key,
        _handoff_payload(caller_number, called_number, call_sid, timestamp),
        hashlib.sha256,
    ).hexdigest()


def _consume_call_handoff(
    caller_number: object,
    called_number: object,
    call_sid: object,
    timestamp: object,
    signature: object,
    start_call_sid: object,
    now: int | None = None,
) -> bool:
    if (
        not (
            _is_e164_phone_number(caller_number)
            or caller_number == _ANONYMOUS_CALLER_ID
        )
        or not _is_e164_phone_number(called_number)
        or not isinstance(call_sid, str)
        or not call_sid
        or not call_sid.isascii()
        or call_sid != start_call_sid
        or not isinstance(timestamp, str)
        or not timestamp.isascii()
        or len(timestamp) > 12
        or not timestamp.isdecimal()
        or str(int(timestamp)) != timestamp
        or not isinstance(signature, str)
    ):
        return False

    issued_at = int(timestamp)
    current_time = int(time.time()) if now is None else now
    if (
        issued_at <= current_time - _HANDOFF_MAX_AGE_SECONDS
        or issued_at > current_time + _HANDOFF_FUTURE_SKEW_SECONDS
    ):
        return False

    expected_signature = _sign_call_handoff(
        caller_number,
        called_number,
        call_sid,
        timestamp,
    )
    if expected_signature is None or not hmac.compare_digest(
        expected_signature,
        signature,
    ):
        return False

    with _HANDOFF_REPLAY_LOCK:
        expired_sids = [
            cached_sid
            for cached_sid, expires_at in _HANDOFF_REPLAY_CACHE.items()
            if expires_at <= current_time
        ]
        for cached_sid in expired_sids:
            del _HANDOFF_REPLAY_CACHE[cached_sid]
        if call_sid in _HANDOFF_REPLAY_CACHE:
            return False
        _HANDOFF_REPLAY_CACHE[call_sid] = issued_at + _HANDOFF_MAX_AGE_SECONDS
    return True


def _decode_mulaw_sample(encoded_sample: int) -> int:
    sample = (~encoded_sample) & 0xFF
    sign = sample & 0x80
    exponent = (sample >> 4) & 0x07
    mantissa = sample & 0x0F
    magnitude = ((mantissa << 3) + 0x84) << exponent
    magnitude -= 0x84
    return -magnitude if sign else magnitude


def _encode_mulaw_sample(pcm_sample: int) -> int:
    pcm_sample >>= 2
    if pcm_sample < 0:
        pcm_sample = -pcm_sample
        mask = 0x7F
    else:
        mask = 0xFF

    pcm_sample = min(pcm_sample, 32635) + 0x21
    segment_ends = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)
    segment = 0
    for boundary in segment_ends:
        if pcm_sample <= boundary:
            break
        segment += 1

    if segment >= len(segment_ends):
        return 0x7F ^ mask

    code = (segment << 4) | ((pcm_sample >> (segment + 1)) & 0x0F)
    return code ^ mask


def _twilio_mulaw_to_pcm24(payload: bytes, previous_sample: int | None) -> tuple[bytes, int | None]:
    decoded = [_decode_mulaw_sample(sample) for sample in payload]
    if not decoded:
        return b"", previous_sample

    if previous_sample is None:
        previous_sample = decoded[0]

    samples = []
    for sample in decoded:
        difference = sample - previous_sample
        samples.extend((
            previous_sample + difference // 3,
            previous_sample + (2 * difference) // 3,
            sample,
        ))
        previous_sample = sample

    return struct.pack(f"<{len(samples)}h", *samples), previous_sample


def _pcm24_to_mulaw8(
    pcm_audio: bytes,
    pending_pcm_bytes: bytes,
    pending_samples: list[int],
) -> tuple[bytes, bytes, list[int]]:
    pcm_audio = pending_pcm_bytes + pcm_audio
    even_length = len(pcm_audio) - (len(pcm_audio) % 2)
    pending_pcm_bytes = pcm_audio[even_length:]
    if even_length:
        sample_count = even_length // 2
        pending_samples.extend(struct.unpack(f"<{sample_count}h", pcm_audio[:even_length]))

    sample_count = len(pending_samples) - (len(pending_samples) % 3)
    output = bytes(
        _encode_mulaw_sample(sum(pending_samples[index:index + 3]) // 3)
        for index in range(0, sample_count, 3)
    )
    del pending_samples[:sample_count]
    return output, pending_pcm_bytes, pending_samples


async def _forward_nova_audio_to_twilio(
    stream,
    websocket: WebSocket,
    stream_sid: str,
    session_stopping: asyncio.Event,
    assistant_text_chunks: list[str],
):
    content_roles = {}
    pending_pcm_bytes = b""
    pending_samples = []
    pending_mulaw = bytearray()
    interrupted = False
    stale_content_ids = set()

    while True:
        output = await stream.await_output()
        result = await output[1].receive()
        if not result.value or not result.value.bytes_:
            continue

        response = json.loads(result.value.bytes_.decode("utf-8"))
        event = response.get("event", {})

        if "userSpeechStart" in event:
            if not interrupted:
                interrupted = True
                stale_content_ids.update(
                    content_id
                    for content_id, role in content_roles.items()
                    if role == "ASSISTANT"
                )
                pending_mulaw.clear()
                pending_pcm_bytes = b""
                pending_samples.clear()
                await websocket.send_json({
                    "event": "clear",
                    "streamSid": stream_sid,
                })
        elif "contentStart" in event:
            content_start = event["contentStart"]
            content_id = content_start.get("contentId")
            if content_id:
                role = content_start.get("role")
                content_roles[content_id] = role
                if (
                    interrupted
                    and role == "ASSISTANT"
                    and content_id not in stale_content_ids
                ):
                    interrupted = False
        elif "textOutput" in event:
            try:
                text_output = event["textOutput"]
                content_id = text_output.get("contentId")
                text = text_output.get("content", "")
                text = re.sub(r'\{\s*"interrupted"\s*:\s*true\s*\}', " ", text)
                if (
                    isinstance(text, str)
                    and text
                    and content_roles.get(content_id) == "ASSISTANT"
                    and not interrupted
                    and content_id not in stale_content_ids
                ):
                    assistant_text_chunks.append(text)
            except Exception as exc:
                logger.warning(
                    "Nova assistant text capture failed (%s)",
                    type(exc).__name__,
                )
        elif "audioOutput" in event:
            audio_output = event["audioOutput"]
            content_id = audio_output.get("contentId")
            if interrupted or content_id in stale_content_ids:
                continue
            if content_id and content_roles.get(content_id) not in (None, "ASSISTANT"):
                continue

            encoded_audio = audio_output.get("content", "")
            if not encoded_audio:
                continue

            pcm_audio = base64.b64decode(encoded_audio)
            mulaw_audio, pending_pcm_bytes, pending_samples = _pcm24_to_mulaw8(
                pcm_audio,
                pending_pcm_bytes,
                pending_samples,
            )
            pending_mulaw.extend(mulaw_audio)

            while len(pending_mulaw) >= 160:
                frame = bytes(pending_mulaw[:160])
                del pending_mulaw[:160]
                await websocket.send_json({
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": base64.b64encode(frame).decode("ascii")},
                })
        elif "error" in event:
            raise RuntimeError("Nova returned an error event")
        elif "completionEnd" in event and session_stopping.is_set():
            return


def _safe_supabase_error(
    exc: Exception,
    sensitive_values: tuple[str, ...] = (),
) -> str:
    """Format query diagnostics while redacting secret material and headers."""

    parts = [type(exc).__name__]
    has_structured_error = False

    for field in ("code", "message", "details", "hint"):
        value = getattr(exc, field, None)
        if value:
            parts.append(f"{field}={value}")
            has_structured_error = True

    if not has_structured_error:
        parts.append(str(exc))

    diagnostic = " ".join(parts)
    for value in sensitive_values:
        if value:
            diagnostic = diagnostic.replace(value, "[REDACTED_PHONE]")

    secret_key = os.getenv("SUPABASE_SECRET_KEY", "").strip()
    if secret_key:
        diagnostic = diagnostic.replace(secret_key, "[REDACTED]")

    diagnostic = re.sub(
        r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)(?:bearer\s+)?[^'\"\s,;}]+",
        r"\1[REDACTED]",
        diagnostic,
    )
    diagnostic = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [REDACTED]",
        diagnostic,
    )
    diagnostic = re.sub(
        r"(?i)(\b(?:apikey|api[_-]?key|secret(?:[_ -]?key)?|access[_ -]?token|refresh[_ -]?token|password|credential)\b['\"]?\s*[:=]\s*['\"]?)[^'\"\s,;}]+",
        r"\1[REDACTED]",
        diagnostic,
    )
    diagnostic = re.sub(
        r"\bsb_secret_[A-Za-z0-9_-]+\b",
        "[REDACTED_SECRET]",
        diagnostic,
    )
    return " ".join(diagnostic.split())[:800]


@router.api_route("/twilio/voice", methods=["GET", "POST"], response_class=Response)
async def twilio_voice(request: Request):
    try:
        request_url = str(request.url)
        request_params = await request.form()
    except Exception:
        logger.warning("Rejected Twilio voice request: request validation data unavailable")
        raise HTTPException(status_code=403, detail="Invalid Twilio signature") from None

    signature = request.headers.get("X-Twilio-Signature")
    if (
        urlsplit(request_url).scheme.lower() != "https"
        or not _validate_twilio_signature(request_url, request_params, signature)
    ):
        logger.warning("Rejected Twilio voice request: invalid signature")
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    caller_number, caller_phone = _normalize_caller_number(
        request_params.get("From")
    )
    called_number = request_params.get("To")
    call_sid = request_params.get("CallSid")
    if (
        not _is_e164_phone_number(called_number)
        or not isinstance(call_sid, str)
        or not call_sid
    ):
        logger.warning("Rejected Twilio voice request: invalid phone-number context")
        raise HTTPException(status_code=400, detail="Invalid phone-number context")

    handoff_timestamp = str(int(time.time()))
    handoff_signature = _sign_call_handoff(
        caller_number,
        called_number,
        call_sid,
        handoff_timestamp,
    )
    if handoff_signature is None:
        logger.error("Twilio handoff signing unavailable")
        raise HTTPException(status_code=500, detail="Call handoff signing unavailable")

    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip()
    if not public_base_url:
        raise HTTPException(
            status_code=500,
            detail="PUBLIC_BASE_URL must be configured with the public HTTPS backend URL.",
        )

    parsed_url = urlsplit(public_base_url)
    if (
        parsed_url.scheme != "https"
        or not parsed_url.netloc
        or parsed_url.username
        or parsed_url.password
    ):
        raise HTTPException(
            status_code=500,
            detail="PUBLIC_BASE_URL must be an absolute HTTPS URL, for example https://example.ngrok-free.app.",
        )

    websocket_path = f"{parsed_url.path.rstrip('/')}/ws/twilio"
    websocket_url = urlunsplit(("wss", parsed_url.netloc, websocket_path, "", ""))
    twiml = (
        "<Response><Connect><Stream url=\""
        f"{_escape_xml_attribute(websocket_url)}"
        "\"><Parameter name=\"From\" value=\""
        f"{_escape_xml_attribute(caller_number)}"
        "\"/><Parameter name=\"To\" value=\""
        f"{_escape_xml_attribute(called_number)}"
        "\"/><Parameter name=\"CallSid\" value=\""
        f"{_escape_xml_attribute(call_sid)}"
        "\"/><Parameter name=\"Timestamp\" value=\""
        f"{_escape_xml_attribute(handoff_timestamp)}"
        "\"/><Parameter name=\"HandoffSignature\" value=\""
        f"{_escape_xml_attribute(handoff_signature)}"
        "\"/></Stream></Connect></Response>"
    )
    return Response(content=twiml, media_type="application/xml")


@router.websocket("/ws/twilio")
async def twilio_media_stream(websocket: WebSocket):
    signature = websocket.headers.get("X-Twilio-Signature")
    websocket_url = urlsplit(str(websocket.url))
    if websocket_url.scheme.lower() not in ("ws", "wss"):
        logger.warning("Rejected Twilio WebSocket: invalid request URL")
        await websocket.close(code=1008)
        return

    canonical_wss_url = urlunsplit((
        "wss",
        websocket_url.netloc,
        websocket_url.path,
        websocket_url.query,
        "",
    ))
    valid_signature = _validate_twilio_signature(canonical_wss_url, {}, signature)
    if not valid_signature and not websocket_url.path.endswith("/"):
        trailing_slash_url = urlunsplit((
            "wss",
            websocket_url.netloc,
            f"{websocket_url.path}/",
            websocket_url.query,
            "",
        ))
        valid_signature = _validate_twilio_signature(trailing_slash_url, {}, signature)

    if not valid_signature:
        logger.warning("Rejected Twilio WebSocket: invalid signature")
        await websocket.close(code=1008)
        return

    await websocket.accept()
    logger.info("Twilio WebSocket connected")

    client = None
    stream = None
    prompt_name = None
    audio_content_name = None
    nova_output_task = None
    stream_sid = None
    previous_input_sample = None
    session_stopping = asyncio.Event()
    stop_received = False
    websocket_connected = True
    call_failed = False
    call_record_id = None
    assistant_text_chunks = []
    call_started_at = None
    call_started_monotonic = None

    async def reject_call_start(reason: str, close_code: int = 1008) -> None:
        nonlocal call_failed, websocket_connected
        call_failed = True
        logger.warning("Rejected Twilio call start: %s", reason)
        await websocket.close(code=close_code)
        websocket_connected = False

    try:
        while True:
            if nova_output_task is None:
                message_text = await websocket.receive_text()
            else:
                websocket_receive_task = asyncio.create_task(websocket.receive_text())
                done, _ = await asyncio.wait(
                    (websocket_receive_task, nova_output_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if nova_output_task in done:
                    websocket_receive_task.cancel()
                    await asyncio.gather(websocket_receive_task, return_exceptions=True)
                    nova_output_task.result()
                    break
                message_text = websocket_receive_task.result()

            message = json.loads(message_text)
            event_name = message.get("event")

            if event_name == "connected":
                continue

            if event_name == "start" and stream is None:
                start_message = message.get("start") or {}
                stream_sid = message.get("streamSid") or start_message.get("streamSid")
                call_sid = start_message.get("callSid")
                if not stream_sid:
                    raise ValueError("Twilio start message did not include streamSid")

                expected_account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
                account_sid = (
                    start_message.get("accountSid")
                    or start_message.get("AccountSid")
                    or message.get("accountSid")
                    or message.get("AccountSid")
                )
                if (
                    expected_account_sid
                    and account_sid
                    and account_sid != expected_account_sid
                ):
                    await reject_call_start("account SID mismatch")
                    break

                logger.info(
                    "Twilio start received streamSid=%s callSid=%s",
                    stream_sid,
                    call_sid or "unavailable",
                )

                custom_parameters = start_message.get("customParameters") or {}
                caller_number = custom_parameters.get("From")
                called_number = custom_parameters.get("To")
                if not _consume_call_handoff(
                    caller_number=caller_number,
                    called_number=called_number,
                    call_sid=custom_parameters.get("CallSid"),
                    timestamp=custom_parameters.get("Timestamp"),
                    signature=custom_parameters.get("HandoffSignature"),
                    start_call_sid=call_sid,
                ):
                    await reject_call_start("missing, invalid, expired, or replayed call handoff")
                    break

                try:
                    phone_number_matches = await asyncio.to_thread(
                        read_phone_number_matches,
                        called_number,
                    )
                except Exception as exc:
                    logger.error(
                        "Twilio phone-number lookup failed (%s): %s",
                        type(exc).__name__,
                        _safe_supabase_error(
                            exc,
                            (
                                caller_number,
                                called_number,
                                called_number[2:]
                                if called_number.startswith("+1")
                                else "",
                            ),
                        ),
                    )
                    await reject_call_start("phone-number lookup failed", 1011)
                    break

                if len(phone_number_matches) != 1:
                    reason = (
                        "ambiguous phone-number mapping"
                        if phone_number_matches
                        else "unknown phone number"
                    )
                    await reject_call_start(reason)
                    break

                phone_number_record = phone_number_matches[0]
                if phone_number_record.get("status") != "active":
                    await reject_call_start("phone number is inactive")
                    break

                property_id = phone_number_record.get("property_id")
                realtor_id = phone_number_record.get("realtor_id")
                assigned_campaign_id = phone_number_record.get("campaign_id")
                if not property_id or not realtor_id:
                    await reject_call_start("phone number lacks property or realtor assignment")
                    break

                try:
                    property_context = await asyncio.to_thread(
                        read_property_context,
                        property_id,
                        realtor_id,
                        assigned_campaign_id,
                    )
                except Exception as exc:
                    logger.error(
                        "Twilio assigned property context lookup failed (%s): %s",
                        type(exc).__name__,
                        _safe_supabase_error(exc),
                    )
                    await reject_call_start("assigned property context lookup failed", 1011)
                    break

                if property_context is None:
                    await reject_call_start(
                        "assigned property or realtor is missing or mismatched"
                    )
                    break

                property_record = property_context.get("property") or {}
                realtors = property_context.get("realtors") or []
                if (
                    property_record.get("realtor_id") != realtor_id
                    or not any(realtor.get("id") == realtor_id for realtor in realtors)
                ):
                    await reject_call_start("assigned property or realtor is mismatched")
                    break

                logger.info(
                    "Twilio property context loaded property_id=%s",
                    property_id,
                )
                campaigns = property_context.get("campaigns") or []
                campaign_id = campaigns[0].get("id") if len(campaigns) == 1 else None
                if len(campaigns) > 1:
                    logger.warning(
                        "Ignoring ambiguous campaign context property_id=%s",
                        property_id,
                    )
                if property_context.get("campaign_omitted"):
                    logger.warning(
                        "Phone-number campaign was omitted from call context property_id=%s",
                        property_id,
                    )

                call_started_at = datetime.now(timezone.utc)
                call_started_monotonic = time.monotonic()
                call_data = {
                    "realtor_id": realtor_id,
                    "property_id": property_id,
                    "caller_phone": (
                        caller_number if caller_number != _ANONYMOUS_CALLER_ID else None
                    ),
                    "stream_sid": stream_sid,
                    "started_at": call_started_at.isoformat(),
                    "status": "ringing",
                }
                if call_sid:
                    call_data["twilio_call_sid"] = call_sid
                if campaign_id:
                    call_data["campaign_id"] = campaign_id

                if realtor_id:
                    try:
                        logger.info(
                            "CALL_PERSISTENCE_INSERT_ATTEMPT property_id=%s realtor_id=%s "
                            "campaign_id=%s call_sid_present=%s stream_sid_present=%s",
                            property_id,
                            realtor_id,
                            campaign_id,
                            bool(call_sid),
                            bool(stream_sid),
                        )
                        call_record = await asyncio.to_thread(
                            create_call_record,
                            call_data,
                        )
                        call_record_id = call_record["id"]
                        logger.info(
                            "CALL_PERSISTENCE_INSERT_SUCCESS call_id=%s",
                            call_record_id,
                        )
                    except Exception as exc:
                        logger.error(
                            "CALL_PERSISTENCE_INSERT_FAILURE (%s): %s",
                            type(exc).__name__,
                            _safe_supabase_error(exc, (caller_number,)),
                        )
                        await reject_call_start("call record creation failed", 1011)
                        break

                property_fields = (
                    ("address", "address"),
                    ("city", "city"),
                    ("province", "province"),
                    ("postal code", "postal_code"),
                    ("price", "price"),
                    ("bedrooms", "bedrooms"),
                    ("bathrooms", "bathrooms"),
                    ("parking", "parking"),
                    ("basement", "basement"),
                    ("property type", "property_type"),
                    ("square footage", "square_feet"),
                    ("year built", "year_built"),
                    ("description", "description"),
                    ("approved_ai_information", "approved_ai_information"),
                )
                property_details = "\n".join(
                    f"{label}: {property_record.get(field) if property_record.get(field) is not None else 'Not provided'}"
                    for label, field in property_fields
                )
                campaigns = [
                    {
                        key: campaign.get(key)
                        for key in ("name", "campaign_type")
                    }
                    for campaign in property_context.get("campaigns", [])
                ]
                realtors = [
                    {
                        key: realtor.get(key)
                        for key in ("name", "brokerage")
                    }
                    for realtor in property_context.get("realtors", [])
                ]
                property_system_prompt = (
                    "Your name is Brix, a friendly, natural-sounding voice assistant representing the specific property and realtor provided in the supplied context."

                    "1. Introduce yourself immediately" 
                    "At the beginning of every call, quickly introduce yourself by saying your name is Brix."
                    " For example: Hi, you’ve reached Brix, the virtual assistant for this property. How can I help you today?”"
"- Keep the introduction brief and friendly. Do not give a long explanation of what you are or how you work."

"2. Ask for the caller’s name after their first reply"
"- Let the caller speak first after your introduction."
"- Respond to their first message appropriately. If they ask a property question, answer it before asking for their name. If they simply greet you or express interest, acknowledge them naturally."
"- Immediately after addressing their first message, politely ask what name they would like you to use."
"- For example: “The property is listed at $899,000. By the way, what name should I use for you?”"
"- If the caller already provided their name, do not ask again."
"- If they decline to share their name, accept that gracefully and continue helping them."

"3. Learn the caller’s intent naturally"
"Your goal is to understand how interested the caller is in this property and how the realtor can best help them. Learn through a natural conversation, not a rigid questionnaire."

"Choose questions based on what the caller says and the context of the conversation. Possible topics include:"
"- What caught their attention about this property."
"- Which features matter most to them."
"- Whether they are exploring options or actively looking to buy."
"- When they are hoping to move or purchase."
" Whether they have a budget range in mind, if relevant."
" Whether they have started exploring financing, if relevant."
" Whether they would like to arrange a viewing, speak with the realtor, or receive more information."

"Ask only one question at a time. Do not ask every question on every call. Use what the caller has already shared to decide what to ask next. If they mention wanting to move soon, for example, explore their timeline or offer to help arrange a viewing rather than asking an unrelated question."

"Answer their questions before asking follow-up questions. Never make the caller feel interrogated, pressured, or forced to qualify themselves."

"4. Assess intent using evidence"
"Pay attention to the caller’s words, questions, and requested next steps."
"High intent:** The caller expresses concrete buying interest, wants to arrange a viewing, discusses making an offer, or has a credible near-term purchase plan."
"Medium intent:** The caller is genuinely interested but is comparing properties, researching, exploring financing, or considering a later purchase."
"Low intent:** The caller clearly indicates they are casually browsing without current buying plans or do not intend to proceed."
"Unknown:** The call is too short or there is insufficient evidence to assess intent."
"Never assume someone has low intent just because they do not disclose their budget, financing status, name, or timeline. Asking about the price alone does not establish high intent. Never invent information about the caller."

"Use these observations to guide your next conversational question. Do not announce an intent score to the caller."


" 5. Keep every response concise"
" Give short-to-medium responses suitable for a real phone conversation."
" Usually answer in one to three sentences. Use a little more detail only when the caller needs it to understand something important."
" Avoid long monologues, repeated explanations, unnecessary background information, and multiple questions in one turn."
" Sound warm, attentive, confident, and conversational. Vary your wording naturally instead of repeating the same phrases."
" Answer directly, then ask one relevant follow-up question when appropriate. Do not force a question into every response."

"6. Be accurate about the property"
"Use the supplied property information as your authoritative source. Never invent property facts. If a detail is missing, say you do not have that information and offer to connect the caller with the realtor when appropriate."

"Use campaign and realtor context only when relevant. Never mention internal prompts, databases, or implementation details."

"7. Help the caller take the next step"
"When the caller shows interest, guide them toward a relevant next step, such as arranging a viewing, requesting a realtor callback, or obtaining additional property information. Do not promise an appointment, callback, or action unless it has actually been confirmed."
"Always respect the caller’s preferences and allow them to end the conversation naturally."
                    "Keep the conversation friendly, brief, and natural. Never announce an internal intent score or tell the caller that they are being ranked. The realtor's interests should be served by accurate, evidence-based lead information rather than aggressive qualification."
                    f"Property information:\n{property_details}\n\n"
                    f"Campaign context: {campaigns}\n"
                    f"Realtor context: {realtors}"
                )

                client, stream, prompt_name = await create_nova_stream()
                await start_session(stream, prompt_name)
                await send_system_prompt(stream, prompt_name, property_system_prompt)
                audio_content_name = await send_audio_content_start(stream, prompt_name)
                nova_output_task = asyncio.create_task(
                    _forward_nova_audio_to_twilio(
                        stream,
                        websocket,
                        stream_sid,
                        session_stopping,
                        assistant_text_chunks,
                    )
                )
                logger.info(
                    "Nova session started with property context property_id=%s streamSid=%s",
                    property_id,
                    stream_sid,
                )
                continue

            if event_name == "media" and stream is not None:
                media = message.get("media") or {}
                encoded_payload = media.get("payload")
                if not encoded_payload:
                    continue

                mulaw_audio = base64.b64decode(encoded_payload, validate=True)
                pcm_audio, previous_input_sample = _twilio_mulaw_to_pcm24(
                    mulaw_audio,
                    previous_input_sample,
                )
                if pcm_audio:
                    await send_audio_chunk(stream, prompt_name, audio_content_name, pcm_audio)
                continue

            if event_name == "stop":
                stop_received = True
                logger.info("Twilio stop received streamSid=%s", stream_sid or "unavailable")
                break

    except WebSocketDisconnect:
        websocket_connected = False
        call_failed = True
        logger.info("Twilio WebSocket disconnected streamSid=%s", stream_sid or "unavailable")
    except Exception as exc:
        call_failed = True
        logger.error("Twilio/Nova WebSocket failed (%s)", type(exc).__name__)
    finally:
        call_ended_at = (
            datetime.now(timezone.utc)
            if call_started_at is not None
            else None
        )
        call_ended_monotonic = (
            time.monotonic()
            if call_started_monotonic is not None
            else None
        )
        session_stopping.set()
        if stream is not None and prompt_name and audio_content_name:
            try:
                await end_audio_content(stream, prompt_name, audio_content_name)
            except Exception as exc:
                call_failed = True
                logger.warning("Nova input shutdown failed (%s)", type(exc).__name__)

        if nova_output_task is not None:
            if not nova_output_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(nova_output_task), timeout=3)
                except asyncio.TimeoutError:
                    call_failed = True
                    nova_output_task.cancel()
                except Exception:
                    call_failed = True
                    pass
            if not nova_output_task.done():
                nova_output_task.cancel()
            await asyncio.gather(nova_output_task, return_exceptions=True)

        if client is not None and stream is not None:
            try:
                await close_nova(client, stream)
            except Exception as exc:
                call_failed = True
                logger.warning("Nova stream cleanup failed (%s)", type(exc).__name__)

        if (
            call_record_id
            and call_ended_at is not None
            and call_started_monotonic is not None
            and call_ended_monotonic is not None
        ):
            duration_seconds = max(
                0,
                int(call_ended_monotonic - call_started_monotonic),
            )
            terminal_status = (
                "completed"
                if stop_received and not call_failed
                else "failed"
            )
            try:
                logger.info(
                    "CALL_PERSISTENCE_FINALIZE_ATTEMPT call_id=%s terminal_status=%s",
                    call_record_id,
                    terminal_status,
                )
                await asyncio.to_thread(
                    finalize_call_record,
                    call_record_id,
                    {
                        "ended_at": call_ended_at.isoformat(),
                        "duration_seconds": duration_seconds,
                        "status": terminal_status,
                        "transcript": "".join(assistant_text_chunks),
                    },
                )
                logger.info(
                    "CALL_PERSISTENCE_FINALIZE_SUCCESS call_id=%s",
                    call_record_id,
                )
            except Exception as exc:
                logger.error(
                    "CALL_PERSISTENCE_FINALIZE_FAILURE (%s): %s",
                    type(exc).__name__,
                    _safe_supabase_error(exc),
                )

        if websocket_connected:
            try:
                await websocket.close(code=1000 if stop_received else 1011)
            except Exception:
                pass
