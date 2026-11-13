"""Test-only SIGSTOP checkpoints for killing an actual executor process.

Never used by the production entry point. Requires a synthetic acceptance file;
checkpoint metadata contains only the run UUID and lifecycle point.
"""

import asyncio
import faulthandler
import json
import os
import signal
import sys
from pathlib import Path
from unittest.mock import patch

from src.execution import worker


def checkpoint(point, row):
    requested = os.environ.get("MCP_ACCEPTANCE_CRASH_POINT")
    if requested != point:
        return
    root = Path(os.environ["MCP_ACCEPTANCE_FIXTURE_DIRECTORY"])
    if json.loads((root / "config.json").read_bytes()).get("synthetic") is not True:
        raise ValueError("synthetic acceptance only")
    Path(os.environ["MCP_ACCEPTANCE_CHECKPOINT_FILE"]).write_text(
        json.dumps(
            {
                "point": point,
                "run_id": str(row["run_id"]),
            }
        )
    )
    os.kill(os.getpid(), signal.SIGSTOP)


class CheckpointBackend(worker.KataBackend):
    async def start(self, row, limits):
        checkpoint("before_create", row)
        pod = await super().start(row, limits)
        checkpoint("after_create", row)
        return pod

    async def attach(self, pod):
        checkpoint("before_attach", {"run_id": pod.run_id})
        channel = await super().attach(pod)
        checkpoint("after_attach", {"run_id": pod.run_id})
        return channel


class CheckpointBroker(worker.Broker):
    async def _call(self, row, *args, **kwargs):
        result = await super()._call(row, *args, **kwargs)
        checkpoint("after_mutation", row)
        return result


class CheckpointRepository(worker.ExecutionRepository):
    async def journal_intent(self, row, *args, **kwargs):
        sequence = await super().journal_intent(row, *args, **kwargs)
        checkpoint("before_mutation", row)
        return sequence

    async def journal_outcome(self, row, sequence, status):
        # Intent is committed and the upstream reply has arrived, but no
        # durable outcome yet: the essential ambiguous-mutation crash window.
        checkpoint("mutation_before_outcome", row)
        return await super().journal_outcome(row, sequence, status)

    async def finish(self, row, *args, **kwargs):
        checkpoint("before_result", row)
        await super().finish(row, *args, **kwargs)
        checkpoint("after_result", row)


def dump_tasks():
    # Diagnostics for a stalled crash case: every pending task's await stack.
    for task in asyncio.all_tasks():
        task.print_stack(file=sys.stderr)
    sys.stderr.flush()


async def run():
    asyncio.get_running_loop().add_signal_handler(signal.SIGUSR2, dump_tasks)
    await worker.run()


def main():
    # SIGUSR1 dumps thread stacks even when the event loop itself is blocked.
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    with (
        patch.object(worker, "KataBackend", CheckpointBackend),
        patch.object(worker, "Broker", CheckpointBroker),
        patch.object(worker, "ExecutionRepository", CheckpointRepository),
    ):
        asyncio.run(run())


if __name__ == "__main__":
    main()
