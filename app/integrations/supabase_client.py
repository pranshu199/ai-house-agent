"""Backend-only Supabase client and persistence operations."""

import json
import logging
import os
import re
import uuid
from functools import lru_cache

from supabase import Client, create_client


logger = logging.getLogger(__name__)


class SupabaseConfigurationError(RuntimeError):
    """Raised when required backend Supabase settings are missing."""


@lru_cache(maxsize=1)
def get_supabase_client() -> Client:
    """Create and cache the backend Supabase client from environment settings."""

    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    secret_key = os.getenv("SUPABASE_SECRET_KEY", "").strip()

    missing_variables = []
    if not supabase_url:
        missing_variables.append("SUPABASE_URL")
    if not secret_key:
        missing_variables.append("SUPABASE_SECRET_KEY")

    if missing_variables:
        missing = ", ".join(missing_variables)
        raise SupabaseConfigurationError(
            f"Missing required backend environment variable(s): {missing}"
        )

    return create_client(supabase_url, secret_key)


def read_first_property() -> dict | None:
    """Read only safe test fields from one existing property, if present."""

    response = (
        get_supabase_client()
        .table("properties")
        .select("id,city,approved_ai_information")
        .limit(1)
        .execute()
    )

    properties = response.data or []
    return properties[0] if properties else None

def read_phone_number_matches(called_number: str) -> list[dict]:
    """Find exact E.164 or confirmed NANP national-number matches."""

    if not re.fullmatch(r"\+[1-9][0-9]{0,14}", called_number):
        raise ValueError("Called number must be in strict E.164 format")

    phone_numbers = [called_number]
    if called_number.startswith("+1") and len(called_number) == 12:
        phone_numbers.append(called_number[2:])

    response = (
        get_supabase_client()
        .table("phone_numbers")
        .select("id,realtor_id,property_id,campaign_id,phone_number,status")
        .in_("phone_number", phone_numbers)
        .execute()
    )
    return response.data or []


def read_property_context(
    property_id: str,
    realtor_id: str,
    campaign_id: str | None,
) -> dict | None:
    """Read and validate the property, assigned realtor, and optional campaign."""

    property_response = (
        get_supabase_client()
        .table("properties")
        .select(
            "id,realtor_id,address,city,province,postal_code,price,bedrooms,bathrooms,"
            "parking,basement,property_type,square_feet,year_built,"
            "description,approved_ai_information,status"
        )
        .eq("id", property_id)
        .limit(1)
        .execute()
    )

    properties = property_response.data or []
    if not properties:
        return None

    property_record = properties[0]
    if property_record.get("realtor_id") != realtor_id:
        return None

    realtor_response = (
        get_supabase_client()
        .table("realtors")
        .select("id,name,email,phone,brokerage")
        .eq("id", realtor_id)
        .limit(1)
        .execute()
    )
    realtor_records = realtor_response.data or []
    if not realtor_records:
        return None

    campaigns = []
    campaign_omitted = False
    if campaign_id:
        campaign_response = (
            get_supabase_client()
            .table("campaigns")
            .select("id,name,campaign_type,status,realtor_id,property_id")
            .eq("id", campaign_id)
            .limit(1)
            .execute()
        )
        campaign_records = campaign_response.data or []

        if (
            len(campaign_records) != 1
            or campaign_records[0].get("property_id") != property_id
            or campaign_records[0].get("realtor_id") != realtor_id
        ):
            campaign_omitted = True
            logger.warning(
                "Ignoring invalid phone-number campaign assignment campaign_id=%s",
                campaign_id,
            )
        elif campaign_records[0].get("status") != "active":
            campaign_omitted = True
            logger.info(
                "Omitting non-active phone-number campaign assignment campaign_id=%s",
                campaign_id,
            )
        else:
            campaigns = campaign_records

    return {
        "property": property_record,
        "campaigns": campaigns,
        "realtors": realtor_records,
        "campaign_omitted": campaign_omitted,
    }


def create_call_record(call_data: dict) -> dict:
    """Insert a call row and return its database-generated ID."""

    response = (
        get_supabase_client()
        .table("calls")
        .insert(call_data)
        .select("id")
        .execute()
    )
    records = response.data or []
    if not records or not records[0].get("id"):
        raise RuntimeError("Call insert did not return a record ID")
    return records[0]


def finalize_call_record(call_id: str, call_data: dict) -> None:
    """Update terminal call status and timing fields."""

    response = (
        get_supabase_client()
        .table("calls")
        .update(call_data)
        .eq("id", call_id)
        .select("id")
        .execute()
    )
    if not response.data:
        raise RuntimeError("Call finalization did not update a record")


def update_call_summary(call_id: str, summary: str) -> None:
    """Persist only the generated summary for an existing call."""

    response = (
        get_supabase_client()
        .table("calls")
        .update({"ai_summary": summary})
        .eq("id", call_id)
        .select("id")
        .execute()
    )
    if not response.data:
        raise RuntimeError("Call summary update did not update a record")


_E164_PHONE_PATTERN = re.compile(r"\+[1-9][0-9]{7,14}")
_FORMATTED_E164_PHONE_PATTERN = re.compile(r"\+[0-9 ().-]+")


def _validate_database_uuid(value: object, field: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a UUID")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _normalize_lead_phone(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value
    if _FORMATTED_E164_PHONE_PATTERN.fullmatch(candidate) is None:
        return None
    normalized = "+" + re.sub(r"\D", "", candidate)
    return (
        normalized
        if _E164_PHONE_PATTERN.fullmatch(normalized) is not None
        else None
    )


def _find_matching_lead(client, realtor_id: str, phone: str) -> dict | None:
    response = (
        client.table("leads")
        .select(
            "id,realtor_id,source_campaign_id,original_property_id,name,email,"
            "budget_min,budget_max,timeline,intent_score,priority,summary,status,intent,lead_type"
        )
        .eq("realtor_id", realtor_id)
        .eq("normalized_phone", phone)
        .limit(2)
        .execute()
    )
    matches = response.data or []
    if len(matches) > 1:
        raise RuntimeError("Multiple leads match the realtor and normalized phone")
    return matches[0] if matches else None


def _lead_updates(
    lead: dict,
    extracted_fields: dict,
    phone: str,
    score: int,
) -> dict:
    preserve_if_present = {
        "name",
        "email",
        "budget_min",
        "budget_max",
        "timeline",
        "summary",
    }
    updates = {}
    for field, value in extracted_fields.items():
        if value is None or (field in {"lead_type", "intent"} and value == "unknown"):
            continue
        if field in preserve_if_present:
            existing_value = lead.get(field)
            if existing_value is not None and (
                not isinstance(existing_value, str) or existing_value.strip()
            ):
                continue
        updates[field] = value
    old_score = lead.get("intent_score")
    effective_score = max(
        score,
        old_score if isinstance(old_score, int) and not isinstance(old_score, bool) else 0,
    )
    effective_priority = (
        "hot" if effective_score >= 70 else "warm" if effective_score >= 40 else "cold"
    )
    updates.update(
        {
            "phone": phone,
            "intent_score": effective_score,
            "priority": effective_priority,
        }
    )
    return updates


def persist_call_lead(
    call_id: str,
    extraction: dict,
    score: int,
    priority: str,
) -> str:
    """Validate persisted call attribution, then update/create and link its lead."""

    call_id = _validate_database_uuid(call_id, "call_id")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise ValueError("Lead score must be an integer from 0 to 100")
    expected_priority = "hot" if score >= 70 else "warm" if score >= 40 else "cold"
    if priority != expected_priority:
        raise ValueError("Lead priority does not match its score")

    client = get_supabase_client()
    call_response = (
        client.table("calls")
        .select(
            "id,realtor_id,campaign_id,property_id,caller_phone,"
            "transcript,status"
        )
        .eq("id", call_id)
        .limit(1)
        .execute()
    )
    calls = call_response.data or []
    if not calls:
        raise RuntimeError("Completed call was not found for lead capture")
    call = calls[0]
    if call.get("status") != "completed":
        raise ValueError("Lead capture requires a completed call")
    transcript = call.get("transcript")
    if isinstance(transcript, str):
        try:
            transcript = json.loads(transcript)
        except json.JSONDecodeError as exc:
            raise ValueError("Call transcript is malformed") from exc
    if not isinstance(transcript, list) or not transcript:
        raise ValueError("Lead capture requires a non-empty transcript")

    phone = _normalize_lead_phone(call.get("caller_phone"))
    if phone is None:
        raise ValueError("Lead capture requires a canonical caller phone number")

    realtor_id = _validate_database_uuid(call.get("realtor_id"), "realtor_id")
    property_id = _validate_database_uuid(
        call.get("property_id"),
        "property_id",
        optional=True,
    )
    campaign_id = _validate_database_uuid(
        call.get("campaign_id"),
        "campaign_id",
        optional=True,
    )

    if property_id:
        property_response = (
            client.table("properties")
            .select("id,realtor_id")
            .eq("id", property_id)
            .limit(1)
            .execute()
        )
        properties = property_response.data or []
        if not properties or properties[0].get("realtor_id") != realtor_id:
            raise ValueError("Call property does not belong to its realtor")

    if campaign_id:
        campaign_response = (
            client.table("campaigns")
            .select("id,realtor_id,property_id")
            .eq("id", campaign_id)
            .limit(1)
            .execute()
        )
        campaigns = campaign_response.data or []
        if (
            not campaigns
            or campaigns[0].get("realtor_id") != realtor_id
            or campaigns[0].get("property_id") != property_id
        ):
            raise ValueError("Call campaign attribution does not match its realtor and property")

    lead = _find_matching_lead(client, realtor_id, phone)

    extracted_fields = {
        "name": extraction["name"],
        "email": extraction["email"],
        "lead_type": extraction["lead_type"],
        "intent": extraction["intent"],
        "budget_min": extraction["budget_min"],
        "budget_max": extraction["budget_max"],
        "timeline": extraction["timeline"],
        "summary": extraction["summary"],
        "source_campaign_id": campaign_id,
        "original_property_id": property_id,
    }

    if lead is not None:
        lead_id = _validate_database_uuid(lead.get("id"), "lead_id")
        update_response = (
            client.table("leads")
            .update(_lead_updates(lead, extracted_fields, phone, score))
            .eq("id", lead_id)
            .select("id")
            .execute()
        )
        if not update_response.data:
            raise RuntimeError("Existing lead update did not update a record")
    else:
        lead_row = {
            "realtor_id": realtor_id,
            "phone": phone,
            "intent_score": score,
            "priority": priority,
        }
        lead_row.update(
            {
                field: value
                for field, value in extracted_fields.items()
                if value is not None
            }
        )
        try:
            insert_response = (
                client.table("leads")
                .insert(lead_row)
                .select("id")
                .execute()
            )
        except Exception as exc:
            if getattr(exc, "code", None) != "23505":
                raise
            lead = _find_matching_lead(client, realtor_id, phone)
            if lead is None:
                raise
            lead_id = _validate_database_uuid(lead.get("id"), "lead_id")
            update_response = (
                client.table("leads")
                .update(_lead_updates(lead, extracted_fields, phone, score))
                .eq("id", lead_id)
                .select("id")
                .execute()
            )
            if not update_response.data:
                raise RuntimeError("Concurrent lead update did not update a record")
        else:
            inserted = insert_response.data or []
            if not inserted or not inserted[0].get("id"):
                raise RuntimeError("Lead insert did not return a record ID")
            lead_id = _validate_database_uuid(inserted[0]["id"], "lead_id")

    link_response = (
        client.table("calls")
        .update({"lead_id": lead_id})
        .eq("id", call_id)
        .eq("status", "completed")
        .select("id")
        .execute()
    )
    if not link_response.data:
        raise RuntimeError("Call-to-lead link did not update a completed call")
    return lead_id
