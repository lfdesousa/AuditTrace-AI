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
