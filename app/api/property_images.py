"""API endpoints for property image management."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse

from app.api.auth import get_authenticated_realtor
from app.schemas.auth import AuthenticatedRealtor
from app.schemas.property_images import (
    PropertyImageMetadataResponse,
    PropertyGalleryResponse,
    SetPrimaryImageRequest,
    ReorderImagesRequest,
)
from app.services.property_images import (
    DatabaseError,
    InvalidFileError,
    ImageLimitExceededError,
    StorageError,
    PropertyImageError,
    auto_promote_primary_image,
    delete_image,
    get_property_images,
    get_image_signed_url,
    insert_image_metadata,
    reorder_images,
    set_primary_image,
    upload_image_to_storage,
    validate_file,
    MAX_IMAGES_PER_PROPERTY,
    MAX_FILE_SIZE_BYTES,
)


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/properties",
    tags=["property-images"],
)


def _image_to_response(image: dict, include_signed_url: bool = False) -> PropertyImageMetadataResponse:
    """Convert image record to response with optional signed URL."""
    signed_url = None
    if include_signed_url:
        try:
            signed_url = get_image_signed_url(image["storage_path"])
        except StorageError as exc:
            logger.warning("Failed to generate signed URL: %s", str(exc))

    return PropertyImageMetadataResponse(
        id=image["id"],
        storage_path=image["storage_path"],
        is_primary=image["is_primary"],
        sort_order=image["sort_order"],
        created_at=image["created_at"],
        signed_url=signed_url,
    )


def _verify_property_ownership(
    property_id: str,
    realtor: AuthenticatedRealtor,
) -> None:
    """Verify that the authenticated realtor owns the property."""
    from app.integrations.supabase_client import get_supabase_client
    from postgrest.exceptions import APIError
    import httpx

    try:
        supabase = get_supabase_client()
        response = (
            supabase.table("properties")
            .select("id")
            .eq("id", property_id)
            .eq("realtor_id", realtor.realtor_id)
            .limit(1)
            .execute()
        )

        if not (response.data or []):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Property not found or access denied",
            )

    except APIError as exc:
        logger.warning("Property lookup failed: %s", str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Property service unavailable",
        ) from None
    except (httpx.HTTPError, Exception) as exc:
        logger.warning("Property lookup error: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service unavailable",
        ) from None


@router.post("/{propertyId}/images/upload", status_code=status.HTTP_201_CREATED)
async def upload_image(
    propertyId: str,
    file: UploadFile,
    realtor: Annotated[AuthenticatedRealtor, Depends(get_authenticated_realtor)],
) -> PropertyImageMetadataResponse:
    """
    Upload a single image for a property.

    - Validates file type (JPEG, PNG, WebP) and size (max 5 MB)
    - Enforces 10-image limit per property
    - Automatically sets first image as primary
    - Returns image metadata with signed URL

    Returns 201 on success, 400 for validation errors, 404 for missing property,
    413 for file too large, 503 for service errors.
    """
    # Verify property ownership
    _verify_property_ownership(propertyId, realtor)

    # Read file
    try:
        file_data = await file.read()
    except Exception as exc:
        logger.warning("Failed to read uploaded file: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to read uploaded file",
        ) from None

    # Validate file
    try:
        extension, mime_type = validate_file(
            file.filename or "image",
            file_data,
            file.content_type or "application/octet-stream",
        )
    except InvalidFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None

    # Check image count
    try:
        existing_images = get_property_images(propertyId, realtor.realtor_id)
    except DatabaseError as exc:
        logger.warning("Failed to check image count: %s", str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to check image limit",
        ) from None

    if len(existing_images) >= MAX_IMAGES_PER_PROPERTY:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Property already has maximum {MAX_IMAGES_PER_PROPERTY} images",
        )

    # Upload to storage
    try:
        storage_path = upload_image_to_storage(
            realtor.realtor_id,
            propertyId,
            file_data,
            extension,
        )
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to upload image",
        ) from None

    # Insert metadata - first image becomes primary
    is_primary = len(existing_images) == 0
    try:
        image = insert_image_metadata(
            realtor.realtor_id,
            propertyId,
            storage_path,
            is_primary=is_primary,
            sort_order=len(existing_images),
        )
    except ImageLimitExceededError:
        # Race condition: another upload happened between check and insert
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Property now has maximum {MAX_IMAGES_PER_PROPERTY} images",
        ) from None
    except DatabaseError as exc:
        # Try to clean up storage
        try:
            from app.integrations.supabase_client import get_supabase_client
            get_supabase_client().storage.from_("property-images").remove([storage_path])
        except Exception:
            logger.warning("Failed to clean up orphaned storage file: %s", storage_path)

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to store image metadata",
        ) from None

    logger.info(
        "Image uploaded successfully",
        extra={
            "image_id": image["id"],
            "property_id": propertyId,
            "is_primary": is_primary,
        },
    )

    return _image_to_response(image, include_signed_url=True)


@router.get("/{propertyId}/images")
async def get_gallery(
    propertyId: str,
    realtor: Annotated[AuthenticatedRealtor, Depends(get_authenticated_realtor)],
) -> PropertyGalleryResponse:
    """
    Retrieve all images for a property with signed URLs.

    Returns list of images ordered by sort_order and identifies primary image.
    """
    # Verify property ownership
    _verify_property_ownership(propertyId, realtor)

    try:
        images = get_property_images(propertyId, realtor.realtor_id)
    except DatabaseError as exc:
        logger.warning("Failed to retrieve images: %s", str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to retrieve images",
        ) from None

    image_responses = [_image_to_response(img, include_signed_url=True) for img in images]
    primary = next((img for img in image_responses if img.is_primary), None)

    return PropertyGalleryResponse(
        images=image_responses,
        primary_image=primary,
    )


@router.delete("/{propertyId}/images/{imageId}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_property_image(
    propertyId: str,
    imageId: str,
    realtor: Annotated[AuthenticatedRealtor, Depends(get_authenticated_realtor)],
) -> None:
    """
    Delete an image from a property.

    - Removes from storage and database
    - If deleted image was primary, auto-promotes another image
    - Returns 204 on success, 404 if image not found
    """
    # Verify property ownership
    _verify_property_ownership(propertyId, realtor)

    try:
        delete_image(imageId, propertyId, realtor.realtor_id)
    except DatabaseError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Image not found",
            ) from None
        logger.warning("Failed to delete image: %s", str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to delete image",
        ) from None

    logger.info(
        "Image deleted successfully",
        extra={
            "image_id": imageId,
            "property_id": propertyId,
        },
    )


@router.patch("/{propertyId}/images/{imageId}/primary")
async def set_primary(
    propertyId: str,
    imageId: str,
    realtor: Annotated[AuthenticatedRealtor, Depends(get_authenticated_realtor)],
) -> PropertyImageMetadataResponse:
    """
    Set an image as the primary image for a property.

    Atomically clears primary from other images and sets this one as primary.
    """
    # Verify property ownership
    _verify_property_ownership(propertyId, realtor)

    try:
        image = set_primary_image(propertyId, realtor.realtor_id, imageId)
    except DatabaseError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Image not found",
            ) from None
        logger.warning("Failed to set primary image: %s", str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to set primary image",
        ) from None

    logger.info(
        "Primary image changed",
        extra={
            "image_id": imageId,
            "property_id": propertyId,
        },
    )

    return _image_to_response(image, include_signed_url=True)


@router.patch("/{propertyId}/images/reorder")
async def reorder_property_images(
    propertyId: str,
    request: ReorderImagesRequest,
    realtor: Annotated[AuthenticatedRealtor, Depends(get_authenticated_realtor)],
) -> PropertyGalleryResponse:
    """
    Reorder images by updating sort_order for multiple images.

    Request body: {"images": [{"id": "uuid", "sort_order": 0}, ...]}
    """
    # Verify property ownership
    _verify_property_ownership(propertyId, realtor)

    # Parse image orders
    image_orders = []
    for item in request.images:
        try:
            img_id = item.get("id")
            sort_order = item.get("sort_order")
            if not img_id or sort_order is None:
                raise ValueError("Missing id or sort_order")
            if not isinstance(sort_order, int) or sort_order < 0:
                raise ValueError("sort_order must be non-negative integer")
            image_orders.append((img_id, sort_order))
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid image order specification: {exc}",
            ) from None

    try:
        images = reorder_images(propertyId, realtor.realtor_id, image_orders)
    except DatabaseError as exc:
        logger.warning("Failed to reorder images: %s", str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to reorder images",
        ) from None

    image_responses = [_image_to_response(img, include_signed_url=True) for img in images]
    primary = next((img for img in image_responses if img.is_primary), None)

    return PropertyGalleryResponse(
        images=image_responses,
        primary_image=primary,
    )
