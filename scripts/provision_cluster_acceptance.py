"""Generate isolated synthetic acceptance manifests; never applies or uses a kube context.

Images must already be provisioned by digest on the selected node or registry.
Refuse existing acceptance namespaces before applying this generated candidate.
Delete only the generated namespaces/cluster RBAC after recording the Job result.
"""

import argparse
import json
import secrets
from pathlib import Path
from uuid import uuid7

from pydantic import TypeAdapter

from src.catalog.models import ImageDigest


def manifests(
    worker_image, runner_image, *, namespace="mcp-acceptance", node="worker3", probe_ip="10.43.0.1"
):
    for image in (worker_image, runner_image):
        TypeAdapter(ImageDigest).validate_python(image)
    if not namespace.startswith("mcp-acceptance") or namespace == "mcp-runs":
        raise ValueError("a dedicated acceptance namespace is required")
    tenant, profile = uuid7(), uuid7()
    licenses = [uuid7() for _ in range(3)]
    tokens = [secrets.token_urlsafe(32) for _ in range(3)]
    admin, platform, password = [secrets.token_urlsafe(32) for _ in range(3)]

    def dsn(role):
        return f"postgresql://{role}:{password}@postgres.{namespace}:5432/mcp_acceptance"

    config = {
        "synthetic": True,
        "api_url": "http://127.0.0.1:8033",
        "tenant_id": str(tenant),
        "gateway_license_id": str(licenses[0]),
        "gateway_token": tokens[0],
        "admin_key": admin,
        "platform_key": platform,
        "profile_id": str(profile),
        "first_license_id": str(licenses[1]),
        "second_license_id": str(licenses[2]),
        "public_probe_ip": "1.1.1.1",
        "cluster_probe_ip": probe_ip,
    }
    entries = [
        {
            "tenant_id": str(tenant),
            "profile_id": str(profile),
            "service": service,
            "alias": alias,
            "license_id": str(licenses[number]),
            "app_key": app,
            "token": tokens[number],
        }
        for number, service, alias, app in (
            (0, "mcp", "acceptance_gateway", "mcp"),
            (1, "acceptance_1", "acceptance_1", "acceptance_1"),
            (2, "acceptance_2", "acceptance_2", "acceptance_2"),
            (1, "acceptance_mutation", "acceptance_1", "acceptance_1"),
        )
    ]
    second_tenant, second_profile = uuid7(), uuid7()
    second_entries = [
        {
            **entry,
            "tenant_id": str(second_tenant),
            "profile_id": str(second_profile),
            "license_id": str(uuid7()),
            "token": secrets.token_urlsafe(32),
        }
        for entry in entries[:3]
    ]
    entries.extend(second_entries)
    secondary = {
        "tenant_id": str(second_tenant),
        "profile_id": str(second_profile),
        "gateway_token": second_entries[0]["token"],
        "references": [
            {k: e[k] for k in ("service", "alias", "license_id", "app_key")} for e in second_entries
        ],
    }
    runtime = {
        "DATABASE_URL": dsn("acceptance_api"),
        "MCP_ACCEPTANCE_OWNER_DATABASE_URL": dsn("postgres"),
        "MCP_ACCEPTANCE_WORKER_DATABASE_URL": dsn("acceptance_worker"),
        "MCP_PLATFORM_DATABASE_URL": dsn("acceptance_platform"),
        "MCP_ADMIN_KEY_FILE": "/run/acceptance/admin",
        "MCP_PLATFORM_ADMIN_KEY_FILE": "/run/acceptance/platform",
        "MCP_ADMIN_TENANT_IDS": json.dumps([str(tenant), str(second_tenant)]),
        "LICENSE_INTROSPECT_URL": "http://127.0.0.1:8040/introspect",
        "MCP_LICENSE_ALLOW_HTTP": "true",
        # Tool sources are registered at runtime (verify_execution_cluster); IP literals are refused.
        "MCP_SOURCE_ALLOWED_HOSTS": "localhost:8040",
        "MCP_LICENSE_APP_KEY": "mcp",
        "LICENSE_CACHE_TTL_SECONDS": "0",
        "MCP_EXECUTION_ENABLED": "true",
        "MCP_APPROVED_RUNNER_IMAGES": json.dumps([runner_image]),
        "MCP_RUNNER_IMAGE_DIGEST": runner_image,
        "MCP_KATA_RUNTIME_CLASS": "kata",
        "MCP_EXECUTION_GLOBAL_SLOTS": "2",
        "MCP_EXECUTION_PER_TENANT_SLOTS": "1",
        "MCP_HEARTBEAT_SECONDS": "1",
        "MCP_LEASE_SECONDS": "10",
        "MCP_RECONCILE_SECONDS": "1",
        "MCP_RUN_START_TIMEOUT_SECONDS": "30",
        "MCP_RUN_MAX_SECONDS": "30",
    }
    objects = []

    def add(kind, name, spec=None, *, ns=namespace, api="v1", **extra):
        value = {
            "apiVersion": api,
            "kind": kind,
            "metadata": {"name": name, **({"namespace": ns} if ns else {})},
            **extra,
        }
        if spec is not None:
            value["spec"] = spec
        objects.append(value)
        return value

    for name in (namespace, "mcp-runs"):
        value = add("Namespace", name, ns=None)
        value["metadata"]["labels"] = {
            "mcp.synthetic-acceptance": "true",
            "pod-security.kubernetes.io/enforce": "restricted",
        }
    add(
        "NetworkPolicy",
        "deny-all",
        {"podSelector": {}, "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": []},
        ns="mcp-runs",
        api="networking.k8s.io/v1",
    )
    add(
        "ResourceQuota",
        "acceptance-capacity",
        {
            "hard": {
                "pods": "2",
                "requests.memory": "896Mi",
                "limits.memory": "896Mi",
                "requests.cpu": "1",
                "limits.cpu": "1",
            }
        },
        ns="mcp-runs",
    )
    add("ServiceAccount", "acceptance")
    add(
        "Role",
        "acceptance",
        ns="mcp-runs",
        api="rbac.authorization.k8s.io/v1",
        rules=[
            {
                "apiGroups": [""],
                "resources": ["pods"],
                "verbs": ["create", "get", "list", "delete"],
            },
            {"apiGroups": [""], "resources": ["pods/attach"], "verbs": ["get", "create"]},
        ],
    )
    subject = {"kind": "ServiceAccount", "name": "acceptance", "namespace": namespace}
    add(
        "RoleBinding",
        "acceptance",
        ns="mcp-runs",
        api="rbac.authorization.k8s.io/v1",
        subjects=[subject],
        roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "acceptance"},
    )
    add(
        "ClusterRole",
        namespace,
        ns=None,
        api="rbac.authorization.k8s.io/v1",
        rules=[
            {
                "apiGroups": ["node.k8s.io"],
                "resources": ["runtimeclasses"],
                "resourceNames": ["kata"],
                "verbs": ["get"],
            }
        ],
    )
    add(
        "ClusterRoleBinding",
        namespace,
        ns=None,
        api="rbac.authorization.k8s.io/v1",
        subjects=[subject],
        roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": namespace},
    )
    add(
        "Secret",
        "fixtures",
        type="Opaque",
        stringData={
            "config.json": json.dumps(config),
            "runtime.json": json.dumps(runtime),
            "secondary.json": json.dumps(secondary),
            "bundle.json": json.dumps({"schema_version": 1, "entries": entries}),
            "admin": admin,
            "platform": platform,
            "postgres-password": password,
        },
    )
    # The test coordinator is reviewable source mounted read-only. API, worker
    # and runner implementations still come from the pinned images.
    add(
        "ConfigMap",
        "acceptance-code",
        data={
            name: (Path(__file__).parent / name).read_text()
            for name in (
                "grant_runtime_role.py",
                "verify_execution_cluster.py",
                "acceptance_fixtures.py",
                "acceptance_worker.py",
                "run_cluster_acceptance.py",
            )
        },
    )
    security = {
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "fsGroup": 1000,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    container_security = {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    add(
        "Service",
        "postgres",
        {"selector": {"app": "acceptance-postgres"}, "ports": [{"port": 5432}]},
    )
    add(
        "Pod",
        "postgres",
        {
            "nodeName": node,
            "automountServiceAccountToken": False,
            "securityContext": {**security, "runAsUser": 999, "runAsGroup": 999, "fsGroup": 999},
            "containers": [
                {
                    "name": "postgres",
                    "image": "postgres:18",
                    "securityContext": container_security,
                    "env": [
                        {"name": "POSTGRES_DB", "value": "mcp_acceptance"},
                        {
                            "name": "POSTGRES_PASSWORD",
                            "valueFrom": {
                                "secretKeyRef": {"name": "fixtures", "key": "postgres-password"}
                            },
                        },
                    ],
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "128Mi"},
                        "limits": {"cpu": "1", "memory": "512Mi"},
                    },
                    "readinessProbe": {
                        "exec": {
                            "command": ["pg_isready", "-U", "postgres", "-d", "mcp_acceptance"]
                        }
                    },
                    "volumeMounts": [
                        {"name": "data", "mountPath": "/var/lib/postgresql"},
                        {"name": "temporary", "mountPath": "/var/run/postgresql"},
                        {"name": "temporary", "mountPath": "/tmp"},  # nosec B108 -- isolated emptyDir mount
                    ],
                }
            ],
            "volumes": [
                {"name": "data", "emptyDir": {"sizeLimit": "512Mi"}},
                {"name": "temporary", "emptyDir": {"sizeLimit": "32Mi"}},
            ],
        },
    )["metadata"]["labels"] = {"app": "acceptance-postgres"}
    add(
        "Job",
        "acceptance",
        {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 900,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "metadata": {"labels": {"app": "mcp-execution-acceptance"}},
                "spec": {
                    "nodeName": node,
                    "restartPolicy": "Never",
                    "serviceAccountName": "acceptance",
                    "securityContext": security,
                    "containers": [
                        {
                            "name": "acceptance",
                            "image": worker_image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["python", "-m", "scripts.run_cluster_acceptance"],
                            "env": [
                                {
                                    "name": "MCP_ACCEPTANCE_FIXTURE_DIRECTORY",
                                    "value": "/run/acceptance",
                                }
                            ],
                            "securityContext": container_security,
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "512Mi"},
                                "limits": {"cpu": "2", "memory": "1Gi"},
                            },
                            "volumeMounts": [
                                {"name": "code", "mountPath": "/app/scripts", "readOnly": True},
                                {
                                    "name": "fixtures",
                                    "mountPath": "/run/acceptance",
                                    "readOnly": True,
                                },
                                {"name": "temporary", "mountPath": "/tmp"},  # nosec B108 -- isolated emptyDir mount
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "code", "configMap": {"name": "acceptance-code"}},
                        {
                            "name": "fixtures",
                            "secret": {
                                "secretName": "fixtures",  # pragma: allowlist secret
                                "defaultMode": 288,
                            },
                        },
                        {"name": "temporary", "emptyDir": {"sizeLimit": "64Mi"}},
                    ],
                },
            },
        },
        api="batch/v1",
    )

    return {"apiVersion": "v1", "kind": "List", "items": objects}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-image", required=True)
    parser.add_argument("--runner-image", required=True)
    parser.add_argument("--namespace", default="mcp-acceptance")
    parser.add_argument("--node", default="worker3")
    parser.add_argument("--cluster-probe-ip", default="10.43.0.1")
    args = parser.parse_args()
    print(
        json.dumps(
            manifests(
                args.worker_image,
                args.runner_image,
                namespace=args.namespace,
                node=args.node,
                probe_ip=args.cluster_probe_ip,
            )
        )
    )


if __name__ == "__main__":
    main()
