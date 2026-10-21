"""The reconciler's Kubernetes surface, as one intent-level client.

Everything the reconcilers do to a cluster goes through `Cluster`, so the
handlers in `reconcile.py` stay pure orchestration (buildable objects in,
intent calls out) and their tests replace this one seam with a fake instead
of stubbing four generated API clients.
"""

from typing import Any

from kubernetes import client as k8s
from kubernetes import config as k8s_config

FIELD_MANAGER = "poseidon-reconciler"


class Cluster:
    def __init__(self) -> None:
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
        self._core = k8s.CoreV1Api()
        self._apps = k8s.AppsV1Api()
        self._networking = k8s.NetworkingV1Api()
        self._rbac = k8s.RbacAuthorizationV1Api()

    # -- upserts ------------------------------------------------------------
    # Create or merge-patch, always sending the whole object. Not server-side
    # apply: the Python client's `patch_*` sends a merge patch, for which
    # `force` is rejected outright, and the apply content type is not plumbed
    # through these generated methods. Sending the full body on every
    # reconcile gets the property that matters — repeated applies converge to
    # the same state instead of accumulating — without pretending to a
    # mechanism we are not using.

    def upsert_namespace(self, body: dict[str, Any]) -> None:
        self._upsert(
            self._core.read_namespace,
            self._core.create_namespace,
            self._core.patch_namespace,
            body,
        )

    def upsert_network_policy(self, namespace: str, body: dict[str, Any]) -> None:
        """Replace, never merge-patch: a policy's selector shape can change
        (matchLabels to matchExpressions, say), and a merge would
        keep both — an impossible AND that silently deselects every pod
        the policy was meant to cover."""
        name = body["metadata"]["name"]
        try:
            existing = self._networking.read_namespaced_network_policy(name, namespace)
        except k8s.exceptions.ApiException as exc:
            if exc.status != 404:
                raise
            self._apply(
                self._networking.create_namespaced_network_policy,
                body,
                namespace=namespace,
            )
            return
        body = dict(body)
        body["metadata"] = {
            **body["metadata"],
            "resourceVersion": existing.metadata.resource_version,
        }
        self._networking.replace_namespaced_network_policy(
            name, namespace, body, field_manager=FIELD_MANAGER
        )

    def upsert_role(self, namespace: str, body: dict[str, Any]) -> None:
        self._upsert(
            self._rbac.read_namespaced_role,
            self._rbac.create_namespaced_role,
            self._rbac.patch_namespaced_role,
            body,
            namespace=namespace,
        )

    def upsert_role_binding(self, namespace: str, body: dict[str, Any]) -> None:
        self._upsert(
            self._rbac.read_namespaced_role_binding,
            self._rbac.create_namespaced_role_binding,
            self._rbac.patch_namespaced_role_binding,
            body,
            namespace=namespace,
        )

    def upsert_deployment(self, namespace: str, body: dict[str, Any]) -> None:
        self._upsert(
            self._apps.read_namespaced_deployment,
            self._apps.create_namespaced_deployment,
            self._apps.patch_namespaced_deployment,
            body,
            namespace=namespace,
        )

    def upsert_service(self, namespace: str, body: dict[str, Any]) -> None:
        self._upsert(
            self._core.read_namespaced_service,
            self._core.create_namespaced_service,
            self._core.patch_namespaced_service,
            body,
            namespace=namespace,
        )

    def upsert_service_account(self, namespace: str, body: dict[str, Any]) -> None:
        self._upsert(
            self._core.read_namespaced_service_account,
            self._core.create_namespaced_service_account,
            self._core.patch_namespaced_service_account,
            body,
            namespace=namespace,
        )

    # -- deletes --------------------------------------------------------------

    def delete_namespace(self, name: str) -> None:
        self._tolerate_404(self._core.delete_namespace, name)

    def delete_deployment(self, namespace: str, name: str) -> None:
        self._tolerate_404(
            self._apps.delete_namespaced_deployment, name, namespace=namespace
        )

    def delete_service(self, namespace: str, name: str) -> None:
        self._tolerate_404(
            self._core.delete_namespaced_service, name, namespace=namespace
        )

    def delete_service_account(self, namespace: str, name: str) -> None:
        self._tolerate_404(
            self._core.delete_namespaced_service_account, name, namespace=namespace
        )

    # -- plumbing -------------------------------------------------------------

    def _upsert(self, read, create, patch, body: dict[str, Any], **scope) -> None:
        """Create the object or merge-patch it whole, by name within `scope`
        (the namespace, for namespaced kinds)."""
        name = body["metadata"]["name"]
        if self._exists(read, name=name, **scope):
            self._apply(patch, body, name=name, **scope)
        else:
            self._apply(create, body, **scope)

    @staticmethod
    def _apply(call, body: dict[str, Any], **kwargs) -> None:
        call(body=body, field_manager=FIELD_MANAGER, **kwargs)

    @staticmethod
    def _exists(read, **kwargs) -> bool:
        try:
            read(**kwargs)
            return True
        except k8s.exceptions.ApiException as exc:
            if exc.status == 404:
                return False
            raise

    @staticmethod
    def _tolerate_404(delete, *args, **kwargs) -> None:
        try:
            delete(*args, **kwargs)
        except k8s.exceptions.ApiException as exc:
            if exc.status != 404:
                raise
