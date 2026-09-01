{{/*
Refuse an unusable configuration at `helm install`, not at CrashLoopBackOff.

Every check here mirrors a refusal the platform already makes at startup. The
platform's refusals are the real control — these do not replace them, and must
never be more permissive than they are. What they buy is the *timing*: a chart
that renders successfully and then crashloops tells you something is wrong
through a pod status and a log line, while `helm install` can tell you which
value to set before anything is deployed.

The one thing this deliberately does NOT do is supply a default that makes an
insecure configuration work. `docker-compose.yml` pre-set ALLOW_INSECURE_NO_AUTH
once and that was the "careless deployment" the security notes warn about,
shipped in the repository. A chart that quietly acknowledges it for you is the
same mistake with a longer reach.
*/}}
{{- define "k8s-agent.validate" -}}

{{- $mode := .Values.auth.mode -}}
{{- if not $mode -}}
{{- fail "auth.mode is not set and this service holds a kubeconfig, so the chart will not choose for you. Set auth.mode=oidc (with auth.oidc.issuer and audience), auth.mode=token (with auth.tokensSecret.name), or auth.mode=disabled together with auth.allowInsecureNoAuth=true for a throwaway environment. Read SECURITY.md first." -}}
{{- end -}}
{{- if not (has $mode (list "oidc" "token" "disabled")) -}}
{{- fail (printf "auth.mode must be one of oidc|token|disabled, got %q" $mode) -}}
{{- end -}}

{{- if eq $mode "oidc" -}}
  {{- if not .Values.auth.oidc.issuer -}}
    {{- fail "auth.mode=oidc requires auth.oidc.issuer" -}}
  {{- end -}}
  {{- if not .Values.auth.oidc.audience -}}
    {{- fail "auth.mode=oidc requires auth.oidc.audience" -}}
  {{- end -}}
{{- end -}}

{{- if eq $mode "token" -}}
  {{- if not .Values.auth.tokensSecret.name -}}
    {{- fail "auth.mode=token requires auth.tokensSecret.name (never set tokens inline in values)" -}}
  {{- end -}}
{{- end -}}

{{- if eq $mode "disabled" -}}
  {{- if not .Values.auth.allowInsecureNoAuth -}}
    {{- fail "auth.mode=disabled holds a kubeconfig and authenticates nobody. Set auth.allowInsecureNoAuth=true to acknowledge, or use oidc/token. Read SECURITY.md first." -}}
  {{- end -}}
{{- end -}}

{{/* Both or neither. Exactly one is refused by the platform at startup. */}}
{{- $db := .Values.database.urlSecret.name -}}
{{- $redis := .Values.redis.urlSecret.name -}}
{{- if and $db (not $redis) -}}
  {{- fail "database.urlSecret.name is set but redis.urlSecret.name is not. Set both or neither." -}}
{{- end -}}
{{- if and $redis (not $db) -}}
  {{- fail "redis.urlSecret.name is set but database.urlSecret.name is not. Set both or neither." -}}
{{- end -}}

{{/*
A multi-replica deployment with no shared state is the quiet failure this
catches: every worker keeps its jobs in its own memory, so whether a poll finds
the investigation depends on which pod the load balancer picked.
*/}}
{{- if and (gt (int .Values.replicaCount) 1) (not $db) -}}
  {{- fail "replicaCount > 1 needs shared state: set database.urlSecret.name and redis.urlSecret.name, or set replicaCount: 1." -}}
{{- end -}}
{{- if and .Values.autoscaling.enabled (not $db) -}}
  {{- fail "autoscaling.enabled needs shared state: set database.urlSecret.name and redis.urlSecret.name." -}}
{{- end -}}

{{- if eq .Values.tenancy.mode "shared" -}}
  {{- if not $db -}}
    {{- fail "tenancy.mode=shared requires a database: there is no in-memory equivalent of row-level security." -}}
  {{- end -}}
  {{- if eq $mode "disabled" -}}
    {{- fail "tenancy.mode=shared requires authentication: every caller being anonymous means every caller is the same tenant." -}}
  {{- end -}}
  {{- if not (has .Values.rbac.defaultRole (list "viewer" "none" "")) -}}
    {{- fail (printf "tenancy.mode=shared refuses rbac.defaultRole=%q above viewer: a permissive default means anyone the IdP can place in a tenant administers it." .Values.rbac.defaultRole) -}}
  {{- end -}}
  {{- if and (eq $mode "oidc") (not .Values.auth.oidc.tenantClaim) -}}
    {{- fail "tenancy.mode=shared with auth.mode=oidc requires auth.oidc.tenantClaim, or every tenant lands in `default`." -}}
  {{- end -}}
{{- end -}}

{{- if not (has .Values.tenancy.mode (list "single" "shared")) -}}
  {{- fail (printf "tenancy.mode must be single or shared, got %q" .Values.tenancy.mode) -}}
{{- end -}}

{{/*
A warning rather than a refusal, matching the platform: an unset tenant quota in
shared mode is a fairness gap, not an unsafe configuration.
*/}}
{{- if and (eq .Values.tenancy.mode "shared") (eq (int .Values.config.rateLimitTenantPerMinute) 0) -}}
{{- printf "\nWARNING: tenancy.mode=shared with config.rateLimitTenantPerMinute=0 — one tenant can consume the whole platform's budget.\n" | print -}}
{{- end -}}

{{- end -}}
