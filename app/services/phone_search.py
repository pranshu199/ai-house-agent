"""Bounded search for available Canadian local phone numbers."""

import re
import threading
import time
from collections import deque
from collections.abc import Mapping

from fastapi import HTTPException

from app.integrations.twilio_client import get_twilio_client
from app.schemas.phone_numbers import PhoneNumberSearchRequest

_CANADIAN_E164_PATTERN = re.compile(r"\+1[2-9][0-9]{9}\Z")
_RATE_WINDOW_SECONDS = 300
_RATE_LIMIT_PER_WINDOW = 20
_SEARCH_RATE_LOCK = threading.Lock()
_SEARCH_REQUESTS: dict[str, deque[float]] = {}


def enforce_search_rate_limit(realtor_id: str, now: float | None = None) -> None:
    """Limit each realtor to 20 searches in a rolling five-minute window."""

    current_time = time.monotonic() if now is None else now
    cutoff = current_time - _RATE_WINDOW_SECONDS

    with _SEARCH_RATE_LOCK:
        for key, requests in tuple(_SEARCH_REQUESTS.items()):
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if not requests:
                del _SEARCH_REQUESTS[key]

        requests = _SEARCH_REQUESTS.setdefault(realtor_id, deque())
        if len(requests) >= _RATE_LIMIT_PER_WINDOW:
            retry_after = max(1, int(requests[0] + _RATE_WINDOW_SECONDS - current_time))
            raise HTTPException(
                status_code=429,
                detail="Phone number search rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )
        requests.append(current_time)


def _capabilities(value: object) -> dict[str, bool] | None:
    if not isinstance(value, Mapping):
        return None

    normalized = {str(key).lower(): capability for key, capability in value.items()}
    if normalized.get("voice") is not True:
        return None

    return {
        "voice": True,
        "sms": normalized.get("sms") is True,
        "mms": normalized.get("mms") is True,
        "fax": normalized.get("fax") is True,
    }


def _provider_text(value: object, max_length: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_length]


def search_canadian_local_numbers(
    request: PhoneNumberSearchRequest,
) -> list[dict]:
    """Search Twilio's Canadian local inventory; this never purchases a number."""

    search_parameters: dict[str, object] = {
        "voice_enabled": True,
        "limit": request.limit,
    }
    if request.area_code:
        search_parameters["area_code"] = int(request.area_code)
    if request.locality:
        search_parameters["in_locality"] = request.locality

    available_numbers = (
        get_twilio_client()
        .available_phone_numbers("CA")
        .local.list(**search_parameters)
    )

    results = []
    for number in available_numbers:
        phone_number = getattr(number, "phone_number", None)
        capabilities = _capabilities(getattr(number, "capabilities", None))
        if (
            getattr(number, "iso_country", None) != "CA"
            or not isinstance(phone_number, str)
            or not _CANADIAN_E164_PATTERN.fullmatch(phone_number)
            or capabilities is None
        ):
            continue

        results.append(
            {
                "phone_number": phone_number,
                "friendly_name": _provider_text(
                    getattr(number, "friendly_name", None),
                    80,
                ),
                "locality": _provider_text(getattr(number, "locality", None), 60),
                "region": _provider_text(getattr(number, "region", None), 2),
                "capabilities": capabilities,
            }
        )
        if len(results) >= request.limit:
            break

    return results
