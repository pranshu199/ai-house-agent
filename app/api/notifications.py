"""Authenticated realtor notification preference endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import get_authenticated_realtor
from app.integrations import supabase_client
from app.schemas.auth import AuthenticatedRealtor
from app.schemas.notifications import NotificationPreferencesUpdate


router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/api/notification-preferences")
def get_notification_preferences(
    realtor: AuthenticatedRealtor = Depends(get_authenticated_realtor),
):
    try:
        return supabase_client.read_notification_preferences(realtor.realtor_id)
    except Exception as exc:
        logger.warning(
            "NOTIFICATION_PREFERENCES_READ_FAILURE error_type=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Notification preferences are temporarily unavailable",
        ) from None


@router.patch("/api/notification-preferences")
def patch_notification_preferences(
    updates: NotificationPreferencesUpdate,
    realtor: AuthenticatedRealtor = Depends(get_authenticated_realtor),
):
    changes = updates.model_dump(exclude_unset=True)
    if not changes or any(not isinstance(value, bool) for value in changes.values()):
        raise HTTPException(
            status_code=422,
            detail="At least one boolean notification preference is required",
        )
    try:
        return supabase_client.save_notification_preferences(
            realtor.realtor_id,
            changes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception as exc:
        logger.warning(
            "NOTIFICATION_PREFERENCES_UPDATE_FAILURE error_type=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Notification preferences are temporarily unavailable",
        ) from None
