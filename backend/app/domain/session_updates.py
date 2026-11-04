"""Current-state delivery contracts, independent of HTTP and asyncio."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from ..models import Session


UpdateReason = Literal["connected", "updated", "freshness"]


@dataclass(frozen=True)
class SessionSnapshot:
    """One committed read; sequence is independent of itinerary revision."""

    sequence: int
    session: Session
    job: dict[str, Any] | None


@dataclass(frozen=True)
class SessionView:
    sequence: int
    session: dict[str, Any]
    job: dict[str, Any] | None
    server_time: str
    reason: UpdateReason
    next_expiry: datetime | None

    def to_dict(self) -> dict[str, Any]:
        # The transport deadline is an application concern, not public state.
        return {"sequence": self.sequence, "session": self.session, "job": self.job,
                "server_time": self.server_time, "reason": self.reason}


class SessionSnapshotReader(Protocol):
    def read_session_snapshot(self, session_id: str) -> SessionSnapshot | None: ...


class SessionUpdateSubscription(Protocol):
    async def wait(self) -> None: ...

    def close(self) -> None: ...


class SessionUpdateNotifications(Protocol):
    def subscribe(self, session_id: str) -> SessionUpdateSubscription: ...

    def notify(self, session_id: str) -> None: ...
