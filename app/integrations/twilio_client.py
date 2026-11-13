"""Server-side Twilio REST client configuration."""

import os
import re
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

from requests.exceptions import RequestException
from twilio.base.exceptions import TwilioException, TwilioRestException
from twilio.rest import Client


class TwilioConfigurationError(RuntimeError):
    """Raised when Twilio server-side credentials are unavailable."""


class TwilioInventoryLookupError(RuntimeError):
    """Raised when Canadian available-number lookup fails before purchase."""


_CANADIAN_E164_PATTERN = re.compile(r"\+1[2-9][0-9]{9}\Z")
_TWILIO_BUNDLE_SID_PATTERN = re.compile(r"BU[0-9A-Fa-f]{32}\Z")
_VOICE_METHOD = "POST"


def get_twilio_client() -> Client:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if not account_sid or not auth_token:
        raise TwilioConfigurationError("Twilio credentials are not configured")
    return Client(account_sid, auth_token)


def get_twilio_voice_webhook_url() -> str:
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip()
    parsed_url = urlsplit(public_base_url)
    if (
        parsed_url.scheme.lower() != "https"
        or not parsed_url.netloc
        or parsed_url.username
        or parsed_url.password
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise TwilioConfigurationError(
            "PUBLIC_BASE_URL must be an absolute HTTPS backend URL"
        )

    voice_path = f"{parsed_url.path.rstrip('/')}/twilio/voice"
    return urlunsplit(("https", parsed_url.netloc, voice_path, "", ""))


def _provisioning_friendly_name(request_id: str) -> str:
    return f"AIHA provisioning {request_id}"


def _has_voice_capability(value: object) -> bool:
    return isinstance(value, Mapping) and value.get("voice") is True


def purchase_canadian_local_number(phone_number: str, request_id: str):
    if not _CANADIAN_E164_PATTERN.fullmatch(phone_number):
        raise ValueError("A Canadian E.164 phone number is required")

    client = get_twilio_client()
    try:
        available_numbers = client.available_phone_numbers("CA").local.list(
            area_code=int(phone_number[2:5]),
            contains=phone_number[-7:],
            voice_enabled=True,
            limit=20,
        )
    except (TwilioException, RequestException):
        raise TwilioInventoryLookupError from None
    if not any(
        getattr(number, "phone_number", None) == phone_number
        and getattr(number, "iso_country", None) == "CA"
        and _has_voice_capability(getattr(number, "capabilities", None))
        for number in available_numbers
    ):
        raise ValueError("Selected number is not available for Canadian voice")

    purchase_parameters = {
        "phone_number": phone_number,
        "friendly_name": _provisioning_friendly_name(request_id),
        "voice_url": get_twilio_voice_webhook_url(),
        "voice_method": _VOICE_METHOD,
    }
    bundle_sid = os.getenv("TWILIO_CA_REGULATORY_BUNDLE_SID", "").strip()
    if bundle_sid:
        if not _TWILIO_BUNDLE_SID_PATTERN.fullmatch(bundle_sid):
            raise TwilioConfigurationError(
                "TWILIO_CA_REGULATORY_BUNDLE_SID is invalid"
            )
        purchase_parameters["bundle_sid"] = bundle_sid

    return client.incoming_phone_numbers.create(**purchase_parameters)


def find_provisioned_canadian_number(phone_number: str, request_id: str):
    if not _CANADIAN_E164_PATTERN.fullmatch(phone_number):
        raise ValueError("A Canadian E.164 phone number is required")

    matches = get_twilio_client().incoming_phone_numbers.list(
        phone_number=phone_number,
        friendly_name=_provisioning_friendly_name(request_id),
        limit=20,
    )
    for number in matches:
        if (
            getattr(number, "phone_number", None) == phone_number
            and getattr(number, "iso_country", None) == "CA"
            and getattr(number, "friendly_name", None)
            == _provisioning_friendly_name(request_id)
        ):
            return number
    return None


def configure_twilio_voice(number_sid: str):
    return get_twilio_client().incoming_phone_numbers(number_sid).update(
        voice_url=get_twilio_voice_webhook_url(),
        voice_method=_VOICE_METHOD,
    )


def release_twilio_number(number_sid: str) -> bool:
    try:
        return get_twilio_client().incoming_phone_numbers(number_sid).delete()
    except TwilioRestException as exc:
        if exc.status == 404:
            return False
        raise
