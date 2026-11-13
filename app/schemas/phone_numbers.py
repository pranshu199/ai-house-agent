"""Phone-number API request schemas."""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


MAX_SEARCH_RESULTS = 20
_AREA_CODE_PATTERN = re.compile(r"[2-9][0-9]{2}\Z")


class PhoneNumberSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    area_code: str | None = None
    locality: str | None = None
    limit: int = Field(default=10, strict=True, ge=1, le=MAX_SEARCH_RESULTS)

    @field_validator("area_code")
    @classmethod
    def validate_area_code(cls, value: str | None) -> str | None:
        if value is not None and not _AREA_CODE_PATTERN.fullmatch(value.strip()):
            raise ValueError("area_code must be a three-digit area code")
        return value.strip() if value is not None else None

    @field_validator("locality")
    @classmethod
    def validate_locality(cls, value: str | None) -> str | None:
        if value is None:
            return None

        value = " ".join(value.split())
        if not value:
            return None
        if len(value) > 60 or any(
            not (character.isalnum() or character in " .'-")
            for character in value
        ):
            raise ValueError("locality contains unsupported characters or is too long")
        return value


class PhoneNumberProvisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=255)
    area_code: str | None = None
    locality: str | None = None

    @field_validator("area_code")
    @classmethod
    def validate_area_code(cls, value: str | None) -> str | None:
        if value is not None and not _AREA_CODE_PATTERN.fullmatch(value.strip()):
            raise ValueError("area_code must be a three-digit area code")
        return value.strip() if value is not None else None

    @field_validator("locality")
    @classmethod
    def validate_locality(cls, value: str | None) -> str | None:
        return PhoneNumberSearchRequest.validate_locality(value)


class PhoneNumberPurchaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone_number: str = Field(pattern=r"^\+1[2-9][0-9]{9}$")


class PhoneNumberReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_release: bool | None = Field(default=None, strict=True)
