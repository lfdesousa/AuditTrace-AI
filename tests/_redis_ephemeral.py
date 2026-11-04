"""Throwaway real-Redis scaffolding for the tool-result cache tests.

Prefixed with ``_`` so pytest never collects it. Mirrors
``tests/_pg_ephemeral.py`` (``free_port``, ``run_label_args``): the container
carries the run-tests label so ``scripts/ci/run-tests.sh`` can reap it, has an
explicit ``--name``, and is removed with ``docker rm -fv`` when the context
manager exits. ``atexit`` is a backstop only.

Docker being unavailable is an error, never a skip (zero-skip policy).
"""

from __future__ import annotations

import atexit
import contextlib
import os
import subprocess
import time
from collections.abc import Iterator

from tests._pg_ephemeral import free_port, run_label_args

#: The chart's rendered Redis image (one constant; same on every chart cell).
REDIS_TEST_IMAGE = "docker.io/bitnamilegacy/redis:7.2.5-debian-12-r4"


def _remove(name: str) -> None:
    subprocess.run(["docker", "rm", "-fv", name], capture_output=True, check=False)


@contextlib.contextmanager
def ephemeral_redis(name_prefix: str) -> Iterator[str]:
    """Run a throwaway Redis and yield its ``redis://`` URL.

    Raises ``RuntimeError`` when Docker or the container is unavailable.
    """
    port = free_port()
    name = f"{name_prefix}{os.getpid()}"
    atexit.register(_remove, name)
    try:
        run = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                *run_label_args(),
                "-e",
                "ALLOW_EMPTY_PASSWORD=yes",
                "-p",
                f"127.0.0.1:{port}:6379",
                REDIS_TEST_IMAGE,
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if run.returncode != 0:
            raise RuntimeError(f"docker run failed: {run.stderr.strip()}")
        deadline = time.monotonic() + 60
        while True:
            ping = subprocess.run(
                ["docker", "exec", name, "redis-cli", "ping"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if ping.stdout.strip() == "PONG":
                break
            if time.monotonic() > deadline:
                raise RuntimeError("ephemeral redis did not answer PONG within 60 s")
            time.sleep(0.25)
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        _remove(name)
