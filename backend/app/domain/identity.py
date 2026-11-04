"""Immutable identity values. Channels resolve principals at the trusted edge."""
from dataclasses import dataclass
from datetime import datetime
from typing import Literal


UserKind = Literal["guest", "registered", "legacy_unclaimed"]
LEGACY_USER_ID = "legacy_unclaimed"


@dataclass(frozen=True)
class Principal:
    user_id: str
    issuer: str | None = None
    subject: str | None = None
    operator: bool = False


@dataclass(frozen=True)
class User:
    user_id: str
    kind: UserKind
    created_at: datetime


@dataclass(frozen=True)
class AccessSessionCredentials:
    token: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True)
class AccessSessionPrincipal:
    principal: Principal
    csrf_hash: str
    expires_at: datetime


@dataclass(frozen=True)
class OwnedSessionPage:
    items: list
    next_cursor: str | None = None
