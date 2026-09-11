{{/*
The platform namespace is the release namespace; platform resources omit an
explicit metadata.namespace and land there. The company's namespace is the
operator's to create, from the Company CR; the chart only selects it, by
the fixed name under `companyNamespace`.
*/}}

{{- define "poseidon.labels" -}}
app.kubernetes.io/part-of: poseidon
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "poseidon.platformNamespace" -}}
{{ .Release.Namespace }}
{{- end }}

{{/*
A ServiceAccount bound to system:auth-delegator — what a service that
verifies callers' projected tokens through the TokenReview API needs.
Usage:
  {{ include "poseidon.authDelegator" (dict "root" . "name" "mcp-gateway") }}
*/}}
{{- define "poseidon.authDelegator" -}}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ .name }}
  labels:
    {{- include "poseidon.labels" .root | nindent 4 }}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: poseidon-{{ .name }}-auth-delegator
  labels:
    {{- include "poseidon.labels" .root | nindent 4 }}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:auth-delegator
subjects:
  - kind: ServiceAccount
    name: {{ .name }}
    namespace: {{ .root.Release.Namespace }}
{{- end }}

{{/*
Scratch space for a read-only root filesystem: the volume (pod level) and
its mount (container level).
*/}}
{{- define "poseidon.scratchVolume" -}}
- name: tmp
  emptyDir: {}
{{- end }}

{{- define "poseidon.scratchMount" -}}
- name: tmp
  mountPath: /tmp
{{- end }}

{{/*
The registry DSN, for the services that read or write it.
*/}}
{{- define "poseidon.registryDsnEnv" -}}
- name: REGISTRY_DSN
  valueFrom:
    secretKeyRef:
      name: registry-credentials
      key: REGISTRY_DSN
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
