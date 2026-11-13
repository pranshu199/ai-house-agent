"""Authentication dependencies for frontend API requests."""

import logging
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from postgrest.exceptions import APIError
from supabase_auth.errors import AuthApiError, AuthUnknownError

from app.integrations.supabase_client import (
    SupabaseConfigurationError,
    get_supabase_client,
)
from app.schemas.auth import AuthenticatedRealtor


logger = logging.getLogger(__name__)
_bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Missing or invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_authenticated_realtor(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer_scheme),
    ],
) -> AuthenticatedRealtor:
    """Validate a Supabase access token and resolve its sole realtor profile."""

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()

    try:
        supabase = get_supabase_client()
        user_response = supabase.auth.get_user(credentials.credentials)
    except AuthApiError as exc:
        if exc.status in {400, 401, 403}:
            raise _unauthorized() from None
        logger.warning("Supabase Auth validation failed with status %s", exc.status)
        raise HTTPException(
            status_code=503,
            detail="Authentication service unavailable",
        ) from None
    except (AuthUnknownError, httpx.HTTPError, SupabaseConfigurationError) as exc:
        logger.warning(
            "Supabase Auth validation unavailable (%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Authentication service unavailable",
        ) from None

    user = user_response.user if user_response else None
    user_id = getattr(user, "id", None)
    if not isinstance(user_id, str) or not user_id:
        raise _unauthorized()

    try:
        response = (
            supabase.table("realtors")
            .select("id")
            .eq("user_id", user_id)
            .limit(2)
            .execute()
        )
    except (APIError, httpx.HTTPError, SupabaseConfigurationError) as exc:
        logger.warning(
            "Realtor profile lookup unavailable (%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Realtor profile service unavailable",
        ) from None

    realtors = response.data or []
    if len(realtors) != 1:
        raise HTTPException(status_code=403, detail="Realtor profile required")

    realtor_id = realtors[0].get("id")
    if not isinstance(realtor_id, str) or not realtor_id:
        raise HTTPException(status_code=403, detail="Realtor profile required")

    return AuthenticatedRealtor(user_id=user_id, realtor_id=realtor_id)
