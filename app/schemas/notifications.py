"""Realtor notification preference schemas."""

from pydantic import BaseModel, ConfigDict


class NotificationPreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sms_enabled: bool | None = None
    email_enabled: bool | None = None
    sms_incoming_calls: bool | None = None
    sms_new_leads: bool | None = None
    sms_viewing_requests: bool | None = None
    email_new_leads: bool | None = None
    email_viewing_requests: bool | None = None
