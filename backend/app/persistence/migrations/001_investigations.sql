-- Durable investigation state: the job record, its progress events, and the
-- rendered reports. Every row an operator can address by id lives here.

CREATE TABLE IF NOT EXISTS investigations (
    id                text PRIMARY KEY,
    -- Subject of the submitting caller. Empty when auth is disabled.
    owner             text NOT NULL DEFAULT '',
    -- Serialised Principal, so a worker that did not receive the request can
    -- still impersonate the original caller against the cluster.
    principal         jsonb,
    status            text NOT NULL DEFAULT 'pending',
    request           jsonb NOT NULL DEFAULT '{}'::jsonb,
    result            jsonb,
    error             text NOT NULL DEFAULT '',
    -- The durable half of cancellation. The Redis message that announces a
    -- cancel is an optimisation; this column is what makes it a guarantee.
    cancel_requested  boolean NOT NULL DEFAULT false,
    -- Claim held by the worker running this job. An expired lease means the
    -- worker died, and the row is reapable.
    lease_worker      text,
    lease_expires_at  timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    started_at        timestamptz,
    finished_at       timestamptz,
    -- The history projection, stored whole rather than re-derived. The API
    -- returns this dict verbatim, so keeping it intact means a query change
    -- cannot drift the response shape.
    history_item      jsonb
);

-- History listing: newest first, per owner, only rows that have a report.
CREATE INDEX IF NOT EXISTS investigations_owner_created_idx
    ON investigations (owner, created_at DESC)
    WHERE history_item IS NOT NULL;

-- The reaper's scan: unfinished work whose lease has run out.
CREATE INDEX IF NOT EXISTS investigations_lease_idx
    ON investigations (status, lease_expires_at);

CREATE TABLE IF NOT EXISTS investigation_events (
    -- Assigned by Postgres, so ordering does not depend on clocks agreeing
    -- across workers. This is the cursor an SSE client resumes from.
    seq              bigserial PRIMARY KEY,
    investigation_id text NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    type             text NOT NULL,
    message          text NOT NULL DEFAULT '',
    data             jsonb NOT NULL DEFAULT '{}'::jsonb,
    at               timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS investigation_events_stream_idx
    ON investigation_events (investigation_id, seq);

-- Rendered reports as blobs. A PDF here is tens of kilobytes; M8 moves these
-- to object storage, which changes the read method and no endpoint.
CREATE TABLE IF NOT EXISTS investigation_reports (
    investigation_id text NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    format           text NOT NULL,
    content          bytea NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (investigation_id, format)
);
