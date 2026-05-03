{{- define "audittrace.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "audittrace.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "audittrace.labels" -}}
helm.sh/chart: {{ include "audittrace.name" . }}
app.kubernetes.io/name: {{ include "audittrace.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "audittrace.selectorLabels" -}}
app.kubernetes.io/name: {{ include "audittrace.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Production-secret hygiene gate.

When .Values.global.productionMode is true, refuse to render the chart if
any `secrets.*` field still matches a known dev default. The throwaway
values live in test fixtures + this repository's CI workflow and must not
reach a production cluster.

Add a new entry whenever a new dev default is introduced; the tuple is
(field dotted path, known-dev value).

Called from templates/NOTES.txt so it evaluates on every `helm template`,
`helm install`, and `helm upgrade`.
*/}}
{{- define "audittrace.assertProductionSecrets" -}}
{{- if .Values.global.productionMode -}}
  {{- $dev := dict
      "secrets.postgres.password"    "test-pg-pass"
      "secrets.postgres.appPassword" "test-pg-pass"
      "secrets.summariser.password"  "test-summariser-pw"
      "secrets.chromadb.token"       "test-chroma-token"
      "secrets.redis.password"       "test-redis-pass"
      "secrets.minio.secretKey"      "test-minio-key"
      "secrets.keycloak.adminPassword" "admin"
  -}}
  {{- $violations := list -}}
  {{- if eq (default "" .Values.secrets.postgres.password) (get $dev "secrets.postgres.password") -}}
    {{- $violations = append $violations "secrets.postgres.password" -}}
  {{- end -}}
  {{- if eq (default "" .Values.secrets.postgres.appPassword) (get $dev "secrets.postgres.appPassword") -}}
    {{- $violations = append $violations "secrets.postgres.appPassword" -}}
  {{- end -}}
  {{- if eq (default "" .Values.secrets.summariser.password) (get $dev "secrets.summariser.password") -}}
    {{- $violations = append $violations "secrets.summariser.password" -}}
  {{- end -}}
  {{- if eq (default "" .Values.secrets.chromadb.token) (get $dev "secrets.chromadb.token") -}}
    {{- $violations = append $violations "secrets.chromadb.token" -}}
  {{- end -}}
  {{- if eq (default "" .Values.secrets.redis.password) (get $dev "secrets.redis.password") -}}
    {{- $violations = append $violations "secrets.redis.password" -}}
  {{- end -}}
  {{- if eq (default "" .Values.secrets.minio.secretKey) (get $dev "secrets.minio.secretKey") -}}
    {{- $violations = append $violations "secrets.minio.secretKey" -}}
  {{- end -}}
  {{- if eq (default "" .Values.secrets.keycloak.adminPassword) (get $dev "secrets.keycloak.adminPassword") -}}
    {{- $violations = append $violations "secrets.keycloak.adminPassword" -}}
  {{- end -}}
  {{- if gt (len $violations) 0 -}}
    {{- fail (printf "global.productionMode=true but the following credentials still match known dev defaults: %s — rotate them via Vault / SOPS / Sealed Secrets before deploying. See values.yaml SECURITY NOTICE." (join ", " $violations)) -}}
  {{- end -}}
{{- end -}}
{{- end -}}

{{/*
Construct the PostgreSQL connection URL for the app role.
*/}}
{{- define "audittrace.postgresAppUrl" -}}
postgresql+psycopg2://audittrace_app:{{ .Values.secrets.postgres.appPassword }}@{{ .Release.Name }}-postgresql:5432/{{ .Values.postgresql.auth.database }}
{{- end }}

{{/*
PostgreSQL URL for the DEDICATED summariser role (ADR-026 §RLS posture,
ADR-030 §Summariser). When manageRole is on, we provision an
`audittrace_summariser` role with LOGIN NOSUPERUSER BYPASSRLS and
minimum grants (SELECT on interactions, SELECT+INSERT+UPDATE on
sessions, no tool_calls). The URL here must match the role the Job
creates in templates/postgres/job-summariser-role.yaml.

Falls back to the generic Bitnami `audittrace` owner role when
manageRole is disabled — in that mode the operator is responsible
for granting BYPASSRLS manually (or accepting the summariser will
not cross users).
*/}}
{{- define "audittrace.postgresOwnerUrl" -}}
{{- if .Values.memoryServer.summariser.manageRole -}}
postgresql+psycopg2://{{ .Values.memoryServer.summariser.roleName }}:{{ .Values.secrets.summariser.password }}@{{ .Release.Name }}-postgresql:5432/{{ .Values.postgresql.auth.database }}
{{- else -}}
postgresql+psycopg2://{{ .Values.postgresql.auth.username }}:{{ .Values.secrets.postgres.password }}@{{ .Release.Name }}-postgresql:5432/{{ .Values.postgresql.auth.database }}
{{- end -}}
{{- end }}

{{/*
Internal Keycloak issuer URL.
*/}}
{{- define "audittrace.keycloakIssuer" -}}
http://{{ .Release.Name }}-keycloak:8080/realms/audittrace
{{- end }}

{{/*
Keycloak JWKS URL.
*/}}
{{- define "audittrace.keycloakJwksUrl" -}}
http://{{ .Release.Name }}-keycloak:8080/realms/audittrace/protocol/openid-connect/certs
{{- end }}

{{/*
Vault Agent Injector annotations — memory-server (ADR-043 §4).
Emits /vault/secrets/env as shell-sourceable exports for the four
secret-sourced env vars the memory-server consumes (postgres URL,
summariser URL, redis pw, chromadb token, minio secret key).

The {{ "{{" }} ... {{ "}}" }} escape sequences emit Vault Agent
template syntax through Helm without interpolation; the literal
braces survive into the rendered annotation so Vault Agent itself
processes them at sidecar startup.
*/}}
{{- define "audittrace.vaultAnnotations.memoryServer" -}}
vault.hashicorp.com/agent-inject: "true"
vault.hashicorp.com/role: "audittrace-server"
vault.hashicorp.com/agent-inject-status: "update"
vault.hashicorp.com/agent-inject-secret-env: "kv/data/audittrace/postgres/app"
vault.hashicorp.com/agent-inject-template-env: |
  {{ "{{ with secret \"kv/data/audittrace/postgres/app\" }}" }}
  export AUDITTRACE_POSTGRES_URL='postgresql+psycopg2://audittrace_app:{{ "{{ .Data.data.password }}" }}@{{ .Release.Name }}-postgresql:5432/{{ .Values.postgresql.auth.database }}'
  {{ "{{ end }}" }}
  {{ "{{ with secret \"kv/data/audittrace/summariser/db\" }}" }}
  export AUDITTRACE_SUMMARIZER_POSTGRES_URL='postgresql+psycopg2://{{ .Values.memoryServer.summariser.roleName }}:{{ "{{ .Data.data.password }}" }}@{{ .Release.Name }}-postgresql:5432/{{ .Values.postgresql.auth.database }}'
  {{ "{{ end }}" }}
  {{ "{{ with secret \"kv/data/audittrace/redis/main\" }}" }}
  export AUDITTRACE_REDIS_PASSWORD='{{ "{{ .Data.data.password }}" }}'
  {{ "{{ end }}" }}
  {{ "{{ with secret \"kv/data/audittrace/chromadb/main\" }}" }}
  export AUDITTRACE_CHROMA_TOKEN='{{ "{{ .Data.data.token }}" }}'
  {{ "{{ end }}" }}
  {{ "{{ with secret \"kv/data/audittrace/minio/root\" }}" }}
  export AUDITTRACE_MINIO_SECRET_KEY='{{ "{{ .Data.data.secret_key }}" }}'
  {{ "{{ end }}" }}
{{- end }}

{{/*
Vault Agent Injector annotations — Keycloak (ADR-043 §4).
Emits exports for KEYCLOAK_ADMIN_PASSWORD + KC_DB_PASSWORD.
*/}}
{{- define "audittrace.vaultAnnotations.keycloak" -}}
vault.hashicorp.com/agent-inject: "true"
vault.hashicorp.com/role: "keycloak"
vault.hashicorp.com/agent-inject-status: "update"
vault.hashicorp.com/agent-inject-secret-env: "kv/data/audittrace/keycloak/admin"
vault.hashicorp.com/agent-inject-template-env: |
  {{ "{{ with secret \"kv/data/audittrace/keycloak/admin\" }}" }}
  export KEYCLOAK_ADMIN_PASSWORD='{{ "{{ .Data.data.password }}" }}'
  {{ "{{ end }}" }}
  {{ "{{ with secret \"kv/data/audittrace/postgres/app\" }}" }}
  export KC_DB_PASSWORD='{{ "{{ .Data.data.password }}" }}'
  {{ "{{ end }}" }}
{{- end }}

{{/*
Vault Agent Injector annotations — MinIO (ADR-043 §4).
Emits exports for MINIO_ROOT_PASSWORD + MINIO_KMS_SECRET_KEY.
*/}}
{{- define "audittrace.vaultAnnotations.minio" -}}
vault.hashicorp.com/agent-inject: "true"
vault.hashicorp.com/role: "minio"
vault.hashicorp.com/agent-inject-status: "update"
vault.hashicorp.com/agent-inject-secret-env: "kv/data/audittrace/minio/root"
vault.hashicorp.com/agent-inject-template-env: |
  {{ "{{ with secret \"kv/data/audittrace/minio/root\" }}" }}
  export MINIO_ROOT_PASSWORD='{{ "{{ .Data.data.secret_key }}" }}'
  export MINIO_KMS_SECRET_KEY='audittrace-key:{{ "{{ .Data.data.kms_master_key }}" }}'
  {{ "{{ end }}" }}
{{- end }}

{{/*
Vault Agent Injector annotations — RLS integration test Pod (ADR-043 §4).
Emits export for POSTGRES_SUPERUSER_PASSWORD sourced from
kv/audittrace/postgres/superuser. The Pod reuses the audittrace-server
Vault role since that policy already grants read on postgres/*.
*/}}
{{- define "audittrace.vaultAnnotations.tests" -}}
vault.hashicorp.com/agent-inject: "true"
vault.hashicorp.com/role: "audittrace-server"
vault.hashicorp.com/agent-inject-status: "update"
# Run as init container only — no long-lived sidecar renewal. The test
# Pod is short-lived; once the tests finish we want vault-agent to exit
# so the Pod can terminate cleanly. Without this `helm test` hangs at
# 2/3 NotReady forever.
vault.hashicorp.com/agent-pre-populate-only: "true"
vault.hashicorp.com/agent-inject-secret-env: "kv/data/audittrace/postgres/superuser"
vault.hashicorp.com/agent-inject-template-env: |
  {{ "{{ with secret \"kv/data/audittrace/postgres/superuser\" }}" }}
  export POSTGRES_SUPERUSER_PASSWORD='{{ "{{ .Data.data.password }}" }}'
  {{ "{{ end }}" }}
{{- end }}

{{/*
Vault Agent Injector annotations — ChromaDB (ADR-043 §4).
Emits export for CHROMA_SERVER_AUTHN_CREDENTIALS sourced from
kv/audittrace/chromadb/main.token.
*/}}
{{- define "audittrace.vaultAnnotations.chromadb" -}}
vault.hashicorp.com/agent-inject: "true"
vault.hashicorp.com/role: "chromadb"
vault.hashicorp.com/agent-inject-status: "update"
vault.hashicorp.com/agent-inject-secret-env: "kv/data/audittrace/chromadb/main"
vault.hashicorp.com/agent-inject-template-env: |
  {{ "{{ with secret \"kv/data/audittrace/chromadb/main\" }}" }}
  export CHROMA_SERVER_AUTHN_CREDENTIALS='{{ "{{ .Data.data.token }}" }}'
  {{ "{{ end }}" }}
{{- end }}

{{/*
Vault Agent secret-source guard — emit before any `set -a; . /vault/secrets/env`.

If the Vault Agent injector failed to add the sidecar (e.g. transient
TLS handshake failure during pod admission — observed 2026-05-03), the
inline `. /vault/secrets/env` blows up with a cryptic "No such file"
error. This guard makes the failure mode unambiguous: exit 79 with a
diagnostic that tells the operator EXACTLY what went wrong and how to
recover. See scripts/deploy-preflight.sh for the pre-deploy gate that
should catch this BEFORE any pod is created.

Use inside `args: [- |]` blocks, BEFORE the `set -a; . /vault/secrets/env`
line, so the guard runs before the source attempt.
*/}}
{{- define "audittrace.vaultSecretFileGuard" -}}
if [ ! -f /vault/secrets/env ]; then
  echo "==============================================================" >&2
  echo "ERROR: Vault Agent did not inject /vault/secrets/env (exit 79)" >&2
  echo "==============================================================" >&2
  echo "The Vault Agent injector failed to attach its sidecar to this" >&2
  echo "pod, so no secret file was rendered. Most common cause is a" >&2
  echo "transient TLS handshake failure between the kube-apiserver and" >&2
  echo "the injector webhook (CA bundle drift in the auto-tls path)." >&2
  echo "" >&2
  echo "Diagnose:" >&2
  echo "  kubectl logs -n audittrace -l app.kubernetes.io/name=vault-agent-injector \\" >&2
  echo "    | grep -i 'tls handshake error'" >&2
  echo "" >&2
  echo "Recover:" >&2
  echo "  kubectl delete pod \"\$HOSTNAME\" -n audittrace" >&2
  echo "  # deployment will recreate via the now-healthy injector." >&2
  echo "" >&2
  echo "Prevent: scripts/deploy-preflight.sh runs a synthetic-pod" >&2
  echo "injection probe before deploys; ensure k8s-rolling-image / " >&2
  echo "k8s-upgrade depend on it." >&2
  exit 79
fi
{{- end }}

{{/*
Vault Agent Injector annotations — summariser-role-creation Job (ADR-043 §4).
Emits exports for PGPASSWORD + SUMMARISER_PASSWORD. Bound to a 1h-TTL
role so the Job's identity is short-lived.
*/}}
{{- define "audittrace.vaultAnnotations.summariserJob" -}}
vault.hashicorp.com/agent-inject: "true"
vault.hashicorp.com/role: "summariser-job"
vault.hashicorp.com/agent-inject-status: "update"
# Run vault-agent as init container only — no long-lived sidecar renewal.
# The Job is short-lived (renders the role, exits). Without this the
# vault-agent SIDECAR keeps the Pod at 2/3 NotReady forever and the
# Helm post-upgrade hook reports `failed: context deadline exceeded`
# (the 2026-05-03 Phase C.8 root cause). Same pattern as the
# `vaultAnnotations.tests` block above.
vault.hashicorp.com/agent-pre-populate-only: "true"
vault.hashicorp.com/agent-inject-secret-env: "kv/data/audittrace/summariser/db"
vault.hashicorp.com/agent-inject-template-env: |
  {{ "{{ with secret \"kv/data/audittrace/postgres/superuser\" }}" }}
  export PGPASSWORD='{{ "{{ .Data.data.password }}" }}'
  {{ "{{ end }}" }}
  {{ "{{ with secret \"kv/data/audittrace/summariser/db\" }}" }}
  export SUMMARISER_PASSWORD='{{ "{{ .Data.data.password }}" }}'
  {{ "{{ end }}" }}
{{- end }}
