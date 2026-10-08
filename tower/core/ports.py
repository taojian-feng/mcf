"""Ports: the only interfaces agents and services may depend on.

Each cloud provides adapters for these (tower/adapters/<cloud>). Keep this file free of
cloud SDK imports so the core runs unchanged on Azure, GCP and AWS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol, TypeVar

from pydantic import BaseModel

from .models import AuditRecord, new_id

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class Event:
    """Envelope for every message on the bus."""
    topic: str
    type: str
    payload: dict[str, Any]
    occurred_at: datetime
    event_id: str = field(default_factory=lambda: new_id("evt"))
    correlation_id: str | None = None  # order id, case id, ... (also the session key for ordering)


Handler = Callable[[Event], None]


class Clock(Protocol):
    def now(self) -> datetime: ...


class MessageBus(Protocol):
    """Topics + subscriptions + durable delayed delivery (used for every SLA timer)."""

    def publish(self, event: Event, *, deliver_at: datetime | None = None) -> str:
        """Publish now, or schedule for deliver_at. Returns a message id usable for cancel."""

    def cancel_scheduled(self, message_id: str) -> None: ...

    def subscribe(self, topic: str, handler: Handler, *, subscription: str) -> None: ...


class DocumentStore(Protocol):
    """Operational state. Collections map to Cosmos containers / Firestore collections / Dynamo tables."""

    def get(self, collection: str, key: str) -> dict[str, Any] | None: ...

    def put(self, collection: str, key: str, doc: dict[str, Any]) -> None: ...

    def query(self, collection: str, **equals: Any) -> list[dict[str, Any]]: ...


class BlobStore(Protocol):
    def write(self, container: str, path: str, data: bytes, *, content_type: str = "application/json") -> str: ...

    def read(self, container: str, path: str) -> bytes: ...


class AuditLedger(Protocol):
    """Append-only. Implementations must make update/delete impossible for agents and users."""

    def append(self, record: AuditRecord) -> None: ...

    def query(
        self,
        *,
        actor: str | None = None,
        order_id: str | None = None,
        sku: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        tenant: str | None = None,
    ) -> list[AuditRecord]: ...


class SecretStore(Protocol):
    def get_secret(self, name: str) -> str: ...


class Directory(Protocol):
    """Identity lookups: roles for approvals, who may see each client's insight data, contacts."""

    def has_role(self, principal: str, role: str) -> bool: ...

    def principals_with_role(self, role: str) -> list[str]: ...

    def pilot_authorization_list(self) -> set[str]:
        """Who may see tenant one's insight data."""

    def insight_access(self, tenant_id: str) -> set[str]:
        """Who may see one client's insight data (sales, forecasts). Tenant one also uses the list above."""

    def contact(self, name: str) -> str | None:
        """Resolve a logical contact (e.g. 'corp_b_data_contact') to an address."""


class Notifier(Protocol):
    def send(self, recipient: str, subject: str, body: str, *, severity: str = "info") -> None: ...


class LLM(Protocol):
    def structured(self, *, system: str, prompt: str, schema: type[T]) -> T | None:
        """Return a parsed schema instance, or None if the model declined / failed."""

    def answer(self, *, system: str, question: str, tools: list[dict[str, Any]],
               run_tool: Callable[[str, dict[str, Any]], str], max_turns: int = 8) -> str | None:
        """Answer a question, calling read-only tools as needed (tools: name, description, input_schema).
        run_tool returns the tool's result as text. None if the model failed or ran out of turns."""


class MCFTransport(Protocol):
    """Raw transport to the Corp A MCF API. Raises MCFError on failure."""

    def call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class MCFError(Exception):
    def __init__(self, operation: str, code: str, message: str, *, retryable: bool):
        super().__init__(f"{operation}: {code}: {message}")
        self.operation = operation
        self.code = code
        self.retryable = retryable


RETRYABLE_MCF_CODES = {"timeout", "throttled", "service_unavailable"}
NON_RETRYABLE_MCF_CODES = {"validation", "unauthorized", "forbidden", "malformed_request"}

