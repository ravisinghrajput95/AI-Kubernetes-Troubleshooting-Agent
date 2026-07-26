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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
