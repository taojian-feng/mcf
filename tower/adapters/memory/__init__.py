"""In-process adapters: run the whole system locally and in tests with virtual time."""
from __future__ import annotations

import copy
import heapq
import itertools
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from pydantic import BaseModel

from tower.core.models import AuditRecord
from tower.core.ports import Event, Handler, MCFError


class VirtualClock:
    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def set(self, t: datetime) -> None:
        if t < self._now:
            raise ValueError("virtual time cannot go backwards")
        self._now = t


class InMemoryBus:
    """Synchronous bus with durable-style scheduled delivery driven by a VirtualClock."""

    def __init__(self, clock: VirtualClock, *, raise_errors: bool = True):
        self.clock = clock
        self.raise_errors = raise_errors
        self._subs: dict[str, list[tuple[str, Handler]]] = defaultdict(list)
        self._queue: deque[tuple[str, Event]] = deque()
        self._scheduled: list[tuple[datetime, int, str, Event]] = []
        self._cancelled: set[str] = set()
        self._seq = itertools.count()
        self._dispatching = False
        self.published: list[Event] = []
        self.dead_letters: list[tuple[Event, Exception]] = []

    def subscribe(self, topic: str, handler: Handler, *, subscription: str) -> None:
        self._subs[topic].append((subscription, handler))

    def publish(self, event: Event, *, deliver_at: datetime | None = None) -> str:
        msg_id = f"msg_{next(self._seq)}"
        if deliver_at is not None and deliver_at > self.clock.now():
            heapq.heappush(self._scheduled, (deliver_at, next(self._seq), msg_id, event))
        else:
            self.published.append(event)
            self._queue.append((msg_id, event))
            self._drain()
        return msg_id

    def cancel_scheduled(self, message_id: str) -> None:
        self._cancelled.add(message_id)

    def _drain(self) -> None:
        if self._dispatching:
            return
        self._dispatching = True
        try:
            while self._queue:
                _, event = self._queue.popleft()
                for _, handler in list(self._subs.get(event.topic, [])):
                    try:
                        handler(event)
                    except Exception as exc:  # mirrors dead-lettering on a real broker
                        self.dead_letters.append((event, exc))
                        if self.raise_errors:
                            raise
        finally:
            self._dispatching = False

    def advance(self, delta: timedelta) -> None:
        """Move virtual time forward, delivering every scheduled message that falls due, in order."""
        target = self.clock.now() + delta
        while self._scheduled and self._scheduled[0][0] <= target:
            due, _, msg_id, event = heapq.heappop(self._scheduled)
            if msg_id in self._cancelled:
                continue
            self.clock.set(max(due, self.clock.now()))
            self.published.append(event)
            self._queue.append((msg_id, event))
            self._drain()
        self.clock.set(target)

    def events(self, topic: str, type_: str | None = None) -> list[Event]:
        return [e for e in self.published if e.topic == topic and (type_ is None or e.type == type_)]


class InMemoryStore:
    def __init__(self):
        self._data: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)

    def get(self, collection: str, key: str) -> dict[str, Any] | None:
        doc = self._data[collection].get(key)
        return copy.deepcopy(doc) if doc is not None else None

    def put(self, collection: str, key: str, doc: dict[str, Any]) -> None:
        self._data[collection][key] = copy.deepcopy(doc)

    def query(self, collection: str, **equals: Any) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(d) for d in self._data[collection].values()
            if all(d.get(k) == v for k, v in equals.items())
        ]


class InMemoryBlobStore:
    def __init__(self):
        self.blobs: dict[tuple[str, str], bytes] = {}

    def write(self, container: str, path: str, data: bytes, *, content_type: str = "application/json") -> str:
        self.blobs[(container, path)] = data
        return f"memory://{container}/{path}"

    def read(self, container: str, path: str) -> bytes:
        return self.blobs[(container, path)]

    def list(self, container: str, prefix: str) -> list[str]:
        return sorted(p for c, p in self.blobs if c == container and p.startswith(prefix))


class LedgerTamperError(PermissionError):
    pass


class InMemoryLedger:
    """Append-only list; update/delete always raise, like an Azure SQL append-only ledger table."""

    def __init__(self):
        self._records: list[AuditRecord] = []
        self.fail_next_appends = 0  # test hook for R10.6

    def append(self, record: AuditRecord) -> None:
        if self.fail_next_appends:
            self.fail_next_appends -= 1
            raise IOError("ledger unavailable")
        self._records.append(record.model_copy(deep=True))

    def update(self, record_id: str, **_: Any) -> None:
        raise LedgerTamperError("audit ledger is append-only")

    def delete(self, record_id: str) -> None:
        raise LedgerTamperError("audit ledger is append-only")

    def query(self, *, actor=None, order_id=None, sku=None, start=None, end=None, tenant=None) -> list[AuditRecord]:
        out = []
        for r in self._records:
            if actor and r.actor != actor:
                continue
            if tenant and r.tenant_id != tenant:
                continue
            if order_id and order_id not in r.order_ids:
                continue
            if sku and sku not in r.skus:
                continue
            if start and r.at < start:
                continue
            if end and r.at > end:
                continue
            out.append(r.model_copy(deep=True))
        return out


class StaticDirectory:
    def __init__(self, roles: dict[str, set[str]] | None = None, pilot_list: set[str] | None = None,
                 contacts: dict[str, str] | None = None, insight: dict[str, set[str]] | None = None):
        self.roles = roles or {}
        self.pilot_list = pilot_list or set()
        self.contacts = contacts or {}
        self.insight = insight or {}

    def has_role(self, principal: str, role: str) -> bool:
        return role in self.roles.get(principal, set())

    def principals_with_role(self, role: str) -> list[str]:
        return sorted(p for p, rs in self.roles.items() if role in rs)

    def pilot_authorization_list(self) -> set[str]:
        return set(self.pilot_list)

    def insight_access(self, tenant_id: str) -> set[str]:
        return set(self.insight.get(tenant_id, set()))

    def contact(self, name: str) -> str | None:
        return self.contacts.get(name)


class RecordingNotifier:
    def __init__(self):
        self.sent: list[dict[str, str]] = []

    def send(self, recipient: str, subject: str, body: str, *, severity: str = "info") -> None:
        self.sent.append({"to": recipient, "subject": subject, "body": body, "severity": severity})

    def to(self, recipient: str) -> list[dict[str, str]]:
        return [m for m in self.sent if m["to"] == recipient]


class NullLLM:
    """No LLM configured: callers fall back to deterministic logic."""

    def structured(self, *, system: str, prompt: str, schema: type[BaseModel]):
        return None

    def answer(self, *, system: str, question: str, tools, run_tool, max_turns: int = 8):
        return None


class ScriptedLLM(NullLLM):
    """For tests: `answer` calls the scripted tools in order, then returns the scripted text (or a
    function of the tool results). `structured` returns scripted outputs by schema name."""

    def __init__(self, *, tool_calls: list[tuple[str, dict[str, Any]]] | None = None, answer_text: Any = "",
                 structured_outputs: dict[str, Any] | None = None):
        self.tool_calls, self.answer_text = tool_calls or [], answer_text
        self.structured_outputs = structured_outputs or {}
        self.seen: list[dict[str, Any]] = []

    def structured(self, *, system: str, prompt: str, schema: type[BaseModel]):
        self.seen.append({"system": system, "prompt": prompt})
        out = self.structured_outputs.get(schema.__name__)
        return schema.model_validate(out) if isinstance(out, dict) else out

    def answer(self, *, system: str, question: str, tools, run_tool, max_turns: int = 8):
        results = [run_tool(name, args) for name, args in self.tool_calls]
        self.seen.append({"system": system, "question": question, "tools": [t["name"] for t in tools],
                          "results": results})
        return self.answer_text(results) if callable(self.answer_text) else self.answer_text


class FakeMCF:
    """Scriptable stand-in for the Corp A MCF API.

    `script[operation]` is a list of outcomes consumed per call: a dict response, or an error
    code string ('timeout', 'throttled', 'validation', ...). When the list is empty the
    `responder` for that operation (if any) or a default success response is used.
    """

    RETRYABLE = {"timeout", "throttled", "service_unavailable"}

    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.script: dict[str, list[Any]] = defaultdict(list)
        self.orders: dict[str, dict[str, Any]] = {}
        self.responder: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}

    def call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, copy.deepcopy(payload)))
        if self.script[operation]:
            outcome = self.script[operation].pop(0)
            if isinstance(outcome, str):
                raise MCFError(operation, outcome, "scripted failure", retryable=outcome in self.RETRYABLE)
            return outcome
        if operation in self.responder:
            return self.responder[operation](payload)
        if operation == "CreateFulfillmentOrder":
            self.orders[payload["sellerFulfillmentOrderId"]] = payload
            return {"accepted": True}
        if operation == "GetFulfillmentOrder":
            return {"estimatedArrivalDate": None}
        return {}

    def ops(self, operation: str) -> list[dict[str, Any]]:
        return [p for op, p in self.calls if op == operation]
