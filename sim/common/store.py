"""Each simulated company keeps its own state: one Cosmos DB database per company in Azure, a dict in tests.

Documents carry an `id` and a `doc_type`; work that is due later carries `next_at` (ISO time),
so the engines can ask for "everything of this type that is due now".
"""
from __future__ import annotations

import copy
import threading
from typing import Any, Protocol


class SimStore(Protocol):
    def get(self, doc_id: str) -> dict[str, Any] | None: ...
    def put(self, doc: dict[str, Any]) -> None: ...
    def delete(self, doc_id: str) -> None: ...
    def query(self, doc_type: str, *, due_by: str | None = None, **equals: Any) -> list[dict[str, Any]]: ...


class MemoryStore:
    def __init__(self):
        self._docs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            d = self._docs.get(doc_id)
            return copy.deepcopy(d) if d is not None else None

    def put(self, doc: dict[str, Any]) -> None:
        with self._lock:
            self._docs[doc["id"]] = copy.deepcopy(doc)

    def delete(self, doc_id: str) -> None:
        with self._lock:
            self._docs.pop(doc_id, None)

    def query(self, doc_type: str, *, due_by: str | None = None, **equals: Any) -> list[dict[str, Any]]:
        with self._lock:
            out = [copy.deepcopy(d) for d in self._docs.values()
                   if d.get("doc_type") == doc_type
                   and (due_by is None or (d.get("next_at") is not None and d["next_at"] <= due_by))
                   and all(d.get(k) == v for k, v in equals.items())]
        return sorted(out, key=lambda d: (d.get("next_at") or "", d["id"]))


class CosmosStore:
    """Container `state` (partition key /id) in the company's own database."""

    def __init__(self, url: str, database: str, credential=None):
        from azure.cosmos import CosmosClient
        from azure.identity import DefaultAzureCredential
        client = CosmosClient(url, credential=credential or DefaultAzureCredential())
        self._c = client.get_database_client(database).get_container_client("state")

    def get(self, doc_id: str) -> dict[str, Any] | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError
        try:
            return _clean(self._c.read_item(item=doc_id, partition_key=doc_id))
        except CosmosResourceNotFoundError:
            return None

    def put(self, doc: dict[str, Any]) -> None:
        self._c.upsert_item(doc)

    def delete(self, doc_id: str) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError
        try:
            self._c.delete_item(item=doc_id, partition_key=doc_id)
        except CosmosResourceNotFoundError:
            pass

    def query(self, doc_type: str, *, due_by: str | None = None, **equals: Any) -> list[dict[str, Any]]:
        where, params = ["c.doc_type = @t"], [{"name": "@t", "value": doc_type}]
        if due_by is not None:
            where.append("IS_DEFINED(c.next_at) AND NOT IS_NULL(c.next_at) AND c.next_at <= @due")
            params.append({"name": "@due", "value": due_by})
        for i, (k, v) in enumerate(equals.items()):
            where.append(f"c.{k} = @p{i}")
            params.append({"name": f"@p{i}", "value": v})
        items = self._c.query_items(f"SELECT * FROM c WHERE {' AND '.join(where)}", parameters=params,
                                    enable_cross_partition_query=True)
        return sorted((_clean(d) for d in items), key=lambda d: (d.get("next_at") or "", d["id"]))


def _clean(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if not k.startswith("_")}
