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
Construct the PostgreSQL connection URL for the owner role (summariser).
*/}}
{{- define "audittrace.postgresOwnerUrl" -}}
postgresql+psycopg2://{{ .Values.postgresql.auth.username }}:{{ .Values.secrets.postgres.password }}@{{ .Release.Name }}-postgresql:5432/{{ .Values.postgresql.auth.database }}
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
