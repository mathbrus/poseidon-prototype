{{/*
The platform namespace is the release namespace; platform resources omit an
explicit metadata.namespace and land there. Company resources set
metadata.namespace to .Values.companyNamespace explicitly.
*/}}

{{- define "poseidon.labels" -}}
app.kubernetes.io/part-of: poseidon
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "poseidon.platformNamespace" -}}
{{ .Release.Namespace }}
{{- end }}

{{/*
Optional OTel export (the anytime-track adoption): when
.Values.telemetry.otlpEndpoint is set, platform services get the standard
SDK variables and common/telemetry.py attaches the OTLP log exporter next
to the always-on stdout JSON. Usage:
  {{ include "poseidon.otelEnv" (dict "root" . "service" "mcp-gateway") }}
*/}}
{{- define "poseidon.otelEnv" -}}
{{- if .root.Values.telemetry.otlpEndpoint }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ .root.Values.telemetry.otlpEndpoint | quote }}
- name: OTEL_SERVICE_NAME
  value: {{ .service | quote }}
{{- end }}
{{- end }}
