"""Run synthetic code only inside the hardened runner container; no host imports."""

import argparse
import json
import subprocess

from runner.protocol import decode, encode
from src.catalog.package import PackageFile, package_digest


def verify(image):
    source = "async def run(inputs, ctx):\n    print('diagnostic only')\n    return {'value': inputs['value'] + 1}\n"
    files = (PackageFile(path="main.py", content=source),)
    frame = {
        "type": "init",
        "protocol": 1,
        "files": [f.model_dump() for f in files],
        "digest": package_digest(files),
        "entrypoint": "main:run",
        "inputs": {"value": 41},
        "context": {},
        "limits": {"frame_bytes": 65536},
    }
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--memory=256m",
            "--cpus=0.25",
            "--pids-limit=64",
            "--tmpfs",
            "/scratch:rw,noexec,nosuid,size=32m,uid=1000,gid=1000",
            image,
        ],
        input=encode(frame, 8388608),
        capture_output=True,
        timeout=30,
        check=True,
    )
    frames = [decode(line + b"\n", 65536) for line in result.stdout.splitlines()]
    assert frames == [{"type": "ready", "protocol": 1}, {"type": "result", "result": {"value": 42}}]
    assert b"diagnostic only" in result.stderr
    print(json.dumps({"image": image, "runner_container": "verified", "kata_acceptance": False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    verify(parser.parse_args().image)


if __name__ == "__main__":
    main()
