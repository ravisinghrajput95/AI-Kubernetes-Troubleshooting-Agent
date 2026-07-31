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

    # `token:subject:group1,group2` entries, comma separated.
    api_tokens: str = Field(default="", validation_alias="API_TOKENS")

    # --- Kubernetes impersonation ------------------------------------------
    # With impersonation on, every cluster read runs as the calling user, so the
    # user's own RBAC applies rather than the service account's. This is what
    # stops an authenticated user reading everything the kubeconfig can reach.
    impersonate_users: bool = Field(default=True, validation_alias="IMPERSONATE_USERS")

    audit_log_path: str = Field(default="", validation_alias="AUDIT_LOG_PATH")

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
