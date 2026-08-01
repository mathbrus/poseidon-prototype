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
