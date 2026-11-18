"""Property image storage and database operations."""

import io
import logging
import os
import re
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
from postgrest.exceptions import APIError
from supabase.lib.client_options import ClientOptions

from app.integrations.supabase_client import get_supabase_client


logger = logging.getLogger(__name__)

# Configuration
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_IMAGES_PER_PROPERTY = 10
STORAGE_BUCKET_NAME = "property-images"
SIGNED_URL_EXPIRY_SECONDS = 3600  # 1 hour


class PropertyImageError(Exception):
    """Base exception for property image operations."""


class InvalidFileError(PropertyImageError):
    """Raised when file validation fails."""


class StorageError(PropertyImageError):
    """Raised when Supabase Storage operation fails."""


class DatabaseError(PropertyImageError):
    """Raised when database operation fails."""


class ImageLimitExceededError(PropertyImageError):
    """Raised when image limit per property is exceeded."""


def validate_file(
    filename: str,
    file_data: bytes,
    mime_type: str,
) -> tuple[str, str]:
    """
    Validate uploaded file and return normalized extension and mime type.

    Args:
        filename: Original filename from upload
        file_data: Raw file bytes
        mime_type: Content-Type header value

    Returns:
        Tuple of (extension, mime_type) normalized for storage

    Raises:
        InvalidFileError: If file fails validation
    """
    # Validate MIME type
    if mime_type not in ALLOWED_MIME_TYPES:
        raise InvalidFileError(
            f"File type '{mime_type}' not allowed. "
            f"Allowed types: {', '.join(sorted(ALLOWED_MIME_TYPES))}"
        )

    # Validate file size
    if len(file_data) > MAX_FILE_SIZE_BYTES:
        raise InvalidFileError(
            f"File size {len(file_data)} bytes exceeds maximum {MAX_FILE_SIZE_BYTES} bytes (5 MB)"
        )

    # Extract and validate extension
    path = Path(filename)
    extension = path.suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise InvalidFileError(
            f"File extension '{extension}' not allowed. "
            f"Allowed extensions: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    # Map MIME type to consistent extension
    mime_to_ext = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }
    normalized_ext = mime_to_ext.get(mime_type, extension)

    return normalized_ext, mime_type


def upload_image_to_storage(
    realtor_id: str,
    property_id: str,
    file_data: bytes,
    extension: str,
) -> str:
    """
    Upload image to Supabase Storage and return storage path.

    Args:
        realtor_id: Realtor UUID
        property_id: Property UUID
        file_data: Raw image bytes
        extension: File extension (e.g., ".jpg")

    Returns:
        Storage path in format: {realtor_id}/{property_id}/{uuid}.{extension}

    Raises:
        StorageError: If upload fails
    """
    try:
        supabase = get_supabase_client()
        image_uuid = str(uuid.uuid4())
        storage_path = f"{realtor_id}/{property_id}/{image_uuid}{extension}"

        supabase.storage.from_(STORAGE_BUCKET_NAME).upload(
            storage_path,
            file_data,
            {
                "content-type": "auto",
                "upsert": False,
            },
        )

        logger.info(
            "Uploaded image to storage",
            extra={
                "realtor_id": realtor_id,
                "property_id": property_id,
                "storage_path": storage_path,
            },
        )
        return storage_path

    except Exception as exc:
        logger.warning(
            "Storage upload failed: %s",
            str(exc),
            extra={
                "realtor_id": realtor_id,
                "property_id": property_id,
            },
        )
        raise StorageError(f"Failed to upload image to storage: {exc}") from exc


def insert_image_metadata(
    realtor_id: str,
    property_id: str,
    storage_path: str,
    is_primary: bool = False,
    sort_order: int = 0,
) -> dict:
    """
    Insert image metadata into database.

    Args:
        realtor_id: Realtor UUID
        property_id: Property UUID
        storage_path: Storage path from upload
        is_primary: Whether this should be the primary image
        sort_order: Sort order for gallery

    Returns:
        Image record from database

    Raises:
        DatabaseError: If insert fails
        ImageLimitExceededError: If property already has 10 images
    """
    try:
        supabase = get_supabase_client()

        # Check current image count
        count_response = (
            supabase.table("property_images")
            .select("id")
            .eq("property_id", property_id)
            .execute()
        )
        current_count = len(count_response.data or [])
        if current_count >= MAX_IMAGES_PER_PROPERTY:
            raise ImageLimitExceededError(
                f"Property already has {MAX_IMAGES_PER_PROPERTY} images"
            )

        # Insert new image
        insert_response = (
            supabase.table("property_images")
            .insert({
                "property_id": property_id,
                "realtor_id": realtor_id,
                "storage_path": storage_path,
                "is_primary": is_primary,
                "sort_order": sort_order,
            })
            .execute()
        )

        images = insert_response.data or []
        if not images:
            raise DatabaseError("Failed to insert image metadata")

        return images[0]

    except ImageLimitExceededError:
        raise
    except APIError as exc:
        logger.warning(
            "Database insert failed: %s",
            str(exc),
            extra={
                "realtor_id": realtor_id,
                "property_id": property_id,
                "storage_path": storage_path,
            },
        )
        raise DatabaseError(f"Failed to insert image metadata: {exc}") from exc
    except (httpx.HTTPError, Exception) as exc:
        logger.warning(
            "Database error: %s",
            type(exc).__name__,
            extra={
                "realtor_id": realtor_id,
                "property_id": property_id,
            },
        )
        raise DatabaseError(f"Database error: {exc}") from exc


def set_primary_image(
    property_id: str,
    realtor_id: str,
    image_id: str,
) -> dict:
    """
    Atomically set an image as primary (remove primary from others).

    Args:
        property_id: Property UUID
        realtor_id: Realtor UUID (for authorization)
        image_id: Image ID to make primary

    Returns:
        Updated image record

    Raises:
        DatabaseError: If operation fails
    """
    try:
        supabase = get_supabase_client()

        # Verify image belongs to this property and realtor
        image_response = (
            supabase.table("property_images")
            .select("id,property_id,realtor_id")
            .eq("id", image_id)
            .eq("property_id", property_id)
            .eq("realtor_id", realtor_id)
            .limit(1)
            .execute()
        )

        if not (image_response.data or []):
            raise DatabaseError("Image not found or access denied")

        # Clear primary from all images for this property
        supabase.table("property_images").update(
            {"is_primary": False}
        ).eq("property_id", property_id).execute()

        # Set new primary
        update_response = (
            supabase.table("property_images")
            .update({"is_primary": True})
            .eq("id", image_id)
            .execute()
        )

        images = update_response.data or []
        if not images:
            raise DatabaseError("Failed to update primary image")

        return images[0]

    except DatabaseError:
        raise
    except APIError as exc:
        logger.warning(
            "Set primary image failed: %s",
            str(exc),
            extra={
                "image_id": image_id,
                "property_id": property_id,
            },
        )
        raise DatabaseError(f"Failed to set primary image: {exc}") from exc
    except (httpx.HTTPError, Exception) as exc:
        logger.warning(
            "Set primary image error: %s",
            type(exc).__name__,
        )
        raise DatabaseError(f"Database error: {exc}") from exc


def auto_promote_primary_image(
    property_id: str,
    realtor_id: str,
) -> dict | None:
    """
    Promote the oldest remaining image to primary (used when primary is deleted).

    Args:
        property_id: Property UUID
        realtor_id: Realtor UUID

    Returns:
        New primary image record, or None if no images remain

    Raises:
        DatabaseError: If operation fails
    """
    try:
        supabase = get_supabase_client()

        # Get the oldest remaining image
        response = (
            supabase.table("property_images")
            .select("id")
            .eq("property_id", property_id)
            .eq("realtor_id", realtor_id)
            .order("created_at", desc=False)
            .limit(1)
            .execute()
        )

        images = response.data or []
        if not images:
            return None

        oldest_id = images[0]["id"]
        return set_primary_image(property_id, realtor_id, oldest_id)

    except DatabaseError:
        raise
    except (APIError, httpx.HTTPError, Exception) as exc:
        logger.warning(
            "Auto-promote primary failed: %s",
            type(exc).__name__,
        )
        raise DatabaseError(f"Failed to auto-promote primary image: {exc}") from exc


def delete_image(
    image_id: str,
    property_id: str,
    realtor_id: str,
) -> bool:
    """
    Delete an image from storage and database.

    Args:
        image_id: Image ID
        property_id: Property UUID
        realtor_id: Realtor UUID (for authorization)

    Returns:
        True if deleted successfully

    Raises:
        DatabaseError: If operation fails
    """
    try:
        supabase = get_supabase_client()

        # Get image metadata
        image_response = (
            supabase.table("property_images")
            .select("storage_path,is_primary")
            .eq("id", image_id)
            .eq("property_id", property_id)
            .eq("realtor_id", realtor_id)
            .limit(1)
            .execute()
        )

        images = image_response.data or []
        if not images:
            raise DatabaseError("Image not found or access denied")

        image = images[0]
        storage_path = image["storage_path"]
        was_primary = image["is_primary"]

        # Delete from database first
        delete_response = (
            supabase.table("property_images")
            .delete()
            .eq("id", image_id)
            .execute()
        )

        if not delete_response.data:
            raise DatabaseError("Failed to delete image metadata")

        # Delete from storage
        try:
            supabase.storage.from_(STORAGE_BUCKET_NAME).remove([storage_path])
            logger.info(
                "Deleted image from storage",
                extra={
                    "storage_path": storage_path,
                    "property_id": property_id,
                },
            )
        except Exception as exc:
            logger.warning(
                "Failed to delete image from storage: %s",
                str(exc),
                extra={
                    "storage_path": storage_path,
                    "property_id": property_id,
                },
            )

        # If deleted image was primary, promote another
        if was_primary:
            try:
                auto_promote_primary_image(property_id, realtor_id)
            except DatabaseError as exc:
                logger.warning(
                    "Failed to auto-promote after deletion: %s",
                    str(exc),
                )

        return True

    except DatabaseError:
        raise
    except (APIError, httpx.HTTPError, Exception) as exc:
        logger.warning(
            "Delete image error: %s",
            type(exc).__name__,
        )
        raise DatabaseError(f"Failed to delete image: {exc}") from exc


def get_image_signed_url(storage_path: str) -> str:
    """
    Generate a signed URL for a property image (valid for 1 hour).

    Args:
        storage_path: Storage path from database

    Returns:
        Signed URL for accessing the image

    Raises:
        StorageError: If signing fails
    """
    try:
        supabase = get_supabase_client()
        response = supabase.storage.from_(STORAGE_BUCKET_NAME).create_signed_url(
            storage_path,
            SIGNED_URL_EXPIRY_SECONDS,
        )

        # First check whether the response is a dictionary
        if not isinstance(response, dict):
            raise StorageError("Invalid response from storage service")

        # Then extract signedURL
        signed_url = response.get("signedURL")
        # If the key is missing, the value is empty, or the value isn't a string
        if not signed_url or not isinstance(signed_url, str):
            raise StorageError("Missing or invalid signedURL in storage response")

        # Return the valid signed URL string
        return signed_url
    except StorageError:
        raise
    except Exception as exc:
        logger.warning(
            "Failed to create signed URL: %s",
            type(exc).__name__,
        )
        raise StorageError(f"Failed to create signed URL: {exc}") from exc


def reorder_images(
    property_id: str,
    realtor_id: str,
    image_orders: list[tuple[str, int]],
) -> list[dict]:
    """
    Update sort_order for multiple images atomically.

    Args:
        property_id: Property UUID
        realtor_id: Realtor UUID
        image_orders: List of (image_id, sort_order) tuples

    Returns:
        Updated image records

    Raises:
        DatabaseError: If operation fails
    """
    try:
        supabase = get_supabase_client()

        # Verify all images belong to this property and realtor
        image_ids = [img_id for img_id, _ in image_orders]
        verify_response = (
            supabase.table("property_images")
            .select("id")
            .eq("property_id", property_id)
            .eq("realtor_id", realtor_id)
            .in_("id", image_ids)
            .execute()
        )

        if len(verify_response.data or []) != len(image_ids):
            raise DatabaseError("One or more images not found or access denied")

        # Update each image's sort_order
        for image_id, sort_order in image_orders:
            supabase.table("property_images").update(
                {"sort_order": sort_order}
            ).eq("id", image_id).execute()

        # Return updated images
        response = (
            supabase.table("property_images")
            .select("*")
            .eq("property_id", property_id)
            .order("sort_order", desc=False)
            .execute()
        )

        return response.data or []

    except DatabaseError:
        raise
    except APIError as exc:
        logger.warning(
            "Reorder images failed: %s",
            str(exc),
        )
        raise DatabaseError(f"Failed to reorder images: {exc}") from exc
    except (httpx.HTTPError, Exception) as exc:
        logger.warning(
            "Reorder error: %s",
            type(exc).__name__,
        )
        raise DatabaseError(f"Database error: {exc}") from exc


def get_property_images(
    property_id: str,
    realtor_id: str,
) -> list[dict]:
    """
    Retrieve all images for a property, ordered by sort_order.

    Args:
        property_id: Property UUID
        realtor_id: Realtor UUID

    Returns:
        List of image records

    Raises:
        DatabaseError: If query fails
    """
    try:
        supabase = get_supabase_client()

        response = (
            supabase.table("property_images")
            .select("*")
            .eq("property_id", property_id)
            .eq("realtor_id", realtor_id)
            .order("sort_order", desc=False)
            .execute()
        )

        return response.data or []

    except APIError as exc:
        logger.warning(
            "Get images failed: %s",
            str(exc),
        )
        raise DatabaseError(f"Failed to retrieve images: {exc}") from exc
    except (httpx.HTTPError, Exception) as exc:
        logger.warning(
            "Get images error: %s",
            type(exc).__name__,
        )
        raise DatabaseError(f"Database error: {exc}") from exc
