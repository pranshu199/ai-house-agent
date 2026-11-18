"""Schema for property image upload and management."""

from dataclasses import dataclass
from typing import Optional
from pydantic import BaseModel, Field


@dataclass(frozen=True)
class PropertyImageMetadata:
    """Image metadata stored in the database."""
    id: str
    property_id: str
    realtor_id: str
    storage_path: str
    is_primary: bool
    sort_order: int
    created_at: str


class PropertyImageUploadResponse(BaseModel):
    """Response after uploading an image."""
    id: str
    storage_path: str
    is_primary: bool
    sort_order: int
    created_at: str


class PropertyImageMetadataResponse(BaseModel):
    """Response with image metadata and signed URL."""
    id: str
    storage_path: str
    is_primary: bool
    sort_order: int
    created_at: str
    signed_url: Optional[str] = Field(
        None,
        description="Temporary signed URL for accessing the image (expires in 1 hour)"
    )


class PropertyGalleryResponse(BaseModel):
    """Response with all images for a property."""
    images: list[PropertyImageMetadataResponse]
    primary_image: Optional[PropertyImageMetadataResponse] = Field(
        None,
        description="The designated primary image for the property"
    )


class SetPrimaryImageRequest(BaseModel):
    """Request to set an image as primary."""
    image_id: str


class ReorderImagesRequest(BaseModel):
    """Request to reorder images."""
    images: list[dict] = Field(
        ...,
        description="List of {id, sort_order} dicts"
    )
