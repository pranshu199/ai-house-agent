"""Backend-only Supabase client and persistence operations."""

import logging
import os
import re
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
