"""Phone-number search endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from requests.exceptions import RequestException
from starlette.concurrency import run_in_threadpool
from twilio.base.exceptions import TwilioException

from app.api.auth import AuthenticatedRealtor, get_authenticated_realtor
from app.integrations import twilio_client
from app.integrations.twilio_client import (
    TwilioConfigurationError,
    TwilioInventoryLookupError,
)
from app.schemas.phone_numbers import (
    PhoneNumberPurchaseRequest,
    PhoneNumberProvisionRequest,
    PhoneNumberReleaseRequest,
    PhoneNumberSearchRequest,
)
from app.services.phone_provisioning import (
    ActivePhoneNumberExists,
    IdempotencyConflict,
    InvalidLifecycleTransition,
    InvalidProvisioningRequest,
    PhoneNumberNotFound,
    PhoneProvisioningService,
    PropertyOwnershipError,
    ProvisioningDatabaseError,
    ProvisioningError,
    ProvisioningInProgress,
    ProvisioningRequestNotFound,
    ProvisioningStatus,
)
from app.services.phone_search import (
    enforce_search_rate_limit,
    search_canadian_local_numbers,
)


router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/phone-numbers/search")
async def search_phone_numbers(
    search: PhoneNumberSearchRequest,
    realtor: AuthenticatedRealtor = Depends(get_authenticated_realtor),
):
    enforce_search_rate_limit(realtor.realtor_id)

    try:
        numbers = await run_in_threadpool(search_canadian_local_numbers, search)
    except TwilioConfigurationError:
        logger.error("Twilio phone-number search is not configured")
        raise HTTPException(
            status_code=503,
            detail="Phone number search is unavailable",
        ) from None
    except (TwilioException, RequestException) as exc:
        logger.warning(
            "Twilio phone-number search failed (%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail="Phone number search is temporarily unavailable",
        ) from None

    return {"numbers": numbers, "count": len(numbers)}


def _raise_provisioning_error(error: ProvisioningError) -> None:
    if isinstance(error, (ProvisioningRequestNotFound, PhoneNumberNotFound)):
        raise HTTPException(status_code=404, detail="Phone-number request not found")
    if isinstance(error, PropertyOwnershipError):
        raise HTTPException(status_code=404, detail="Property not found")
    if isinstance(error, InvalidProvisioningRequest):
        raise HTTPException(status_code=422, detail=str(error))
    if isinstance(
        error,
        (
            ActivePhoneNumberExists,
            IdempotencyConflict,
            InvalidLifecycleTransition,
            ProvisioningInProgress,
        ),
    ):
        raise HTTPException(status_code=409, detail=str(error))
    if isinstance(error, ProvisioningDatabaseError):
        logger.warning("Phone-number provisioning database operation failed")
        raise HTTPException(
            status_code=503,
            detail="Phone-number provisioning is temporarily unavailable",
        ) from None
    raise HTTPException(status_code=500, detail="Phone-number request failed") from None


def _service_call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except ProvisioningError as exc:
        _raise_provisioning_error(exc)


def _mark_failed(
    service: PhoneProvisioningService,
    principal: AuthenticatedRealtor,
    request_id: str,
    target_status: ProvisioningStatus,
    error_code: str,
) -> None:
    try:
        service.transition_request(
            principal=principal,
            request_id=request_id,
            target_status=target_status,
            last_error_code=error_code,
        )
    except ProvisioningError:
        logger.warning("Unable to persist phone-number failure state")


def _configure_and_complete(
    service: PhoneProvisioningService,
    principal: AuthenticatedRealtor,
    request_id: str,
) -> dict:
    request = _service_call(
        service.get_request,
        principal=principal,
        request_id=request_id,
    )
    try:
        status = ProvisioningStatus(request.get("status"))
    except ValueError:
        raise HTTPException(status_code=503, detail="Unknown provisioning state") from None

    if status == ProvisioningStatus.COMPLETED:
        assignment = _service_call(
            service.complete_assignment,
            principal=principal,
            request_id=request_id,
        )
        return {
            "request_id": request_id,
            "phone_number": request.get("phone_number"),
            "status": status.value,
            "assigned_phone_number_id": assignment.get("assigned_phone_number_id"),
        }

    if status in {
        ProvisioningStatus.PURCHASED,
        ProvisioningStatus.CONFIGURATION_FAILED,
    }:
        _service_call(
            service.transition_request,
            principal=principal,
            request_id=request_id,
            target_status=ProvisioningStatus.CONFIGURATION_IN_PROGRESS,
        )
    elif status != ProvisioningStatus.CONFIGURATION_IN_PROGRESS:
        raise HTTPException(
            status_code=409,
            detail="Provisioning request is not ready for voice configuration",
        )

    if not request.get("twilio_sid"):
        raise HTTPException(
            status_code=409,
            detail="Purchased number is missing; reconcile the purchase first",
        )

    try:
        twilio_client.configure_twilio_voice(request["twilio_sid"])
    except TwilioConfigurationError:
        _mark_failed(
            service,
            principal,
            request_id,
            ProvisioningStatus.CONFIGURATION_FAILED,
            "voice_config_unavailable",
        )
        raise HTTPException(
            status_code=503,
            detail="Phone-number voice configuration is unavailable",
        ) from None
    except (TwilioException, RequestException) as exc:
        logger.warning(
            "Twilio voice configuration failed (%s)",
            type(exc).__name__,
        )
        _mark_failed(
            service,
            principal,
            request_id,
            ProvisioningStatus.CONFIGURATION_FAILED,
            "voice_config_failed",
        )
        raise HTTPException(
            status_code=502,
            detail="Purchased number needs voice-configuration retry",
        ) from None

    assignment = _service_call(
        service.complete_assignment,
        principal=principal,
        request_id=request_id,
    )
    return {
        "request_id": request_id,
        "phone_number": request.get("phone_number"),
        "status": ProvisioningStatus.COMPLETED.value,
        "assigned_phone_number_id": assignment.get("assigned_phone_number_id"),
    }


@router.post("/api/phone-numbers/provisioning")
def create_phone_number_provisioning_request(
    request: PhoneNumberProvisionRequest,
    principal: AuthenticatedRealtor = Depends(get_authenticated_realtor),
):
    service = PhoneProvisioningService()
    try:
        record = service.create_request(
            principal=principal,
            property_id=request.property_id,
            idempotency_key=request.idempotency_key,
            requested_area_code=request.area_code,
            requested_locality=request.locality,
        )
    except ProvisioningError as exc:
        _raise_provisioning_error(exc)
    return {"request": record}


@router.post("/api/phone-numbers/provisioning/{request_id}/purchase")
def purchase_phone_number(
    request_id: str,
    request: PhoneNumberPurchaseRequest | None = None,
    principal: AuthenticatedRealtor = Depends(get_authenticated_realtor),
):
    service = PhoneProvisioningService()
    provisioning = _service_call(
        service.get_request,
        principal=principal,
        request_id=request_id,
    )
    try:
        status = ProvisioningStatus(provisioning.get("status"))
    except ValueError:
        raise HTTPException(status_code=503, detail="Unknown provisioning state") from None

    if status in {
        ProvisioningStatus.PURCHASED,
        ProvisioningStatus.CONFIGURATION_IN_PROGRESS,
        ProvisioningStatus.CONFIGURATION_FAILED,
        ProvisioningStatus.COMPLETED,
    }:
        return _configure_and_complete(service, principal, request_id)
    if status == ProvisioningStatus.PURCHASE_IN_PROGRESS:
        raise HTTPException(
            status_code=409,
            detail="Purchase outcome is unresolved; reconcile before retrying",
        )
    if status not in {
        ProvisioningStatus.PENDING,
        ProvisioningStatus.PURCHASE_FAILED,
    }:
        raise HTTPException(status_code=409, detail="Provisioning request cannot be purchased")
    if request is None:
        raise HTTPException(status_code=422, detail="A selected phone number is required")

    _service_call(
        service.start_purchase,
        principal=principal,
        request_id=request_id,
        phone_number=request.phone_number,
    )
    try:
        purchased_number = twilio_client.purchase_canadian_local_number(
            request.phone_number,
            request_id,
        )
    except (ValueError, TwilioConfigurationError) as exc:
        error_code = (
            "number_unavailable"
            if isinstance(exc, ValueError)
            else "twilio_not_configured"
        )
        _mark_failed(
            service,
            principal,
            request_id,
            ProvisioningStatus.PURCHASE_FAILED,
            error_code,
        )
        status_code = 422 if isinstance(exc, ValueError) else 503
        detail = (
            "Selected number is not available for Canadian voice"
            if status_code == 422
            else "Phone-number purchasing is unavailable"
        )
        raise HTTPException(status_code=status_code, detail=detail) from None
    except TwilioInventoryLookupError:
        logger.warning("Canadian phone-number inventory lookup failed")
        _mark_failed(
            service,
            principal,
            request_id,
            ProvisioningStatus.PURCHASE_FAILED,
            "inventory_lookup_failed",
        )
        raise HTTPException(
            status_code=502,
            detail="Canadian phone-number inventory is temporarily unavailable",
        ) from None
    except (TwilioException, RequestException) as exc:
        logger.warning(
            "Twilio purchase outcome is unresolved (%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail="Purchase outcome is unresolved; reconcile before retrying",
        ) from None

    returned_number = getattr(purchased_number, "phone_number", None)
    twilio_sid = getattr(purchased_number, "sid", None)
    country_code = getattr(purchased_number, "iso_country", None)
    if returned_number != request.phone_number or not twilio_sid:
        raise HTTPException(
            status_code=502,
            detail="Purchased number needs reconciliation before assignment",
        )

    _service_call(
        service.record_purchase_result,
        principal=principal,
        request_id=request_id,
        country_code=country_code,
        phone_number=returned_number,
        twilio_sid=twilio_sid,
    )
    return _configure_and_complete(service, principal, request_id)


@router.post("/api/phone-numbers/provisioning/{request_id}/reconcile")
def reconcile_phone_number_purchase(
    request_id: str,
    principal: AuthenticatedRealtor = Depends(get_authenticated_realtor),
):
    service = PhoneProvisioningService()
    provisioning = _service_call(
        service.get_request,
        principal=principal,
        request_id=request_id,
    )
    if provisioning.get("status") == ProvisioningStatus.PURCHASE_IN_PROGRESS.value:
        phone_number = provisioning.get("phone_number")
        if not phone_number:
            raise HTTPException(
                status_code=409,
                detail="Purchase target is missing; no new purchase was made",
            )
        try:
            purchased_number = twilio_client.find_provisioned_canadian_number(
                phone_number,
                request_id,
            )
        except TwilioConfigurationError:
            raise HTTPException(
                status_code=503,
                detail="Phone-number reconciliation is unavailable",
            ) from None
        except (TwilioException, RequestException) as exc:
            logger.warning(
                "Twilio purchase reconciliation failed (%s)",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=502,
                detail="Phone-number reconciliation is temporarily unavailable",
            ) from None

        if purchased_number is None:
            raise HTTPException(
                status_code=409,
                detail="No matching Twilio purchase is visible; no new purchase was made",
            )
        _service_call(
            service.record_purchase_result,
            principal=principal,
            request_id=request_id,
            country_code=getattr(purchased_number, "iso_country", None),
            phone_number=getattr(purchased_number, "phone_number", None),
            twilio_sid=getattr(purchased_number, "sid", None),
        )
    return _configure_and_complete(service, principal, request_id)


@router.post("/api/phone-numbers/{phone_number_id}/deactivate")
def deactivate_phone_number(
    phone_number_id: str,
    principal: AuthenticatedRealtor = Depends(get_authenticated_realtor),
):
    service = PhoneProvisioningService()
    phone = _service_call(
        service.deactivate_phone_number,
        principal=principal,
        phone_number_id=phone_number_id,
    )
    return {"phone_number_id": phone["id"], "status": phone["status"]}


@router.post("/api/phone-numbers/{phone_number_id}/release")
def release_phone_number(
    phone_number_id: str,
    request: PhoneNumberReleaseRequest | None = None,
    principal: AuthenticatedRealtor = Depends(get_authenticated_realtor),
):
    if request is None or request.confirm_release is not True:
        raise HTTPException(status_code=400, detail="confirm_release must be true")

    service = PhoneProvisioningService()
    phone = _service_call(
        service.get_phone_number,
        principal=principal,
        phone_number_id=phone_number_id,
    )
    if phone.get("status") != "inactive":
        raise HTTPException(
            status_code=409,
            detail="Deactivate the phone number before releasing it",
        )
    if not phone.get("twilio_sid"):
        raise HTTPException(status_code=409, detail="Twilio number SID is missing")

    try:
        deleted = twilio_client.release_twilio_number(phone["twilio_sid"])
    except TwilioConfigurationError:
        raise HTTPException(
            status_code=503,
            detail="Phone-number release is unavailable",
        ) from None
    except (TwilioException, RequestException) as exc:
        logger.warning(
            "Twilio number release failed (%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail="Phone-number release is temporarily unavailable",
        ) from None

    return {
        "phone_number_id": phone_number_id,
        "released": True,
        "already_released": not deleted,
    }
