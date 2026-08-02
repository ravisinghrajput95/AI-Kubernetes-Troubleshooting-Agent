from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    service_name: str = "ai-kubernetes-agent"
    cors_origins: list[str] = ["http://localhost:3000"]

    openai_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("OPENAI_API_KEY", "OPENAI"),
    )
    openai_model: str = Field(
        default="gpt-4o-mini",
        validation_alias=AliasChoices("OPENAI_MODEL", "OPENAI-MODEL"),
    )
    kubeconfig_path: str = Field(default="", validation_alias="KUBECONFIG_PATH")
    kubectl_timeout_seconds: int = 30
    llm_timeout_seconds: int = 45

    # --- Collection limits --------------------------------------------------
    # Page size the API server serves list requests in. Lower values reduce
    # apiserver and etcd memory pressure on large clusters at the cost of more
    # round trips.
    kubectl_chunk_size: int = Field(default=500, validation_alias="KUBECTL_CHUNK_SIZE")
    # Hard cap on list items retained from any single read. A cluster with more
    # objects than this is investigated from a partial view, and the truncation
    # is recorded as evidence rather than applied silently.
    max_list_items: int = Field(default=2000, validation_alias="MAX_LIST_ITEMS")

    # Optional observability backends. Empty means "not deployed": the
    # collectors record unavailable evidence rather than failing, so an
    # investigation degrades instead of breaking.
    prometheus_url: str = Field(default="", validation_alias="PROMETHEUS_URL")
    loki_url: str = Field(default="", validation_alias="LOKI_URL")
    observability_timeout_seconds: int = 15
    metrics_lookback_minutes: int = 60

    # --- Authentication -----------------------------------------------------
    # "oidc" for production, "token" for simple deployments, "disabled" only for
    # local development. `disabled` is refused unless explicitly acknowledged,
    # because this service holds a kubeconfig.
    auth_mode: str = Field(default="disabled", validation_alias="AUTH_MODE")
    allow_insecure_no_auth: bool = Field(default=False, validation_alias="ALLOW_INSECURE_NO_AUTH")

    oidc_issuer: str = Field(default="", validation_alias="OIDC_ISSUER")
    oidc_audience: str = Field(default="", validation_alias="OIDC_AUDIENCE")
    oidc_jwks_url: str = Field(default="", validation_alias="OIDC_JWKS_URL")
    oidc_username_claim: str = Field(default="email", validation_alias="OIDC_USERNAME_CLAIM")
    oidc_groups_claim: str = Field(default="groups", validation_alias="OIDC_GROUPS_CLAIM")
    # Empty means one tenant for everyone. Set it and the claim decides.
    oidc_tenant_claim: str = Field(default="", validation_alias="OIDC_TENANT_CLAIM")

    # `token:subject:group1,group2` entries, comma separated.
    api_tokens: str = Field(default="", validation_alias="API_TOKENS")

    # --- Authorisation ------------------------------------------------------
    # `group=role` pairs, so the customer's IdP drives platform roles and there
    # is no second directory to maintain. Groups already ride on `Principal`;
    # before this they were used only for Kubernetes impersonation.
    oidc_role_mappings: str = Field(default="", validation_alias="OIDC_ROLE_MAPPINGS")

    # What a caller with no binding and no matching group gets.
    #
    # `admin` is the default because it is *today's behaviour exactly*: before
    # roles existed, every authenticated caller in a single-tenant deployment
    # could do everything. Preserving that is what keeps the single-tenant
    # deployment supported rather than something you have to administer your
    # way back into. Set `viewer` (or `none`) and start assigning roles to get
    # real RBAC in a single-tenant install.
    #
    # In `shared` mode this is refused above `viewer` — see `validate_authz()`.
    rbac_default_role: str = Field(default="admin", validation_alias="RBAC_DEFAULT_ROLE")

    # Where file-backed role bindings live when there is no DATABASE_URL.
    # Relative, like the report store and the agent identity directory.
    rbac_store_dir: str = Field(default="data/rbac", validation_alias="RBAC_STORE_DIR")

    def validate_authz(self) -> None:
        """Refuse an authorisation configuration that cannot mean what it says.

        The one that matters is a permissive default in a multi-tenant
        deployment: `RBAC_DEFAULT_ROLE=admin` plus `TENANCY_MODE=shared` means
        anyone the IdP can place in a tenant administers it, which is the
        failure this milestone exists to remove rather than relocate.
        """
        from app.authz.models import Role

        raw = (self.rbac_default_role or "").strip().lower()
        fallback = None if raw in {"", "none"} else Role.parse(raw)

        if fallback is not None and self.multi_tenant and fallback.rank > Role.VIEWER.rank:
            raise RuntimeError(
                f"RBAC_DEFAULT_ROLE={self.rbac_default_role!r} grants "
                f"{fallback} to every caller with no explicit role, and "
                f"TENANCY_MODE=shared means that is every caller the identity "
                f"provider can place in any tenant. Use 'viewer' or 'none' and "
                f"assign roles with OIDC_ROLE_MAPPINGS or `python -m app.rbacctl`."
            )

        # Parsed at startup so a malformed mapping is a refused boot rather
        # than a customer whose admins are silently viewers.
        from app.authz.resolver import parse_role_mappings

        parse_role_mappings(self.oidc_role_mappings)

    # --- Kubernetes impersonation ------------------------------------------
    # With impersonation on, every cluster read runs as the calling user, so the
    # user's own RBAC applies rather than the service account's. This is what
    # stops an authenticated user reading everything the kubeconfig can reach.
    impersonate_users: bool = Field(default=True, validation_alias="IMPERSONATE_USERS")

    audit_log_path: str = Field(default="", validation_alias="AUDIT_LOG_PATH")

    # How long rendered reports are kept. Investigations accumulate faster than
    # anyone reads them; the record that one happened is cheap and stays, the
    # PDF/JSON/Markdown are pruned. 0 disables pruning entirely.
    report_retention_days: int = Field(default=14, validation_alias="REPORT_RETENTION_DAYS")
    # How often the sweep runs. Retention is a floor, not a deadline: a report
    # may outlive it by up to one interval.
    report_retention_sweep_hours: float = Field(
        default=6.0, validation_alias="REPORT_RETENTION_SWEEP_HOURS"
    )

    # --- Tenancy ------------------------------------------------------------
    # `single` is the default and the supported single-tenant deployment
    # (ADR-006): one implicit tenant, nothing to isolate, no behaviour change.
    # `shared` puts a tenant on every row and enforces it with row-level
    # security — which needs Postgres, so it is refused without one rather than
    # half-honoured.
    tenancy_mode: str = Field(default="single", validation_alias="TENANCY_MODE")

    @property
    def multi_tenant(self) -> bool:
        return self.tenancy_mode.strip().lower() == "shared"

    def validate_tenancy(self) -> None:
        mode = self.tenancy_mode.strip().lower()
        if mode not in {"single", "shared"}:
            raise RuntimeError(
                f"TENANCY_MODE={self.tenancy_mode!r} is not a mode. Use 'single' "
                f"(the default) or 'shared'."
            )
        if mode == "shared" and not self.database_url:
            raise RuntimeError(
                "TENANCY_MODE=shared requires DATABASE_URL. Tenant isolation is "
                "enforced by Postgres row-level security; there is no in-memory "
                "equivalent, and a multi-tenant deployment whose isolation is a "
                "Python conditional is worse than one that refused to start."
            )
        if mode == "shared" and self.auth_mode.strip().lower() == "disabled":
            raise RuntimeError(
                "TENANCY_MODE=shared requires authentication. Without it every "
                "caller is anonymous, so every caller is the same tenant and "
                "the isolation is decorative."
            )

    # --- Distributed state --------------------------------------------------
    # Both unset is the supported single-process default: jobs and reports stay
    # in this process and on local disk, and no infrastructure is required.
    # Both set moves state to Postgres and Redis, which is what makes a
    # multi-worker deployment safe. Exactly one set is refused at startup —
    # a half-configured deployment that silently loses jobs is the failure this
    # exists to remove.
    database_url: str = Field(default="", validation_alias="DATABASE_URL")
    redis_url: str = Field(default="", validation_alias="REDIS_URL")
    # Namespaces every Redis key, so two deployments can share one Redis.
    redis_key_prefix: str = Field(default="k8sagent", validation_alias="REDIS_KEY_PREFIX")

    # How long a worker's claim on a job stays valid without a heartbeat. A
    # worker that dies leaves its job reapable after this long, not forever.
    job_lease_seconds: int = Field(default=60, validation_alias="JOB_LEASE_SECONDS")
    # Backstop poll for a cancellation whose pub/sub message never arrived.
    # Redis carries the cancel in milliseconds; this bounds the worst case.
    job_cancel_poll_seconds: float = Field(default=2.0, validation_alias="JOB_CANCEL_POLL_SECONDS")
    # Identifies this worker in leases. Defaults to host:pid at startup.
    worker_id: str = Field(default="", validation_alias="WORKER_ID")

    # How many investigations one worker runs at once.
    #
    # This was a hardcoded 4 with no way to change it, which made it — not
    # memory — the platform's concurrency ceiling: reaching the roadmap's 5,000
    # concurrent investigations would have needed 1,250 workers.
    #
    # Measured cost per concurrent investigation (`scripts/payload_bench.py`,
    # `--memory`): peak heap is about 5x the stored result, so 13.4 MB at the
    # `MAX_LIST_ITEMS` ceiling of 2,000 pods and under 4 MB on a small cluster.
    # A worker with 2 GB to spare can hold roughly 100.
    #
    # The default stays 4 deliberately. Memory is not the only cost — collection
    # and analysis both occupy worker threads, and anyio's default thread pool
    # is 40 — so raising this is an operator's decision made against their own
    # cluster sizes, not one to inherit from a changed default.
    job_max_concurrent: int = Field(default=4, ge=1, validation_alias="JOB_MAX_CONCURRENT")

    # --- Cluster agents -----------------------------------------------------
    # 0 disables the gateway entirely, which is the default: an agent is opt-in
    # and the local kubeconfig path needs none of this.
    agent_gateway_port: int = Field(default=0, validation_alias="AGENT_GATEWAY_PORT")

    # Where an agent that has no certificate yet exchanges its bootstrap token
    # for one. A separate listener because it is the only surface an
    # unauthenticated peer may reach — a fleet that has finished enrolling can
    # firewall this off and lose nothing but the ability to add clusters.
    # 0 means one above the gateway port.
    agent_enrolment_port: int = Field(default=0, validation_alias="AGENT_ENROLMENT_PORT")

    # What the gateway tells a newly-enrolled agent to dial for Connect
    # (`RegistrationResponse.gateway_endpoint`), which need not be the address
    # it registered against. Empty means the agent keeps using what it was
    # given on the command line.
    agent_gateway_advertise: str = Field(default="", validation_alias="AGENT_GATEWAY_ADVERTISE")

    # How agents authenticate. `mtls` is the default and the only mode in which
    # the platform knows who it is talking to: identity comes from the peer
    # certificate, and `AgentHello` cannot override it.
    #
    # `disabled` is the M4a behaviour kept as an explicit, logged opt-in for
    # local development — plaintext, a shared bootstrap token in metadata, and
    # a cluster id the agent asserts about itself. Same discipline as the
    # single-process job store: supported, chosen deliberately, and never
    # arrived at by accident.
    agent_gateway_tls: str = Field(default="mtls", validation_alias="AGENT_GATEWAY_TLS")

    # Shared secret for `disabled` mode only. In `mtls` mode enrolment uses
    # single-use tokens from `agentctl` and this is ignored.
    agent_bootstrap_token: str = Field(default="", validation_alias="AGENT_BOOTSTRAP_TOKEN")

    # The SPIFFE trust domain agent identities are named in:
    # `spiffe://<domain>/cluster/<cluster-id>`.
    agent_trust_domain: str = Field(
        default="k8s-agent.local", validation_alias="AGENT_TRUST_DOMAIN"
    )

    # Where a generated development CA and the file-backed enrolment state
    # live. Relative, like the report store, so the backend must be started
    # from `backend/` — with DATABASE_URL set, enrolment state moves to
    # Postgres and only the CA remains on disk.
    agent_identity_dir: str = Field(
        default="data/agent_identity", validation_alias="AGENT_IDENTITY_DIR"
    )
    # Supply both to use a CA you control. Left empty, the gateway generates a
    # development CA under `agent_identity_dir` and says so, loudly.
    agent_ca_cert_file: str = Field(default="", validation_alias="AGENT_CA_CERT_FILE")
    agent_ca_key_file: str = Field(default="", validation_alias="AGENT_CA_KEY_FILE")

    # Names the gateway's own TLS certificate is issued for — whatever agents
    # dial it by. The leaf is generated at startup and chains to the CA agents
    # already trust, so this list is the only thing that needs to change when
    # the gateway moves.
    agent_gateway_dns_names: str = Field(
        default="localhost", validation_alias="AGENT_GATEWAY_DNS_NAMES"
    )
    agent_gateway_ip_addresses: str = Field(
        default="127.0.0.1,::1", validation_alias="AGENT_GATEWAY_IP_ADDRESSES"
    )

    # The image the onboarding manifest installs.
    #
    # Configurable because a customer running an air-gapped or mirrored
    # registry cannot pull from GHCR, and the manifest is useless to them if
    # the reference is baked in. The default tracks `main`; a published
    # release moves it to a version, and pinning one is the right thing to do
    # for a fleet.
    agent_image: str = Field(
        default="ghcr.io/ravisinghrajput95/k8s-ops-agent:edge",
        validation_alias="AGENT_IMAGE",
    )

    # Issued certificate life, per ADR-005. Agents renew at 2/3 of it, so the
    # default leaves a 30-day overlap window.
    agent_cert_ttl_hours: int = Field(default=24 * 90, validation_alias="AGENT_CERT_TTL_HOURS")

    # How often the gateway re-reads the revocation list and drops any live
    # stream whose certificate has since been revoked. Revocation that only
    # took effect at reconnect would mean nothing against a stream designed to
    # stay open for weeks.
    agent_revocation_sweep_seconds: float = Field(
        default=30.0, validation_alias="AGENT_REVOCATION_SWEEP_SECONDS"
    )

    @property
    def agent_gateway_enabled(self) -> bool:
        return self.agent_gateway_port > 0

    @property
    def agent_mtls_enabled(self) -> bool:
        return self.agent_gateway_tls.strip().lower() != "disabled"

    def validate_agent_gateway(self) -> None:
        """Refuse a TLS mode that is neither of the two supported ones.

        A typo in `AGENT_GATEWAY_TLS` must not fall through to plaintext. This
        is the one setting where being wrong quietly is a security hole.
        """
        mode = self.agent_gateway_tls.strip().lower()
        if mode not in {"mtls", "disabled"}:
            raise RuntimeError(
                f"AGENT_GATEWAY_TLS={self.agent_gateway_tls!r} is not a mode. "
                f"Use 'mtls' (the default) or 'disabled' for local development."
            )
        if bool(self.agent_ca_cert_file) != bool(self.agent_ca_key_file):
            missing = "AGENT_CA_KEY_FILE" if self.agent_ca_cert_file else "AGENT_CA_CERT_FILE"
            raise RuntimeError(
                f"Only one half of the agent CA is configured; {missing} is also required."
            )

    @property
    def agent_ca_paths(self) -> tuple[Path, Path]:
        """Where the CA is, configured or defaulted."""
        if self.agent_ca_cert_file and self.agent_ca_key_file:
            return Path(self.agent_ca_cert_file), Path(self.agent_ca_key_file)
        base = Path(self.agent_identity_dir)
        return base / "ca.crt", base / "ca.key"

    @property
    def distributed_state(self) -> bool:
        return bool(self.database_url and self.redis_url)

    def validate_state_backend(self) -> None:
        """Refuse a half-configured distributed deployment.

        Starting with only one of the two would appear to work and would lose
        every job the moment a second worker existed.
        """
        if bool(self.database_url) is bool(self.redis_url):
            return
        missing = "REDIS_URL" if self.database_url else "DATABASE_URL"
        present = "DATABASE_URL" if self.database_url else "REDIS_URL"
        raise RuntimeError(
            f"{present} is set but {missing} is not. Distributed state needs "
            f"both; set {missing}, or unset {present} to run single-process."
        )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
