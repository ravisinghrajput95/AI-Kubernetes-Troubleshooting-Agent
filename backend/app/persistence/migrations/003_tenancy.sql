-- Tenant isolation, enforced by the database rather than by every query.
--
-- The design rule from ADR-006 is "tenant id on every row, enforced in the
-- data layer, not in handlers". This file is what makes that literally true:
--
--   * `tenant_id` defaults to `current_setting('app.current_tenant')`, so an
--     INSERT that never mentions a tenant is still stamped with one. No store
--     method changed to support this.
--   * Row-level security compares `tenant_id` to the same setting, so a SELECT
--     with no WHERE clause returns only the caller's rows.
--   * FORCE ROW LEVEL SECURITY, because the application connects as the table
--     owner and owners bypass RLS by default. Without FORCE these policies
--     would exist, read correctly, and do nothing — which is the worst
--     possible state for a security control.
--
-- The `'*'` escape is the queue consumer: claiming and reaping cannot know a
-- tenant before reading the row that names one. It is set only by
-- `app.tenancy.system_scope()`.

-- Added nullable first, then backfilled, then constrained.
--
-- `ADD COLUMN ... NOT NULL DEFAULT current_setting(...)` looks tidier and would
-- fail on any table that already has rows: the default is evaluated once to
-- fill them, no `app.current_tenant` is set during a migration, and NULL
-- violates the constraint. Empty tables hide it, which is exactly how this
-- would have reached a customer upgrade and nowhere else.
ALTER TABLE investigations
    ADD COLUMN IF NOT EXISTS tenant_id text
    DEFAULT current_setting('app.current_tenant', true);

ALTER TABLE investigation_events
    ADD COLUMN IF NOT EXISTS tenant_id text
    DEFAULT current_setting('app.current_tenant', true);

ALTER TABLE investigation_reports
    ADD COLUMN IF NOT EXISTS tenant_id text
    DEFAULT current_setting('app.current_tenant', true);

ALTER TABLE agent_bootstrap_tokens
    ADD COLUMN IF NOT EXISTS tenant_id text
    DEFAULT current_setting('app.current_tenant', true);

ALTER TABLE agent_certificates
    ADD COLUMN IF NOT EXISTS tenant_id text
    DEFAULT current_setting('app.current_tenant', true);

-- Rows written before tenancy belong to the tenant that existed at the time.
UPDATE investigations SET tenant_id = 'default' WHERE tenant_id IS NULL OR tenant_id = '';
UPDATE investigation_events SET tenant_id = 'default' WHERE tenant_id IS NULL OR tenant_id = '';
UPDATE investigation_reports SET tenant_id = 'default' WHERE tenant_id IS NULL OR tenant_id = '';
UPDATE agent_bootstrap_tokens SET tenant_id = 'default' WHERE tenant_id IS NULL OR tenant_id = '';
UPDATE agent_certificates SET tenant_id = 'default' WHERE tenant_id IS NULL OR tenant_id = '';

-- Only now can it be NOT NULL. A row that reached this point without a tenant
-- would be a row no policy can place, so the constraint is the backstop.
ALTER TABLE investigations ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE investigation_events ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE investigation_reports ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE agent_bootstrap_tokens ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE agent_certificates ALTER COLUMN tenant_id SET NOT NULL;

-- Every tenanted lookup leads with the tenant, so it leads in the index too.
CREATE INDEX IF NOT EXISTS investigations_tenant_idx
    ON investigations (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS investigation_events_tenant_idx
    ON investigation_events (tenant_id, investigation_id, seq);
CREATE INDEX IF NOT EXISTS agent_certificates_tenant_idx
    ON agent_certificates (tenant_id, cluster_id);

-- The policy, applied identically to all five tables.
--
-- `current_setting('app.current_tenant', true)` is NULL when nothing set it —
-- a connection used outside a request, or a migration. That case matches no
-- row, which is the correct default for a security control: closed.
CREATE OR REPLACE FUNCTION app_tenant_visible(row_tenant text) RETURNS boolean AS $$
    SELECT current_setting('app.current_tenant', true) = '*'
        OR row_tenant = current_setting('app.current_tenant', true);
$$ LANGUAGE sql STABLE;

ALTER TABLE investigations ENABLE ROW LEVEL SECURITY;
ALTER TABLE investigations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS investigations_tenant ON investigations;
CREATE POLICY investigations_tenant ON investigations
    USING (app_tenant_visible(tenant_id))
    WITH CHECK (app_tenant_visible(tenant_id));

ALTER TABLE investigation_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE investigation_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS investigation_events_tenant ON investigation_events;
CREATE POLICY investigation_events_tenant ON investigation_events
    USING (app_tenant_visible(tenant_id))
    WITH CHECK (app_tenant_visible(tenant_id));

ALTER TABLE investigation_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE investigation_reports FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS investigation_reports_tenant ON investigation_reports;
CREATE POLICY investigation_reports_tenant ON investigation_reports
    USING (app_tenant_visible(tenant_id))
    WITH CHECK (app_tenant_visible(tenant_id));

ALTER TABLE agent_bootstrap_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_bootstrap_tokens FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS agent_bootstrap_tokens_tenant ON agent_bootstrap_tokens;
CREATE POLICY agent_bootstrap_tokens_tenant ON agent_bootstrap_tokens
    USING (app_tenant_visible(tenant_id))
    WITH CHECK (app_tenant_visible(tenant_id));

ALTER TABLE agent_certificates ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_certificates FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS agent_certificates_tenant ON agent_certificates;
CREATE POLICY agent_certificates_tenant ON agent_certificates
    USING (app_tenant_visible(tenant_id))
    WITH CHECK (app_tenant_visible(tenant_id));

-- The tenant registry. Not itself tenanted: it is the list of tenants, and a
-- deployment's operator needs to see all of them.
CREATE TABLE IF NOT EXISTS tenants (
    id          text PRIMARY KEY,
    name        text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);

INSERT INTO tenants (id, name) VALUES ('default', 'Default')
    ON CONFLICT (id) DO NOTHING;
