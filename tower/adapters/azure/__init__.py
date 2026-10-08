"""Azure adapters. Imports of Azure SDKs are local so the core never needs them installed.

Auth everywhere is DefaultAzureCredential (managed identity in Container Apps, az login locally).
Environment variables (set by infra/azure/main.bicep outputs):
  MCF_SERVICEBUS_NAMESPACE   <ns>.servicebus.windows.net
  MCF_COSMOS_URL             https://<acct>.documents.azure.com:443/
  MCF_COSMOS_DB              mcf
  MCF_BLOB_URL               https://<acct>.blob.core.windows.net
  MCF_SQL_SERVER / MCF_SQL_DB
  MCF_KEYVAULT_URL           https://<kv>.vault.azure.net/
  MCF_APPCONFIG_URL          https://<cfg>.azconfig.io
  MCF_ACS_ENDPOINT / MCF_EMAIL_SENDER    (optional; without them notifications are logged to stdout)
  MCF_API_MODE               sandbox (in-process fake MCF API) | live (MCF_API_BASE_URL, OAuth2 client
                             credentials from Key Vault secrets mcf-client-id / mcf-client-secret)
  MCF_CORPB_URL              Corp B endpoint for OMS and other outbound messages (key: corpb-inbound-api-key)
  MCF_FOUNDRY_RESOURCE / MCF_LLM_MODEL   (optional; Claude on Microsoft Foundry)
  MCF_SUBSCRIPTION           subscription name this process consumes as (one per agent)
"""
from __future__ import annotations

import json
import logging
import os
import struct
import threading
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from tower.core.models import AuditRecord
from tower.core.ports import Event, Handler

log = logging.getLogger("tower.azure")


def _credential():
    from azure.identity import DefaultAzureCredential
    return DefaultAzureCredential()


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


# ---------------------------------------------------------------- Service Bus (MessageBus)

class ServiceBusBus:
    """Topics + subscriptions; timers use scheduled messages (durable, cancellable)."""

    def __init__(self, namespace: str, credential=None):
        from azure.servicebus import ServiceBusClient
        self._client = ServiceBusClient(namespace, credential or _credential())
        self._handlers: list[tuple[str, str, Handler]] = []
        self._senders: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _sender(self, topic: str):
        with self._lock:
            if topic not in self._senders:
                self._senders[topic] = self._client.get_topic_sender(topic_name=topic)
            return self._senders[topic]

    def publish(self, event: Event, *, deliver_at: datetime | None = None) -> str:
        from azure.servicebus import ServiceBusMessage
        msg = ServiceBusMessage(
            json.dumps({"type": event.type, "payload": event.payload, "occurred_at": event.occurred_at.isoformat(),
                        "event_id": event.event_id, "correlation_id": event.correlation_id}),
            message_id=event.event_id, subject=event.type, correlation_id=event.correlation_id,
            content_type="application/json",
        )
        sender = self._sender(event.topic)
        if deliver_at is not None:
            [seq] = sender.schedule_messages(msg, deliver_at)
            return f"{event.topic}#{seq}"
        sender.send_messages(msg)
        return event.event_id

    def cancel_scheduled(self, message_id: str) -> None:
        if "#" not in message_id:
            return
        topic, seq = message_id.rsplit("#", 1)
        try:
            self._sender(topic).cancel_scheduled_messages(int(seq))
        except Exception:
            pass  # already delivered; handlers re-check state so a late timer is harmless

    def subscribe(self, topic: str, handler: Handler, *, subscription: str) -> None:
        self._handlers.append((topic, subscription, handler))

    def run_forever(self) -> None:
        """One receiver thread per (topic, subscription). Failed messages are abandoned and
        dead-lettered by Service Bus after MaxDeliveryCount."""
        threads = [threading.Thread(target=self._pump, args=h, daemon=True) for h in self._handlers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _pump(self, topic: str, subscription: str, handler: Handler) -> None:
        """Long handlers (a 1,360-item forecast file) keep their lock: the renewer extends it for up to an hour."""
        from azure.servicebus import AutoLockRenewer
        renewer = AutoLockRenewer(max_lock_renewal_duration=3600)
        with self._client.get_subscription_receiver(topic_name=topic, subscription_name=subscription,
                                                    auto_lock_renewer=renewer) as rx:
            for msg in rx:
                try:
                    handler(_to_event(topic, msg))
                    rx.complete_message(msg)
                except Exception:
                    log.exception("%s/%s: handler failed; message will be redelivered", topic, subscription)
                    rx.abandon_message(msg)


def _to_event(topic: str, msg) -> Event:
    """Our own envelope, or an Event Grid event (feed files: Microsoft.Storage.BlobCreated)."""
    body = json.loads(b"".join(msg.body))
    if isinstance(body, list):
        body = body[0]
    if "payload" in body and "type" in body:
        return Event(topic=topic, type=body["type"], payload=body["payload"],
                     occurred_at=datetime.fromisoformat(body["occurred_at"]),
                     event_id=body["event_id"], correlation_id=body.get("correlation_id"))
    when = body.get("eventTime") or body.get("time")
    return Event(topic=topic, type=body.get("eventType") or body.get("type") or "external", payload=body,
                 occurred_at=datetime.fromisoformat(when.replace("Z", "+00:00")) if when else datetime.now(timezone.utc),
                 event_id=str(body.get("id") or msg.message_id), correlation_id=body.get("subject"))


# ---------------------------------------------------------------- Cosmos DB (DocumentStore)

class CosmosStore:
    """One container per collection, partitioned on /id. TTL on order_pii enforces R9.5."""

    def __init__(self, url: str, database: str, credential=None):
        from azure.cosmos import CosmosClient
        self._db = CosmosClient(url, credential=credential or _credential()).get_database_client(database)

    def get(self, collection: str, key: str) -> dict[str, Any] | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError
        try:
            doc = self._db.get_container_client(collection).read_item(item=key, partition_key=key)
        except CosmosResourceNotFoundError:
            return None
        return {k: v for k, v in doc.items() if not k.startswith("_") and k != "id"}

    def put(self, collection: str, key: str, doc: dict[str, Any]) -> None:
        self._db.get_container_client(collection).upsert_item({**doc, "id": key})

    def query(self, collection: str, **equals: Any) -> list[dict[str, Any]]:
        where = " AND ".join(f"c.{k} = @p{i}" for i, k in enumerate(equals)) or "true"
        params = [{"name": f"@p{i}", "value": v} for i, v in enumerate(equals.values())]
        items = self._db.get_container_client(collection).query_items(
            f"SELECT * FROM c WHERE {where}", parameters=params, enable_cross_partition_query=True)
        return [{k: v for k, v in d.items() if not k.startswith("_") and k != "id"} for d in items]


# ---------------------------------------------------------------- Blob Storage (BlobStore)

class AzureBlobStore:
    def __init__(self, account_url: str, credential=None):
        from azure.storage.blob import BlobServiceClient
        self._svc = BlobServiceClient(account_url, credential=credential or _credential())

    def write(self, container: str, path: str, data: bytes, *, content_type: str = "application/json") -> str:
        from azure.storage.blob import ContentSettings
        blob = self._svc.get_blob_client(container, path)
        blob.upload_blob(data, overwrite=True, content_settings=ContentSettings(content_type=content_type))
        return blob.url

    def read(self, container: str, path: str) -> bytes:
        return self._svc.get_blob_client(container, path).download_blob().readall()

    def list(self, container: str, prefix: str) -> list[str]:
        return [b.name for b in self._svc.get_container_client(container).list_blobs(name_starts_with=prefix)]


# ---------------------------------------------------------------- Azure SQL ledger (AuditLedger)

class SqlLedger:
    """Azure SQL Database append-only ledger tables (see infra/azure/sql/audit.sql).

    The engine rejects UPDATE and DELETE on APPEND_ONLY ledger tables, which is what makes
    R10.5 / R10.7 hold even against a compromised agent identity.
    """

    def __init__(self, server: str, database: str, credential=None):
        self._server, self._db = server, database
        self._cred = credential or _credential()

    def _conn(self):
        import pyodbc
        token = self._cred.get_token("https://database.windows.net/.default").token.encode("utf-16-le")
        attrs = {1256: struct.pack(f"<I{len(token)}s", len(token), token)}  # SQL_COPT_SS_ACCESS_TOKEN
        return pyodbc.connect(
            f"Driver={{ODBC Driver 18 for SQL Server}};Server=tcp:{self._server},1433;Database={self._db};"
            "Encrypt=yes;TrustServerCertificate=no;", attrs_before=attrs)

    def append(self, record: AuditRecord) -> None:
        refs = ([("order", v) for v in record.order_ids] + [("sku", v) for v in record.skus]
                + [("node", v) for v in record.node_ids]
                + ([("tenant", record.tenant_id)] if record.tenant_id else []))
        with self._conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "INSERT INTO audit.records (record_id, kind, at, actor, action, owning_party, subject_id, body) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                record.record_id, record.kind.value, record.at, record.actor, record.action,
                record.owning_party.value if record.owning_party else None, record.subject_id,
                record.model_dump_json())
            if refs:
                cur.executemany("INSERT INTO audit.record_refs (record_id, ref_type, ref_value) VALUES (?, ?, ?)",
                                [(record.record_id, t, v) for t, v in refs])
            cn.commit()

    def query(self, *, actor=None, order_id=None, sku=None, start=None, end=None, tenant=None) -> list[AuditRecord]:
        sql, args = ["SELECT r.body FROM audit.records r WHERE 1=1"], []
        if actor:
            sql.append("AND r.actor = ?"); args.append(actor)
        for ref_type, val in (("order", order_id), ("sku", sku), ("tenant", tenant)):
            if val:
                sql.append("AND EXISTS (SELECT 1 FROM audit.record_refs x WHERE x.record_id = r.record_id "
                           "AND x.ref_type = ? AND x.ref_value = ?)")
                args += [ref_type, val]
        if start:
            sql.append("AND r.at >= ?"); args.append(start)
        if end:
            sql.append("AND r.at <= ?"); args.append(end)
        sql.append("ORDER BY r.at")
        with self._conn() as cn:
            rows = cn.cursor().execute(" ".join(sql), *args).fetchall()
        return [AuditRecord.model_validate_json(r[0]) for r in rows]


# ---------------------------------------------------------------- Key Vault (SecretStore)

class KeyVaultSecrets:
    """Values are cached for a few minutes, so a rotated secret is picked up without a restart."""

    def __init__(self, vault_url: str, credential=None, *, ttl_s: float = 300):
        from azure.keyvault.secrets import SecretClient
        self._client = SecretClient(vault_url, credential or _credential())
        self._ttl, self._cache = ttl_s, {}

    def get_secret(self, name: str) -> str:
        import time
        hit = self._cache.get(name)
        if hit and hit[1] > time.monotonic():
            return hit[0]
        value = self._client.get_secret(name).value
        self._cache[name] = (value, time.monotonic() + self._ttl)
        return value


# ---------------------------------------------------------------- App Configuration (Directory)

class AppConfigDirectory:
    """Roles, insight access lists, contacts and the client registry from Azure App Configuration.

    Keys: mcf:roles (JSON principal -> [roles], synced from Entra ID groups / app roles),
    mcf:pilot_authorization_list (JSON list, tenant one), mcf:insight_access (JSON tenant -> [principals]),
    mcf:contacts (JSON name -> address), mcf:tenants (JSON tenant id -> client contract).
    Callers' identities are verified upstream (APIM validates Entra ID tokens).
    """

    def __init__(self, url: str, credential=None):
        from azure.appconfiguration import AzureAppConfigurationClient
        self._client = AzureAppConfigurationClient(url, credential or _credential())

    def _json(self, key: str, default):
        try:
            return json.loads(self._client.get_configuration_setting(key=key).value)
        except Exception:
            return default

    def has_role(self, principal: str, role: str) -> bool:
        return role in self._json("mcf:roles", {}).get(principal, [])

    def principals_with_role(self, role: str) -> list[str]:
        return sorted(p for p, rs in self._json("mcf:roles", {}).items() if role in rs)

    def pilot_authorization_list(self) -> set[str]:
        return set(self._json("mcf:pilot_authorization_list", []))

    def insight_access(self, tenant_id: str) -> set[str]:
        return set(self._json("mcf:insight_access", {}).get(tenant_id, []))

    def contact(self, name: str) -> str | None:
        return self._json("mcf:contacts", {}).get(name)

    def tenants(self):
        """The client registry. Raises if App Configuration can't be read, so callers keep their last copy."""
        from azure.core.exceptions import ResourceNotFoundError

        from tower.core.tenants import Tenant, default_tenants
        try:
            raw = json.loads(self._client.get_configuration_setting(key="mcf:tenants").value)
        except ResourceNotFoundError:
            return default_tenants()
        tenants = default_tenants()
        for tid, doc in (raw or {}).items():
            tenants[tid] = Tenant.model_validate({**doc, "tenant_id": tid})
        return tenants

    def pilot_config(self):
        """PilotConfig from key mcf:pilot_config (JSON) plus the client registry; defaults for anything not set."""
        from tower.core.config import PilotConfig
        config = PilotConfig.model_validate(self._json("mcf:pilot_config", {}))
        config.tenants = self.tenants()
        return config


# ---------------------------------------------------------------- Communication Services (Notifier)

class AcsEmailNotifier:
    """Email through Azure Communication Services. A recipient with no address in mcf:contacts, or a
    failed send, is logged instead so a missing contact never breaks the agent that raised it."""

    def __init__(self, endpoint: str, sender: str, directory: AppConfigDirectory, credential=None):
        from azure.communication.email import EmailClient
        self._client = EmailClient(endpoint, credential or _credential())
        self._sender, self._directory, self._log = sender, directory, LogNotifier()

    def send(self, recipient: str, subject: str, body: str, *, severity: str = "info") -> None:
        address = recipient if "@" in recipient else self._directory.contact(recipient)
        if not address:
            self._log.send(recipient, subject, body, severity=severity, note="no email address in mcf:contacts")
            return
        try:
            self._client.begin_send({
                "senderAddress": self._sender,
                "recipients": {"to": [{"address": address}]},
                "content": {"subject": f"[Control Tower {severity}] {subject}", "plainText": body},
            })
            print(json.dumps({"email_sent": {"to": address, "subject": subject, "severity": severity}}), flush=True)
        except Exception as e:  # noqa: BLE001 - delivery problems are logged, not raised
            self._log.send(address, subject, body, severity=severity, note=f"email failed: {e}")


class LogNotifier:
    """Dev fallback: notifications go to stdout, which Container Apps ships to Log Analytics."""

    def send(self, recipient: str, subject: str, body: str, *, severity: str = "info", note: str | None = None) -> None:
        entry = {"to": recipient, "subject": subject, "severity": severity, "body": body}
        if note:
            entry["note"] = note
        print(json.dumps({"notification": entry}), flush=True)


# ---------------------------------------------------------------- Claude (LLM): Microsoft Foundry or Anthropic API

class FoundryClaude:
    """Structured outputs via the Anthropic SDK's Foundry client, signed in with the app's managed
    identity (Entra ID), so there is no API key to store. Any failure or refusal returns None so
    callers fall back to deterministic rules. Keeps call and token counts for evaluations."""

    def __init__(self, resource: str, credential, model: str = "claude-opus-5-5", *, client=None):
        if client is None:
            from anthropic import AnthropicFoundry
            from azure.identity import get_bearer_token_provider
            client = AnthropicFoundry(
                resource=resource,
                azure_ad_token_provider=get_bearer_token_provider(credential, "https://ai.azure.com/.default"))
        self._client = client
        self.model = model
        self.calls = self.input_tokens = self.output_tokens = 0
        self._count = threading.Lock()  # evaluations call from several threads
        self._log = logging.getLogger("tower.llm")

    def structured(self, *, system: str, prompt: str, schema: type[BaseModel]):
        import anthropic
        try:
            # Thinking is always on for this model and counts toward max_tokens, so leave room for it.
            resp = self._client.messages.parse(
                model=self.model, max_tokens=16000, system=system,
                messages=[{"role": "user", "content": prompt}], output_format=schema,
            )
        except anthropic.APIError as e:
            self._log.warning("Claude call failed, falling back to rules: %s", e)
            return None
        with self._count:
            self.calls += 1
            self.input_tokens += resp.usage.input_tokens
            self.output_tokens += resp.usage.output_tokens
        if resp.stop_reason != "end_turn":  # refusal or truncated: the rules decide
            self._log.warning("Claude stopped with %s, falling back to rules", resp.stop_reason)
            return None
        return resp.parsed_output

    def answer(self, *, system: str, question: str, tools: list[dict[str, Any]], run_tool, max_turns: int = 8):
        """A manual tool loop: the tools only read, and each result goes back as text."""
        import anthropic
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        try:
            for _ in range(max_turns):
                resp = self._client.messages.create(model=self.model, max_tokens=16000, system=system,
                                                    tools=tools, messages=messages)
                with self._count:
                    self.calls += 1
                    self.input_tokens += resp.usage.input_tokens
                    self.output_tokens += resp.usage.output_tokens
                if resp.stop_reason == "end_turn":
                    return "".join(b.text for b in resp.content if b.type == "text").strip() or None
                if resp.stop_reason != "tool_use":
                    self._log.warning("copilot stopped with %s", resp.stop_reason)
                    return None
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": b.id, "content": run_tool(b.name, dict(b.input))}
                    for b in resp.content if b.type == "tool_use"]})
        except anthropic.APIError as e:
            self._log.warning("copilot call failed: %s", e)
            return None
        self._log.warning("copilot ran out of turns")
        return None


class AnthropicClaude(FoundryClaude):
    """The same model straight from Anthropic's API, for subscriptions where Foundry has no Claude quota.
    The key lives in Key Vault as `anthropic-api-key`."""

    def __init__(self, api_key: str, model: str = "claude-opus-5-5"):
        from anthropic import Anthropic
        super().__init__("", None, model, client=Anthropic(api_key=api_key))


def _claude(env, secrets: "KeyVaultSecrets", cred):
    """An Anthropic API key in Key Vault wins (it works on any subscription); otherwise Foundry if configured."""
    model = env.get("MCF_LLM_MODEL") or "claude-opus-5-5"
    try:
        key = secrets.get_secret("anthropic-api-key")
    except Exception:  # not set (the usual case on Foundry) or vault unreachable
        key = None
    if key:
        return AnthropicClaude(key, model)
    if env.get("MCF_FOUNDRY_RESOURCE"):
        return FoundryClaude(env["MCF_FOUNDRY_RESOURCE"], cred, model)
    return None


# ---------------------------------------------------------------- composition

def azure_ports():
    from tower.adapters.http_mcf import HttpMCFTransport
    from tower.adapters.memory import FakeMCF
    from tower.runtime import Ports

    env = os.environ
    cred = _credential()
    from tower.services.clients import ClientDirectory

    secrets = KeyVaultSecrets(env["MCF_KEYVAULT_URL"], cred)
    store = CosmosStore(env["MCF_COSMOS_URL"], env.get("MCF_COSMOS_DB", "mcf"), cred)
    # pilot.yaml's people and clients, plus the clients onboarded through the tower
    directory = ClientDirectory(AppConfigDirectory(env["MCF_APPCONFIG_URL"], cred), store)
    llm = _claude(env, secrets, cred)
    return Ports(
        clock=SystemClock(),
        bus=ServiceBusBus(env["MCF_SERVICEBUS_NAMESPACE"], cred),
        store=store,
        blobs=AzureBlobStore(env["MCF_BLOB_URL"], cred),
        ledger=SqlLedger(env["MCF_SQL_SERVER"], env["MCF_SQL_DB"], cred),
        directory=directory,
        notifier=(AcsEmailNotifier(env["MCF_ACS_ENDPOINT"], env["MCF_EMAIL_SENDER"], directory, cred)
                  if env.get("MCF_ACS_ENDPOINT") else LogNotifier()),
        mcf=(HttpMCFTransport(env["MCF_API_BASE_URL"], secrets) if env.get("MCF_API_MODE") == "live" else FakeMCF()),
        llm=llm,
        masking_key=secrets.get_secret("pii-masking-key").encode(),
        secrets=secrets,
        console_url=env.get("MCF_CONSOLE_URL") or None,
        gateway_url=env.get("MCF_GATEWAY_URL") or None,
    )
