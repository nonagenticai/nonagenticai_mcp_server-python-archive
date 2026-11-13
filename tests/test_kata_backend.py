"""Locked Python Kubernetes client adapter tests. A real Kata job is still required."""

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid7

import pytest
from kubernetes.client.rest import ApiException

from src.execution.kata import KataBackend, pod_record
from src.execution.models import ExecutionDeniedError
from tests.fixtures import IMAGE, settings


def record(name="pod", uid="uid", run_id=None, fence=1, phase="Running", exit_code=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            uid=uid,
            labels={
                "app.kubernetes.io/managed-by": "mcp-executor",
                "mcp.run-id": run_id or str(uuid7()),
                "mcp.fence": str(fence),
            },
        ),
        status=SimpleNamespace(
            phase=phase,
            container_statuses=[
                SimpleNamespace(
                    state=SimpleNamespace(
                        terminated=SimpleNamespace(exit_code=exit_code)
                        if exit_code is not None
                        else None
                    )
                )
            ],
        ),
    )


@pytest.fixture
def backend(monkeypatch):
    import kubernetes

    monkeypatch.setattr(kubernetes.config, "load_incluster_config", Mock())
    config = settings(kata_runtime_class="kata-qemu", runner_image_digest=IMAGE)
    result = KataBackend(config)
    result.api = Mock()
    return result


async def test_client_start_inspect_list_and_cleanup(backend, monkeypatch):
    import kubernetes

    from src.catalog.resolver import ResolvedTool
    from src.toolkits.models import BoundContext, EffectiveTool, ToolReference
    from tests.fixtures import TENANT_A, TOOL, tool

    resolved = ResolvedTool(
        tool().model_dump_json(),
        EffectiveTool(
            tool=ToolReference(tenant_id=TENANT_A, id=TOOL, version=1), public_name="synthetic_tool"
        ).model_dump_json(),
        BoundContext(tenant_id=TENANT_A).model_dump_json(),
    )
    run_id = uuid7()
    row = {
        "run_id": run_id,
        "fence": 1,
        "pod_uid": None,
        "authority": {
            "manifest": resolved.manifest_json,
            "effective": resolved.effective_json,
            "context": resolved.context_json,
        },
    }
    value = record(run_id=str(run_id))
    backend.api.create_namespaced_pod.return_value = value
    backend.api.read_namespaced_pod.return_value = value
    backend.api.list_namespaced_pod.return_value = SimpleNamespace(
        items=[value], metadata=SimpleNamespace(_continue=None)
    )
    monkeypatch.setattr(
        kubernetes.client.NodeV1Api,
        "read_runtime_class",
        Mock(return_value=SimpleNamespace(handler="kata-qemu")),
    )
    await backend.ready()
    pod = await backend.start(row, resolved.effective.limits)
    assert pod.run_id == str(run_id)
    assert (await backend.list())[0].uid == "uid"
    backend.api.read_namespaced_pod.side_effect = [value, ApiException(status=404)]
    assert await backend.stop(pod)
    assert backend.api.delete_namespaced_pod.call_args.kwargs["body"].preconditions.uid == "uid"
    await backend.close()


async def test_ambiguous_create_verifies_exact_existing_pod(backend):

    run_id = uuid7()
    row = {"run_id": run_id, "fence": 1, "pod_uid": None}
    backend.api.create_namespaced_pod.side_effect = ApiException(status=409)
    backend.api.read_namespaced_pod.return_value = record(run_id=str(run_id))
    # Manifest construction is separately tested: use a locally patched constructor.
    from unittest.mock import patch

    with patch("src.execution.kata.pod_manifest", return_value={}):
        assert (await backend.start(row, None)).run_id == str(run_id)
        backend.api.read_namespaced_pod.return_value = record(run_id=str(uuid7()))
        with pytest.raises(ExecutionDeniedError):
            await backend.start(row, None)
        backend.api.read_namespaced_pod.side_effect = ApiException(status=404)
        with pytest.raises(ExecutionDeniedError):
            await backend.start(row, None)


async def test_inspection_and_inventory_fail_closed(backend):
    backend.api.read_namespaced_pod.side_effect = ApiException(status=403)
    with pytest.raises(ExecutionDeniedError):
        await backend.inspect("pod")
    backend.api.list_namespaced_pod.return_value = SimpleNamespace(
        metadata=SimpleNamespace(_continue="more")
    )
    with pytest.raises(ExecutionDeniedError):
        await backend.list()
    value = record()
    value.metadata.labels = {}
    with pytest.raises(ExecutionDeniedError):
        pod_record(value)
    backend.settings = backend.settings.model_copy(update={"kata_runtime_class": None})
    with pytest.raises(ExecutionDeniedError):
        await backend.ready()


async def test_delete_failure_never_confirms_absence(backend, monkeypatch):
    value = record()
    pod = pod_record(value)
    backend.api.read_namespaced_pod.return_value = value
    backend.api.delete_namespaced_pod.side_effect = ApiException(status=403)
    with pytest.raises(ExecutionDeniedError):
        await backend.stop(pod)
    backend.api.delete_namespaced_pod.side_effect = None

    async def immediate(*args):
        pass

    monkeypatch.setattr("src.execution.kata.asyncio.sleep", immediate)
    assert await backend.stop(pod) is False
    backend.api.read_namespaced_pod.return_value = record(uid="replacement")
    with pytest.raises(ExecutionDeniedError):
        await backend.stop(pod)
    backend.api.read_namespaced_pod.side_effect = ApiException(status=404)
    assert await backend.stop(pod)


async def test_attach_preserves_binary_utf8_and_disables_unbounded_capture(backend, monkeypatch):
    value = record()
    backend.api.read_namespaced_pod.return_value = value
    ws = Mock()
    ws.is_open.return_value = True
    ws.read_stdout.return_value = b"caf\xc3"
    ws.read_stderr.return_value = b"diagnostic"
    stream = Mock(return_value=ws)
    monkeypatch.setattr("src.execution.kata.attach_stream", Mock(return_value=stream))
    channel = await backend.attach(pod_record(value))
    assert stream.call_args.kwargs["binary"] is True
    assert stream.call_args.kwargs["tty"] is False
    assert await channel.receive() == (b"caf\xc3", b"diagnostic")
    ws.read_stdout.assert_called_with(timeout=0)
    ws.read_stderr.assert_called_with(timeout=0)
    ws.read_all.assert_not_called()
    await channel.send(b"hello\n")
    ws.write_stdin.assert_called_with("hello\n")
    ws.is_open.return_value = False
    assert await channel.receive() is None
    await channel.close()
    backend.api.read_namespaced_pod.side_effect = ApiException(status=404)
    with pytest.raises(ExecutionDeniedError):
        await backend.attach(pod_record(value))


async def test_attach_options_pass_the_real_locked_generated_client(backend, monkeypatch):
    from kubernetes.stream import ws_client

    value = record()
    backend.api.read_namespaced_pod.return_value = value
    socket = Mock()
    monkeypatch.setattr(ws_client, "create_websocket", Mock(return_value=socket))
    channel = await backend.attach(pod_record(value))
    assert isinstance(channel.websocket, ws_client.WSClient) and channel.websocket.binary is True
    # No unbounded combined capture of runner output.
    assert isinstance(channel.websocket._all, ws_client._IgnoredIO)
    await channel.close()


async def test_attach_receive_never_blocks_the_loop_or_drops_frames(backend, monkeypatch):
    """Regression: WSClient.read_stdout()/read_stderr() default to a blocking
    poll(None) and read_all() discards buffered channels. Driving the real WSClient
    over a socketpair, an api_call frame that arrives after `ready` must be
    delivered and an idle channel must return promptly."""
    import socket
    import threading
    import time
    from collections import deque

    from kubernetes.stream import ws_client
    from websocket import ABNF

    readable, writer = socket.socketpair()
    frames = deque()

    class Socket:
        connected = True
        sock = readable

        def recv_data_frame(self, _control):
            readable.recv(1)
            op_code, data = frames.popleft()
            return op_code, SimpleNamespace(data=data)

        def close(self, **_kwargs):
            pass

    def push(op_code, data):
        frames.append((op_code, data))
        writer.send(b"x")

    value = record()
    backend.api.read_namespaced_pod.return_value = value
    monkeypatch.setattr(ws_client, "create_websocket", Mock(return_value=Socket()))
    channel = await backend.attach(pod_record(value))
    push(ABNF.OPCODE_BINARY, b"\x01ready\n")
    push(ABNF.OPCODE_BINARY, b"\x01call\n")
    # Safety valve only: a blocking read would otherwise hang the test forever.
    safety = threading.Timer(3, push, (ABNF.OPCODE_CLOSE, b""))
    safety.start()
    try:
        started = time.monotonic()
        output = b""
        while b"call" not in output:
            chunk = await channel.receive()
            assert chunk is not None, "frame dropped"
            output += chunk[0]
        assert output == b"ready\ncall\n"
        assert await channel.receive() == (b"", b"")
        assert time.monotonic() - started < 2
    finally:
        safety.cancel()
        await channel.close()
        readable.close()
        writer.close()
