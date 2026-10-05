-- Audit_Service storage (R10). Run once against database mcf-audit as the Entra admin group.
-- APPEND_ONLY ledger tables: the engine rejects UPDATE and DELETE for every principal (R10.5, R10.7),
-- and ledger digests are exported to the immutable 'audit-digests' blob container for tamper evidence.
CREATE SCHEMA audit;
GO
CREATE TABLE audit.records (
    record_id    NVARCHAR(64)  NOT NULL,
    kind         NVARCHAR(32)  NOT NULL,
    at           DATETIME2(3)  NOT NULL,
    actor        NVARCHAR(256) NOT NULL,
    action       NVARCHAR(128) NOT NULL,
    owning_party NVARCHAR(16)  NULL,
    subject_id   NVARCHAR(64)  NULL,
    body         NVARCHAR(MAX) NOT NULL   -- full AuditRecord JSON
) WITH (LEDGER = ON (APPEND_ONLY = ON));
GO
CREATE TABLE audit.record_refs (
    record_id NVARCHAR(64)  NOT NULL,
    ref_type  NVARCHAR(8)   NOT NULL,     -- order | sku | node
    ref_value NVARCHAR(128) NOT NULL
) WITH (LEDGER = ON (APPEND_ONLY = ON));
GO
-- R10.3: query by agent, order, SKU, user or time range in under 30 s
CREATE INDEX ix_records_at ON audit.records (at);
CREATE INDEX ix_records_actor_at ON audit.records (actor, at);
CREATE INDEX ix_records_id ON audit.records (record_id);
CREATE INDEX ix_refs_lookup ON audit.record_refs (ref_type, ref_value) INCLUDE (record_id);
GO
-- Agents' managed identity: INSERT and SELECT only.
CREATE USER [mcfpilot-agents-id] FROM EXTERNAL PROVIDER;
GRANT INSERT, SELECT ON SCHEMA::audit TO [mcfpilot-agents-id];
GO
