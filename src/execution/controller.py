"""One fenced controller per accepted invocation; no uploaded code in this process."""

import asyncio
from datetime import UTC, datetime

from runner.protocol import Decoder, ProtocolError, encode
from src.broker.models import UpstreamStatusError
from src.catalog.validation import validate_instance
from src.execution.kata import pod_name
from src.execution.models import ExecutionDeniedError, OutcomeUnknownError
from src.execution.service import resolved_snapshot


class Controller:
    def __init__(self, repository, service, broker, backend, catalog):
        self.repository, self.service, self.broker = repository, service, broker
        self.backend, self.catalog = backend, catalog
        self.settings = repository.settings

    async def guard(self, row):
        while True:
            await asyncio.sleep(self.settings.heartbeat_seconds)
            if datetime.now(UTC) >= row["deadline"]:
                raise TimeoutError()
            await self.repository.heartbeat(row["run_id"], row["worker_id"], row["fence"])
            await self.service.live(row)

    async def pump(self, row, pod, limits):
        channel = None
        try:
            while True:
                current = await self.backend.inspect(pod.name)
                if (
                    current is None
                    or current.uid != pod.uid
                    or current.phase in {"Failed", "Succeeded"}
                ):
                    raise ExecutionDeniedError()
                if current.phase == "Running":
                    break
                if datetime.now(UTC) >= row["start_deadline"]:
                    raise TimeoutError()
                await asyncio.sleep(0.1)
            channel = await self.backend.attach(pod)
            resolved = resolved_snapshot(row)
            files = await self.catalog.files(
                row["owner_tenant_id"], row["tool_id"], row["tool_version"]
            )
            # Init has an independent package bound; subsequent frames use the much
            # smaller invocation ceiling. Package content remains off all public APIs.
            await channel.send(
                encode(
                    {
                        "type": "init",
                        "protocol": 1,
                        "files": [f.model_dump() for f in files],
                        "digest": resolved.manifest.package_sha256,
                        "entrypoint": resolved.manifest.entrypoint,
                        "inputs": row["inputs"],
                        "context": resolved.context.public_values(),
                        "limits": limits.model_dump(),
                    },
                    8388608,
                )
            )
            decoder = Decoder(
                min(limits.frame_bytes, self.settings.max_frame_bytes),
                self.settings.max_output_bytes,
                self.settings.max_frames,
            )
            ready = finalized = False
            result = None
            sequence = logs = 0
            while True:
                # A silent Running pod must not consume the entire run deadline
                # before init/ready completes.
                if not ready:
                    remaining = (row["start_deadline"] - datetime.now(UTC)).total_seconds()
                    if remaining <= 0:
                        raise TimeoutError()
                    chunk = await asyncio.wait_for(channel.receive(), remaining)
                else:
                    remaining = (row["deadline"] - datetime.now(UTC)).total_seconds()
                    chunk = await asyncio.wait_for(channel.receive(), max(0.001, remaining))
                if chunk is None:
                    break
                output, stderr = chunk
                logs += len(stderr)
                if logs > self.settings.max_log_bytes:
                    raise ProtocolError("diagnostics limit")
                for frame in decoder.feed(output):
                    if finalized:
                        raise ProtocolError("frame after final")
                    kind = frame["type"]
                    if not ready:
                        if kind != "ready":
                            raise ProtocolError("ready required")
                        if datetime.now(UTC) >= row["start_deadline"]:
                            raise TimeoutError()
                        ready = True
                        await self.repository.running(row)
                        continue
                    if kind == "api_call":
                        if frame["sequence"] != sequence + 1:
                            raise ProtocolError("repeated or unordered sequence")
                        sequence += 1
                        if sequence > limits.api_calls:
                            raise ExecutionDeniedError()
                        response, error = None, None
                        try:
                            response = await self.broker.call(
                                row,
                                frame["operation_id"],
                                frame["arguments"],
                                frame.get("idempotency_key"),
                            )
                        except UpstreamStatusError as status:
                            error = {
                                "code": "http_status",
                                "status": status.status,
                                "body": status.body,
                            }
                        await channel.send(
                            encode(
                                {
                                    "type": "api_result",
                                    "sequence": sequence,
                                    "result": response,
                                    "error": error,
                                },
                                min(limits.frame_bytes, self.settings.max_frame_bytes),
                            )
                        )
                    elif kind == "result":
                        validate_instance(
                            resolved.manifest.result_schema, frame["result"], self.settings
                        )
                        if len(encode(frame, limits.frame_bytes)) > min(
                            limits.output_bytes, self.settings.max_result_bytes
                        ):
                            raise ProtocolError("result size")
                        result, finalized = frame["result"], True
                    elif kind == "log":
                        logs += len(frame["message"].encode())
                        if logs > self.settings.max_log_bytes:
                            raise ProtocolError("diagnostics limit")
                    else:
                        raise ProtocolError("unexpected runner frame")
                if not ready and datetime.now(UTC) >= row["start_deadline"]:
                    raise TimeoutError()
                await asyncio.sleep(0)
            if decoder.buffer or not finalized:
                raise ProtocolError("incomplete result")
            # Attach EOF alone does not establish success. Wait for the actual pod
            # termination status and require the same immutable UID with exit 0.
            while True:
                current = await self.backend.inspect(pod.name)
                if current is None or current.uid != pod.uid:
                    raise ExecutionDeniedError()
                if current.phase in {"Failed", "Succeeded"}:
                    if current.phase != "Succeeded" or current.exit_code != 0:
                        raise ExecutionDeniedError()
                    return result
                await asyncio.sleep(0.1)
        finally:
            if channel is not None:
                await channel.close()

    async def execute(self, row):
        status, code, result = "failed", "execution_failed", None
        pod, guard, pump = None, None, None
        try:
            guard = asyncio.create_task(self.guard(row))
            await asyncio.wait_for(
                self.broker.preflight(row),
                max(
                    0.001,
                    (
                        min(row["start_deadline"], row["deadline"]) - datetime.now(UTC)
                    ).total_seconds(),
                ),
            )
            limits, _ = await self.service.live(row)
            limits = limits.limits
            await self.repository.pod(row, pod_name(row))  # durable intent before create
            pod = await self.backend.start(row, limits)
            await self.repository.pod(row, pod.name, pod.uid)
            pump = asyncio.create_task(self.pump(row, pod, limits))
            seconds = max(0.001, (row["deadline"] - datetime.now(UTC)).total_seconds())
            async with asyncio.timeout(seconds):
                done, _ = await asyncio.wait({guard, pump}, return_when=asyncio.FIRST_COMPLETED)
                if guard in done:
                    await guard
                    raise ExecutionDeniedError()
                result = await pump
                # Recheck cancellation, lease and live revocation before success.
                await self.service.live(row)
                status, code = "succeeded", None
        except OutcomeUnknownError:
            status, code = "interrupted", "outcome_unknown"
        except TimeoutError:
            status, code = "timed_out", "deadline_exceeded"
        except asyncio.CancelledError:
            # Worker shutdown: ``finally`` stops the pod and persists the
            # interruption before the cancellation propagates to the canceller
            # (worker.py's shutdown gather); swallowing it would break
            # asyncio.timeout/TaskGroup cancellation accounting.
            status, code = "interrupted", "worker_stopped"
            raise
        except Exception:
            # Any authority/channel loss after execution may include a completed
            # side effect. Never requeue or import/replay the package.
            status, code = (
                ("interrupted", "execution_interrupted") if pod else ("failed", "preflight_failed")
            )
        finally:
            for task in (guard, pump):
                if task:
                    task.cancel()
            await asyncio.gather(*(t for t in (guard, pump) if t), return_exceptions=True)
            absent = False
            try:
                # A failed create response can still have created the pod.
                current = pod or await self.backend.inspect(pod_name(row))
                if current and (
                    current.run_id != str(row["run_id"]) or current.fence != row["fence"]
                ):
                    raise ExecutionDeniedError()
                absent = current is None or await self.backend.stop(current)
            except Exception:
                absent = False
            try:
                await self.repository.finish(row, status, result=result, code=code, absent=absent)
            except ExecutionDeniedError:
                # Expired/replaced owner cannot persist a terminal result or free
                # capacity. Reconciliation owns that decision now.
                pass
