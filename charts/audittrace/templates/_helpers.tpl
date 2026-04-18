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
