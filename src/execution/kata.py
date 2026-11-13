"""Kubernetes attach transport, in-cluster only. No exec, secret reads or fallback."""

import asyncio
import math

from src.execution.backend import Pod
from src.execution.models import ExecutionDeniedError
from src.execution.service import resolved_snapshot


def pod_name(row):
    return "mcp-" + row["run_id"].hex + "-" + str(row["fence"])


def pod_manifest(row, settings, limits):
    manifest = resolved_snapshot(row).manifest
    if (
        not settings.kata_runtime_class
        or manifest.runner_image_digest != settings.runner_image_digest
        or manifest.runner_image_digest not in settings.approved_runner_images
    ):
        raise ExecutionDeniedError()
    memory = min(limits.memory_mi, settings.run_memory_limit_mi)
    cpu = min(limits.cpu_millicores, settings.run_cpu_limit)
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name(row),
            "namespace": settings.run_namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "mcp-executor",
                "mcp.run-id": str(row["run_id"]),
                "mcp.fence": str(row["fence"]),
            },
        },
        "spec": {
            "runtimeClassName": settings.kata_runtime_class,
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "terminationGracePeriodSeconds": 1,
            "activeDeadlineSeconds": settings.run_start_timeout_seconds
            + math.ceil(min(limits.wall_seconds, settings.run_max_seconds)),
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "fsGroup": 1000,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "runner",
                    "image": manifest.runner_image_digest,
                    # The digest is mandatory and immutable; permit air-gapped
                    # clusters to provision the reviewed digest into containerd.
                    "imagePullPolicy": "IfNotPresent",
                    "stdin": True,
                    "stdinOnce": True,
                    "tty": False,
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "requests": {
                            "memory": f"{min(memory, settings.run_memory_request_mi)}Mi",
                            "cpu": f"{min(cpu, settings.run_cpu_request)}m",
                        },
                        "limits": {
                            "memory": f"{memory}Mi",
                            "cpu": f"{cpu}m",
                            "ephemeral-storage": "64Mi",
                        },
                    },
                    "volumeMounts": [{"name": "scratch", "mountPath": "/scratch"}],
                }
            ],
            "volumes": [{"name": "scratch", "emptyDir": {"medium": "Memory", "sizeLimit": "32Mi"}}],
        },
    }


def pod_record(value):
    labels = value.metadata.labels or {}
    if labels.get("app.kubernetes.io/managed-by") != "mcp-executor":
        raise ExecutionDeniedError()
    statuses = value.status.container_statuses or []
    terminated = statuses[0].state.terminated if statuses else None
    return Pod(
        value.metadata.name,
        value.metadata.uid,
        labels["mcp.run-id"],
        int(labels["mcp.fence"]),
        value.status.phase,
        terminated.exit_code if terminated else None,
    )


def attach_stream():
    """kubernetes.stream.stream, but with WSClient(capture_all=False).

    The generated attach method rejects unknown keywords, so capture_all can only
    reach websocket_call through the request override. Without it WSClient keeps a
    combined copy of all output for the whole run, and the only public drain,
    read_all(), also discards frames already buffered per channel.
    """
    import functools
    import importlib

    from kubernetes.stream import ws_client

    # The package re-exports the `stream` partial under the submodule's name.
    module = importlib.import_module("kubernetes.stream.stream")
    return functools.partial(
        module._websocket_request,
        functools.partial(ws_client.websocket_call, capture_all=False),
        None,
    )


class AttachChannel:
    def __init__(self, websocket):
        self.websocket = websocket
        self.lock = asyncio.Lock()

    async def send(self, data):
        async with self.lock:
            await asyncio.to_thread(self.websocket.write_stdin, data.decode("utf-8"))

    def _poll(self):
        if not self.websocket.is_open():
            return None
        self.websocket.update(timeout=0.2)
        # read_stdout()/read_stderr() default to timeout=None, i.e. an unbounded
        # poll for the next frame whenever the channel is empty. That blocked the
        # executor until the runner wrote again, which never happens while it waits
        # for an api_result. timeout=0 only drains what has already arrived; a
        # frame picked up by these reads stays buffered for the next call.
        return self.websocket.read_stdout(timeout=0), self.websocket.read_stderr(timeout=0)

    async def receive(self):
        async with self.lock:
            # All socket polling runs off the event loop thread.
            chunk = await asyncio.to_thread(self._poll)
            if chunk is None:
                return None
            stdout, stderr = chunk
            # WSClient uses binary=True to preserve UTF-8 split across websocket chunks.
            return (
                stdout if isinstance(stdout, bytes) else stdout.encode(),
                stderr if isinstance(stderr, bytes) else stderr.encode(),
            )

    async def close(self):
        async with self.lock:
            await asyncio.to_thread(self.websocket.close)


class KataBackend:
    def __init__(self, settings):
        # Optional dependency is present exclusively in the worker image.
        from kubernetes import client, config

        config.load_incluster_config()
        self.settings = settings
        self.api_client = client.ApiClient()
        self.api = client.CoreV1Api(self.api_client)
        # stream() changes its ApiClient transport. Never share it with pod CRUD.
        self.attach_client = client.ApiClient()
        self.attach_api = client.CoreV1Api(self.attach_client)

    async def ready(self):
        from kubernetes import client

        if not self.settings.kata_runtime_class or not self.settings.runner_image_digest:
            raise ExecutionDeniedError()
        # Resource-name scoped read permission, no node/secret/namespace management.
        runtime = await asyncio.to_thread(
            client.NodeV1Api(self.api_client).read_runtime_class,
            self.settings.kata_runtime_class,
            _request_timeout=5,
        )
        if not runtime.handler:
            raise ExecutionDeniedError()
        await self.list()

    async def inspect(self, name):
        from kubernetes.client.rest import ApiException

        try:
            return pod_record(
                await asyncio.to_thread(
                    self.api.read_namespaced_pod,
                    name,
                    self.settings.run_namespace,
                    _request_timeout=5,
                )
            )
        except ApiException as error:
            if error.status == 404:
                return None
            raise ExecutionDeniedError() from None

    async def start(self, row, limits):
        from kubernetes.client.rest import ApiException

        name = pod_name(row)
        try:
            pod = pod_record(
                await asyncio.to_thread(
                    self.api.create_namespaced_pod,
                    self.settings.run_namespace,
                    pod_manifest(row, self.settings, limits),
                    _request_timeout=5,
                )
            )
        except ApiException, OSError, TimeoutError:
            # An ambiguous create is resolved by exact deterministic name, never
            # by launching a second pod. If inspection fails, retain the slot.
            pod = await self.inspect(name)
            if pod is None:
                raise ExecutionDeniedError() from None
        if (
            pod.run_id != str(row["run_id"])
            or pod.fence != row["fence"]
            or (row["pod_uid"] and row["pod_uid"] != pod.uid)
        ):
            raise ExecutionDeniedError()
        return pod

    async def attach(self, pod):
        stream = attach_stream()

        current = await self.inspect(pod.name)
        if current is None or current.uid != pod.uid:
            raise ExecutionDeniedError()
        # Fresh API client per stream, as websocket calls replace client transport.
        from kubernetes import client

        api = client.CoreV1Api(client.ApiClient())
        websocket = await asyncio.to_thread(
            stream,
            api.connect_get_namespaced_pod_attach,
            pod.name,
            self.settings.run_namespace,
            container="runner",
            stdin=True,
            stdout=True,
            stderr=True,
            tty=False,
            _preload_content=False,
            binary=True,
            _request_timeout=5,
        )
        return AttachChannel(websocket)

    async def stop(self, pod):
        from kubernetes import client
        from kubernetes.client.rest import ApiException

        current = await self.inspect(pod.name)
        if current is None:
            return True
        if current.uid != pod.uid or current.run_id != pod.run_id or current.fence != pod.fence:
            raise ExecutionDeniedError()
        try:
            await asyncio.to_thread(
                self.api.delete_namespaced_pod,
                pod.name,
                self.settings.run_namespace,
                body=client.V1DeleteOptions(
                    grace_period_seconds=0, preconditions=client.V1Preconditions(uid=pod.uid)
                ),
                _request_timeout=5,
            )
        except ApiException as error:
            if error.status != 404:
                raise ExecutionDeniedError() from None
        for _ in range(20):
            if await self.inspect(pod.name) is None:
                return True
            await asyncio.sleep(0.25)
        return False

    async def list(self):
        result = await asyncio.to_thread(
            self.api.list_namespaced_pod,
            self.settings.run_namespace,
            label_selector="app.kubernetes.io/managed-by=mcp-executor",
            limit=200,
            _request_timeout=5,
        )
        if result.metadata._continue:
            raise ExecutionDeniedError("runner pod inventory exceeds capacity")
        return [pod_record(p) for p in result.items]

    async def close(self):
        await asyncio.to_thread(self.api_client.close)
        await asyncio.to_thread(self.attach_client.close)
