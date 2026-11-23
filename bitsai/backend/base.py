"""Contract between plugin features and Datadog.

Every feature (chat, investigations, alerts, unfurls) talks to :class:`BitsBackend`, never to
MCP or REST directly. Datadog's Bits surfaces are young and change; when they do, only the
adapter in ``backend/mcp.py`` (or ``backend/rest.py``) should need to change.

Shapes here are derived from the Phase 0 fixtures in ``tests/fixtures/`` — keep them in sync.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class BackendError(Exception):
    """Base class for errors surfaced to users. ``str(err)`` must be safe to show in a room."""


class AuthRequired(BackendError):
    """The user has no usable credential (never linked, revoked, or refresh failed)."""


class PermissionDenied(BackendError):
    """Datadog returned 403 — the user's role lacks a permission (e.g. ``assistant_access``)."""


class RateLimited(BackendError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class UpstreamError(BackendError):
    """Unexpected Datadog response, network failure, or schema drift."""


# --------------------------------------------------------------------------- chat


class ChatStatus(enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    REQUIRES_ACTION = "requires_action"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"  # anything not listed above; treat as non-terminal

    @property
    def terminal(self) -> bool:
        return self in (ChatStatus.COMPLETED, ChatStatus.FAILED, ChatStatus.CANCELLED)


@dataclass(frozen=True)
class PendingAction:
    """Bits wants the user to approve/deny something before continuing."""

    id: str
    description: str  # human-readable summary, Markdown
    raw: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class ChatTurn:
    conversation_id: str
    task_id: str
    status: ChatStatus
    message: str | None = None  # Markdown; links may be relative ("/monitors/123")
    pending_action: PendingAction | None = None
    error: str | None = None
    poll_after: float | None = None  # server hint, seconds


# --------------------------------------------------------------------------- investigations


class InvestigationStatus(enum.Enum):
    IN_PROGRESS = "in progress"
    NEEDS_DIRECTION = "needs direction"
    CONCLUSIVE = "conclusive"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"
    UNKNOWN = "unknown"

    @property
    def terminal(self) -> bool:
        return self in (
            InvestigationStatus.CONCLUSIVE,
            InvestigationStatus.INCONCLUSIVE,
            InvestigationStatus.FAILED,
        )

    @classmethod
    def parse(cls, raw: object) -> InvestigationStatus:
        """Case/space/underscore tolerant: "in progress", "In_Progress", ... else UNKNOWN."""
        norm = " ".join(str(raw or "").lower().replace("_", " ").replace("-", " ").split())
        for st in cls:
            if st is not cls.UNKNOWN and st.value == norm:
                return st
        return cls.UNKNOWN


@dataclass(frozen=True)
class Conclusion:
    title: str
    summary: str
    description: str = ""  # Markdown, may be long


@dataclass(frozen=True)
class Investigation:
    id: str
    status: InvestigationStatus
    raw_status: str
    title: str | None = None
    conclusions: tuple[Conclusion, ...] = ()
    url: str | None = None  # absolute deep link into the Datadog app


@dataclass(frozen=True)
class MonitorAlertRef:
    monitor_id: int
    event_id: str  # numeric string, >= 16 digits
    event_ts_ms: int


# --------------------------------------------------------------------------- unfurl


class ResourceKind(enum.Enum):
    MONITOR = "monitor"
    DASHBOARD = "dashboard"
    INCIDENT = "incident"
    NOTEBOOK = "notebook"


@dataclass(frozen=True)
class ResourceSummary:
    kind: ResourceKind
    id: str
    title: str
    url: str
    status: str | None = None  # e.g. monitor overall_state, incident state
    fields: tuple[tuple[str, str], ...] = ()  # small ordered key/value list for a card


# --------------------------------------------------------------------------- backend


@runtime_checkable
class BitsBackend(Protocol):
    """All calls act **as one Datadog user**; instances are bound to a credential.

    Obtain one per request via ``BackendFactory.for_user(mxid)``. Implementations must:
    - inject the MCP ``telemetry.intent`` argument themselves;
    - map 401 -> AuthRequired, 403 -> PermissionDenied, 429 -> RateLimited, else UpstreamError;
    - never log credentials or full response bodies at INFO or above.
    """

    # chat
    async def chat_send(
        self,
        message: str,
        conversation_id: str | None = None,
        pending_action_id: str | None = None,
    ) -> ChatTurn: ...

    async def chat_poll(self, conversation_id: str, task_id: str) -> ChatTurn:
        """One poll. The server long-polls (~4–16 s); callers loop until ``status.terminal``
        or ``REQUIRES_ACTION``, honouring an overall timeout."""
        ...

    # investigations
    async def investigation_start_general(
        self,
        description: str,
        tags: list[str] | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> str:
        """Returns the investigation id. ``description`` max 4096 chars."""
        ...

    async def investigation_start_from_alert(self, alert: MonitorAlertRef) -> str: ...

    async def investigation_get(self, investigation_id: str) -> Investigation: ...

    async def investigation_steer(self, investigation_id: str, message: str) -> None:
        """For a running investigation. Use ``investigation_resume`` once terminal."""
        ...

    async def investigation_resume(self, investigation_id: str, context: str) -> None: ...

    # unfurl / lookups
    async def resource_get(self, kind: ResourceKind, resource_id: str) -> ResourceSummary: ...

    async def graph_snapshot(self, metric_query: str, start_s: int, end_s: int) -> str | None:
        """Public PNG URL, or None if unsupported for this credential type."""
        ...


class BackendFactory(Protocol):
    async def for_user(self, mxid: str) -> BitsBackend:
        """Raises AuthRequired if the user has not linked Datadog."""
        ...
