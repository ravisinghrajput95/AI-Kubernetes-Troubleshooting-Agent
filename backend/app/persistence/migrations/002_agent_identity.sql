-- Agent identity: the state that makes a bootstrap token single-use and a
-- certificate revocable.

-- Enrolment credentials, stored as SHA-256 digests and never in the clear.
-- A dump of this table yields nothing that can enrol an agent.
CREATE TABLE IF NOT EXISTS agent_bootstrap_tokens (
    token_hash   text PRIMARY KEY,
    -- The cluster this token may enrol, fixed at issue. The registering agent
    -- does not get to choose its own name; this column is the name.
    cluster_id   text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    -- Single-use lives here. Spending is a conditional UPDATE against
    -- `consumed_at IS NULL`, which is the same mutual exclusion the job claim
    -- uses: many workers may try, exactly one UPDATE matches.
    consumed_at  timestamptz
);

-- Listing tokens for a cluster during onboarding.
CREATE INDEX IF NOT EXISTS agent_bootstrap_tokens_cluster_idx
    ON agent_bootstrap_tokens (cluster_id, created_at DESC);

-- Every certificate the platform has issued. Kept after expiry so an audit can
-- answer "what identity was live on this date", which a table pruned to the
-- currently-valid set cannot.
CREATE TABLE IF NOT EXISTS agent_certificates (
    -- Lowercase hex of the x509 serial, which is what the gateway reads off a
    -- peer certificate.
    serial          text PRIMARY KEY,
    cluster_id      text NOT NULL,
    issued_at       timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    revoked_at      timestamptz,
    revoked_reason  text NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS agent_certificates_cluster_idx
    ON agent_certificates (cluster_id, issued_at DESC);

-- The gateway's revocation check, and the sweeper's. Partial, because the
-- revoked set is expected to stay a small fraction of the whole.
CREATE INDEX IF NOT EXISTS agent_certificates_revoked_idx
    ON agent_certificates (serial)
    WHERE revoked_at IS NOT NULL;
