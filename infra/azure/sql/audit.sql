-- Audit_Service storage (R10). Run against database mcf-audit as a member of the Entra admin group.
-- Idempotent: safe to re-run on every deploy. sqlcmd variables: AGENTS_IDENTITY_NAME, AGENTS_CLIENT_ID.
-- APPEND_ONLY ledger tables: the engine rejects UPDATE and DELETE for every principal (R10.5, R10.7),
-- and ledger digests are exported to the immutable 'audit-digests' blob container for tamper evidence.
IF SCHEMA_ID('audit') IS NULL EXEC('CREATE SCHEMA audit');
GO
IF OBJECT_ID('audit.records') IS NULL
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
IF OBJECT_ID('audit.record_refs') IS NULL
CREATE TABLE audit.record_refs (
    record_id NVARCHAR(64)  NOT NULL,
    ref_type  NVARCHAR(8)   NOT NULL,     -- order | sku | node
    ref_value NVARCHAR(128) NOT NULL
) WITH (LEDGER = ON (APPEND_ONLY = ON));
GO
-- R10.3: query by agent, order, SKU, user or time range in under 30 s
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_records_at' AND object_id = OBJECT_ID('audit.records'))
    CREATE INDEX ix_records_at ON audit.records (at);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_records_actor_at' AND object_id = OBJECT_ID('audit.records'))
    CREATE INDEX ix_records_actor_at ON audit.records (actor, at);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_records_id' AND object_id = OBJECT_ID('audit.records'))
    CREATE INDEX ix_records_id ON audit.records (record_id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_refs_lookup' AND object_id = OBJECT_ID('audit.record_refs'))
    CREATE INDEX ix_refs_lookup ON audit.record_refs (ref_type, ref_value) INCLUDE (record_id);
GO
-- Agents' managed identity: INSERT and SELECT only. Created by client id (SID) so the deploying
-- principal does not need Directory Readers in Entra ID.
IF DATABASE_PRINCIPAL_ID('$(AGENTS_IDENTITY_NAME)') IS NULL
    CREATE USER [$(AGENTS_IDENTITY_NAME)]
        WITH SID = CAST(CAST('$(AGENTS_CLIENT_ID)' AS UNIQUEIDENTIFIER) AS VARBINARY(16)), TYPE = E;
GRANT INSERT, SELECT ON SCHEMA::audit TO [$(AGENTS_IDENTITY_NAME)];
GO
