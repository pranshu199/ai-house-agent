"""Trusted identity context used by authenticated application services."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AuthenticatedRealtor:
    user_id: str
    realtor_id: str
