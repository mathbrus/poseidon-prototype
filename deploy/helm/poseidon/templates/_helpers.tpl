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
Optional OTel export: when
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

{{/*
Pod- and container-level security posture shared by the platform
workloads: non-root (uid 10001, the user baked into the poseidon images),
no privilege-escalation path, all capabilities dropped, default seccomp
filter. Services whose root filesystem can be read-only mount an emptyDir
at /tmp for scratch space (git clones, tempfiles). Postgres runs its own
variant (uid 999, the image's postgres user).
*/}}
{{- define "poseidon.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{- define "poseidon.containerSecurityContext" -}}
allowPrivilegeEscalation: false
capabilities:
  drop: ["ALL"]
readOnlyRootFilesystem: true
{{- end }}

{{/*
Same posture minus the read-only root: for third-party images whose
write locations we do not control (LiteLLM).
*/}}
{{- define "poseidon.containerSecurityContextWritable" -}}
allowPrivilegeEscalation: false
capabilities:
  drop: ["ALL"]
readOnlyRootFilesystem: false
{{- end }}
