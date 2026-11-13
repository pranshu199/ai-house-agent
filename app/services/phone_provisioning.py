"""Database-only foundation for recoverable phone-number provisioning."""

import re
from enum import StrEnum

import httpx
from postgrest.exceptions import APIError

from app.integrations.supabase_client import get_supabase_client
from app.schemas.auth import AuthenticatedRealtor
from app.schemas.phone_numbers import PhoneNumberSearchRequest


_CANADIAN_E164_PATTERN = re.compile(r"\+1[2-9][0-9]{9}\Z")
_TWILIO_PHONE_SID_PATTERN = re.compile(r"PN[0-9A-Fa-f]{32}\Z")
_ERROR_CODE_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_IN_FLIGHT_STATUSES = (
    "pending",
    "purchase_in_progress",
    "purchase_failed",
    "purchased",
    "configuration_in_progress",
    "configuration_failed",
)


class ProvisioningStatus(StrEnum):
    PENDING = "pending"
    PURCHASE_IN_PROGRESS = "purchase_in_progress"
    PURCHASE_FAILED = "purchase_failed"
    PURCHASED = "purchased"
    CONFIGURATION_IN_PROGRESS = "configuration_in_progress"
    CONFIGURATION_FAILED = "configuration_failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


_ALLOWED_TRANSITIONS = {
    ProvisioningStatus.PENDING: {
        ProvisioningStatus.PURCHASE_IN_PROGRESS,
        ProvisioningStatus.CANCELLED,
    },
    ProvisioningStatus.PURCHASE_IN_PROGRESS: {
        ProvisioningStatus.PURCHASE_FAILED,
        ProvisioningStatus.PURCHASED,
    },
    ProvisioningStatus.PURCHASE_FAILED: {
        ProvisioningStatus.PURCHASE_IN_PROGRESS,
        ProvisioningStatus.CANCELLED,
    },
    ProvisioningStatus.PURCHASED: {
        ProvisioningStatus.CONFIGURATION_IN_PROGRESS,
    },
    ProvisioningStatus.CONFIGURATION_IN_PROGRESS: {
        ProvisioningStatus.CONFIGURATION_FAILED,
    },
    ProvisioningStatus.CONFIGURATION_FAILED: {
        ProvisioningStatus.CONFIGURATION_IN_PROGRESS,
    },
    ProvisioningStatus.COMPLETED: set(),
    ProvisioningStatus.CANCELLED: set(),
}


class ProvisioningError(RuntimeError):
    """Base error for safe provisioning service failures."""


class InvalidProvisioningRequest(ProvisioningError):
    pass


class PropertyOwnershipError(ProvisioningError):
    pass


class ActivePhoneNumberExists(ProvisioningError):
    pass


class IdempotencyConflict(ProvisioningError):
    pass


class ProvisioningInProgress(ProvisioningError):
    pass


class ProvisioningRequestNotFound(ProvisioningError):
    pass


class PhoneNumberNotFound(ProvisioningError):
    pass


class InvalidLifecycleTransition(ProvisioningError):
    pass


class ProvisioningDatabaseError(ProvisioningError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class PhoneProvisioningService:
    """Own provisioning-request persistence; provider operations are separate."""

    def __init__(self, client=None):
        self._client = client if client is not None else get_supabase_client()

    def create_request(
        self,
        *,
        principal: AuthenticatedRealtor,
        property_id: str,
        idempotency_key: str,
        requested_area_code: str | None = None,
        requested_locality: str | None = None,
    ) -> dict:
        request = self._validated_request(requested_area_code, requested_locality)
        if not isinstance(principal, AuthenticatedRealtor):
            raise InvalidProvisioningRequest(
                "Authenticated realtor context is required"
            )
        realtor_id = principal.realtor_id
        user_id = principal.user_id
        self._validate_identifiers(realtor_id, user_id, property_id)
        key = self._validate_idempotency_key(idempotency_key)

        self._validate_property_ownership(property_id, realtor_id)

        existing = self._find_idempotent_request(realtor_id, key)
        if existing is not None:
            if self._matches_request(
                existing,
                property_id,
                user_id,
                request.area_code,
                request.locality,
            ):
                return existing
            raise IdempotencyConflict(
                "Idempotency key was already used for a different request"
            )

        active_numbers = self._execute(
            self._client.table("phone_numbers")
            .select("id")
            .eq("property_id", property_id)
            .eq("status", "active")
            .limit(1)
        )
        if active_numbers.data:
            raise ActivePhoneNumberExists(
                "Property already has an active phone number"
            )

        in_flight = self._find_in_flight_request(property_id)
        if in_flight is not None:
            raise ProvisioningInProgress(
                "Property already has an unresolved provisioning request"
            )

        payload = {
            "realtor_id": realtor_id,
            "property_id": property_id,
            "requested_by": user_id,
            "idempotency_key": key,
            "status": ProvisioningStatus.PENDING.value,
            "requested_area_code": request.area_code,
            "requested_locality": request.locality,
            "attempts": 0,
        }
        try:
            response = self._execute(
                self._client.table("phone_number_provisioning")
                .insert(payload)
                .select("*")
            )
        except ProvisioningDatabaseError as exc:
            if exc.code != "23505":
                raise
            existing = self._find_idempotent_request(realtor_id, key)
            if existing is not None and self._matches_request(
                existing,
                property_id,
                user_id,
                request.area_code,
                request.locality,
            ):
                return existing
            if self._find_in_flight_request(property_id) is not None:
                raise ProvisioningInProgress(
                    "Property already has an unresolved provisioning request"
                ) from None
            raise IdempotencyConflict(
                "Provisioning request conflicts with an existing request"
            ) from None

        records = response.data or []
        if len(records) != 1:
            raise ProvisioningDatabaseError(
                "Provisioning request insert returned an invalid result"
            )
        return records[0]

    def transition_request(
        self,
        *,
        principal: AuthenticatedRealtor,
        request_id: str,
        target_status: ProvisioningStatus,
        last_error_code: str | None = None,
    ) -> dict:
        self._validate_principal(principal)
        return self._transition(
            request_id=request_id,
            realtor_id=principal.realtor_id,
            target_status=target_status,
            last_error_code=last_error_code,
        )

    def start_purchase(
        self,
        *,
        principal: AuthenticatedRealtor,
        request_id: str,
        phone_number: str,
    ) -> dict:
        self._validate_principal(principal)
        if (
            not isinstance(phone_number, str)
            or not _CANADIAN_E164_PATTERN.fullmatch(phone_number)
        ):
            raise InvalidProvisioningRequest(
                "Selected number must be a Canadian E.164 number"
            )
        return self._transition(
            request_id=request_id,
            realtor_id=principal.realtor_id,
            target_status=ProvisioningStatus.PURCHASE_IN_PROGRESS,
            extra_updates={"phone_number": phone_number},
        )

    def get_request(
        self,
        *,
        principal: AuthenticatedRealtor,
        request_id: str,
    ) -> dict:
        self._validate_principal(principal)
        return self._find_request(request_id, principal.realtor_id)

    def get_phone_number(
        self,
        *,
        principal: AuthenticatedRealtor,
        phone_number_id: str,
    ) -> dict:
        self._validate_principal(principal)
        self._validate_identifiers(phone_number_id)
        response = self._execute(
            self._client.table("phone_numbers")
            .select("id,realtor_id,property_id,phone_number,twilio_sid,status")
            .eq("id", phone_number_id)
            .eq("realtor_id", principal.realtor_id)
            .limit(1)
        )
        records = response.data or []
        if len(records) != 1:
            raise PhoneNumberNotFound("Phone number was not found")
        return records[0]

    def deactivate_phone_number(
        self,
        *,
        principal: AuthenticatedRealtor,
        phone_number_id: str,
    ) -> dict:
        phone = self.get_phone_number(
            principal=principal,
            phone_number_id=phone_number_id,
        )
        if phone.get("status") == "inactive":
            return phone
        if phone.get("status") != "active":
            raise InvalidLifecycleTransition(
                "Only an active phone number can be deactivated"
            )

        response = self._execute(
            self._client.table("phone_numbers")
            .update({"status": "inactive"})
            .eq("id", phone_number_id)
            .eq("realtor_id", principal.realtor_id)
            .eq("status", "active")
            .select("id,realtor_id,property_id,phone_number,twilio_sid,status")
        )
        records = response.data or []
        if len(records) == 1:
            return records[0]

        phone = self.get_phone_number(
            principal=principal,
            phone_number_id=phone_number_id,
        )
        if phone.get("status") == "inactive":
            return phone
        raise InvalidLifecycleTransition(
            "Phone-number status changed concurrently"
        )

    def record_purchase_result(
        self,
        *,
        principal: AuthenticatedRealtor,
        request_id: str,
        country_code: str,
        phone_number: str,
        twilio_sid: str,
    ) -> dict:
        self._validate_principal(principal)
        if (
            country_code != "CA"
            or not isinstance(phone_number, str)
            or not _CANADIAN_E164_PATTERN.fullmatch(phone_number)
        ):
            raise InvalidProvisioningRequest(
                "Purchased number must be a Canadian E.164 number"
            )
        if (
            not isinstance(twilio_sid, str)
            or not _TWILIO_PHONE_SID_PATTERN.fullmatch(twilio_sid)
        ):
            raise InvalidProvisioningRequest("Twilio SID is invalid")

        return self._transition(
            request_id=request_id,
            realtor_id=principal.realtor_id,
            target_status=ProvisioningStatus.PURCHASED,
            extra_updates={
                "phone_number": phone_number,
                "twilio_sid": twilio_sid,
            },
        )

    def complete_assignment(
        self,
        *,
        principal: AuthenticatedRealtor,
        request_id: str,
    ) -> dict:
        self._validate_principal(principal)
        record = self._find_request(request_id, principal.realtor_id)
        try:
            status = ProvisioningStatus(record.get("status"))
        except ValueError:
            raise ProvisioningDatabaseError(
                "Provisioning request has an unknown lifecycle state"
            ) from None

        if status not in {
            ProvisioningStatus.CONFIGURATION_IN_PROGRESS,
            ProvisioningStatus.COMPLETED,
        }:
            raise InvalidLifecycleTransition(
                "Phone-number assignment requires configuration to be in progress"
            )
        if not record.get("phone_number") or not record.get("twilio_sid"):
            raise InvalidLifecycleTransition(
                "A purchased number is required before assignment"
            )

        response = self._execute(
            self._client.rpc(
                "complete_phone_number_provisioning",
                {
                    "p_request_id": request_id,
                    "p_realtor_id": principal.realtor_id,
                },
            )
        )
        records = response.data or []
        if isinstance(records, dict):
            return records
        if len(records) != 1:
            raise ProvisioningDatabaseError(
                "Phone-number assignment returned an invalid result"
            )
        return records[0]

    def _transition(
        self,
        *,
        request_id: str,
        realtor_id: str,
        target_status: ProvisioningStatus,
        last_error_code: str | None = None,
        extra_updates: dict | None = None,
    ) -> dict:
        self._validate_identifiers(realtor_id, request_id)
        try:
            target_status = ProvisioningStatus(target_status)
        except (TypeError, ValueError):
            raise InvalidProvisioningRequest(
                "Unknown provisioning lifecycle state"
            ) from None
        record = self._find_request(request_id, realtor_id)
        try:
            current_status = ProvisioningStatus(record.get("status"))
        except ValueError:
            raise ProvisioningDatabaseError(
                "Provisioning request has an unknown lifecycle state"
            ) from None

        if target_status not in _ALLOWED_TRANSITIONS[current_status]:
            raise InvalidLifecycleTransition(
                f"Cannot transition from {current_status.value} to {target_status.value}"
            )

        selected_number = (extra_updates or {}).get("phone_number")
        if (
            selected_number is not None
            and record.get("phone_number") is not None
            and selected_number != record.get("phone_number")
        ):
            raise InvalidLifecycleTransition(
                "A provisioning request cannot switch its selected number"
            )

        updates = {"status": target_status.value}
        if target_status in {
            ProvisioningStatus.PURCHASE_FAILED,
            ProvisioningStatus.CONFIGURATION_FAILED,
        }:
            if (
                not isinstance(last_error_code, str)
                or not _ERROR_CODE_PATTERN.fullmatch(last_error_code)
            ):
                raise InvalidProvisioningRequest("A safe error code is required")
            updates["last_error_code"] = last_error_code
        elif target_status in {
            ProvisioningStatus.PURCHASED,
            ProvisioningStatus.PURCHASE_IN_PROGRESS,
            ProvisioningStatus.CONFIGURATION_IN_PROGRESS,
        }:
            updates["last_error_code"] = None

        if target_status == ProvisioningStatus.PURCHASE_IN_PROGRESS:
            updates["attempts"] = int(record.get("attempts", 0)) + 1
        updates.update(extra_updates or {})

        response = self._execute(
            self._client.table("phone_number_provisioning")
            .update(updates)
            .eq("id", request_id)
            .eq("realtor_id", realtor_id)
            .eq("status", current_status.value)
            .select("*")
        )
        records = response.data or []
        if len(records) != 1:
            raise InvalidLifecycleTransition(
                "Provisioning request changed concurrently"
            )
        return records[0]

    def _validate_property_ownership(
        self,
        property_id: str,
        realtor_id: str,
    ) -> None:
        response = self._execute(
            self._client.table("properties")
            .select("id,realtor_id")
            .eq("id", property_id)
            .eq("realtor_id", realtor_id)
            .limit(1)
        )
        if len(response.data or []) != 1:
            raise PropertyOwnershipError(
                "Property does not belong to the authenticated realtor"
            )

    def _find_idempotent_request(
        self,
        realtor_id: str,
        idempotency_key: str,
    ) -> dict | None:
        response = self._execute(
            self._client.table("phone_number_provisioning")
            .select("*")
            .eq("realtor_id", realtor_id)
            .eq("idempotency_key", idempotency_key)
            .limit(2)
        )
        records = response.data or []
        if len(records) > 1:
            raise ProvisioningDatabaseError(
                "Duplicate idempotency records violate a database invariant"
            )
        return records[0] if records else None

    def _find_in_flight_request(self, property_id: str) -> dict | None:
        response = self._execute(
            self._client.table("phone_number_provisioning")
            .select("id,status")
            .eq("property_id", property_id)
            .in_("status", _IN_FLIGHT_STATUSES)
            .limit(1)
        )
        records = response.data or []
        return records[0] if records else None

    def _find_request(self, request_id: str, realtor_id: str) -> dict:
        response = self._execute(
            self._client.table("phone_number_provisioning")
            .select("*")
            .eq("id", request_id)
            .eq("realtor_id", realtor_id)
            .limit(1)
        )
        records = response.data or []
        if len(records) != 1:
            raise ProvisioningRequestNotFound("Provisioning request not found")
        return records[0]

    @staticmethod
    def _matches_request(
        record: dict,
        property_id: str,
        user_id: str,
        area_code: str | None,
        locality: str | None,
    ) -> bool:
        return (
            record.get("property_id") == property_id
            and record.get("requested_by") == user_id
            and record.get("requested_area_code") == area_code
            and record.get("requested_locality") == locality
        )

    @staticmethod
    def _validate_identifiers(*identifiers: str) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in identifiers):
            raise InvalidProvisioningRequest("Provisioning identifiers are required")

    @classmethod
    def _validate_principal(cls, principal: AuthenticatedRealtor) -> None:
        if not isinstance(principal, AuthenticatedRealtor):
            raise InvalidProvisioningRequest(
                "Authenticated realtor context is required"
            )
        cls._validate_identifiers(principal.realtor_id, principal.user_id)

    @staticmethod
    def _validate_idempotency_key(value: str) -> str:
        if not isinstance(value, str):
            raise InvalidProvisioningRequest("Idempotency key is required")
        value = value.strip()
        if not value or len(value) > 255 or "\x00" in value:
            raise InvalidProvisioningRequest("Idempotency key is invalid")
        return value

    @staticmethod
    def _validated_request(
        area_code: str | None,
        locality: str | None,
    ) -> PhoneNumberSearchRequest:
        try:
            return PhoneNumberSearchRequest(
                area_code=area_code,
                locality=locality,
                limit=1,
            )
        except ValueError:
            raise InvalidProvisioningRequest(
                "Requested number filters are invalid"
            ) from None

    @staticmethod
    def _execute(query):
        try:
            return query.execute()
        except APIError as exc:
            raise ProvisioningDatabaseError(
                "Provisioning database operation failed",
                code=getattr(exc, "code", None),
            ) from None
        except httpx.HTTPError:
            raise ProvisioningDatabaseError(
                "Provisioning database is unavailable"
            ) from None
