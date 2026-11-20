"""The suite's ephemeral Postgres containers must not leave their data volume.

The ``postgres`` image declares a ``VOLUME`` for its data directory, so every
container gets an ANONYMOUS volume. ``docker rm -f`` without ``-v`` removes the
container and leaves that volume behind: 3,835 dangling anonymous volumes (494
in one day, 281 GB) filled the laptop's disk and put the k3s node into
DiskPressure. These tests run ONE real container lifecycle each and assert on
the volume(s) of THAT container only (found through ``docker inspect``), never
on a global volume count, so they are safe under pytest-xdist.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from scripts.neuter.pg import start_container, stop_container
from tests import _pg_ephemeral

_DOCKER = shutil.which("docker") is not None
if _DOCKER:
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=10)
    except Exception:
        _DOCKER = False

requires_docker = pytest.mark.skipif(not _DOCKER, reason="docker unavailable")


def _volumes_of(container: str) -> list[str]:
    """Names of the volumes mounted into ``container`` (anonymous or named)."""
    out = subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}\n{{end}}{{end}}',
            container,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def _volume_exists(name: str) -> bool:
    return (
        subprocess.run(
            ["docker", "volume", "inspect", name], capture_output=True
        ).returncode
        == 0
    )


def _assert_volumes_gone(volumes: list[str]) -> None:
    leftovers = [v for v in volumes if _volume_exists(v)]
    # Remove only what THIS test created, so a RED run does not add to the leak.
    for volume in leftovers:
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True)
    assert leftovers == [], f"container removal left its volume(s) behind: {leftovers}"


@requires_docker
def test_pg_ephemeral_removal_takes_the_anonymous_data_volume():
    """Neuter: ``docker rm -f`` instead of ``-fv`` in remove_ephemeral_container
    -> the data volume survives and this goes RED."""
    name_prefix = "audittrace-voltest-pg-"
    dsn = _pg_ephemeral.start_ephemeral_postgres(name_prefix, "pw", "voltest")
    assert dsn is not None, "docker is available but the container did not start"
    name = f"{name_prefix}{os.getpid()}"
    volumes = _volumes_of(name)
    assert volumes, "no volume to track: the test would pass vacuously"

    _pg_ephemeral.remove_ephemeral_container(name)

    _assert_volumes_gone(volumes)


@requires_docker
def test_neuter_pool_stop_container_takes_the_anonymous_data_volume():
    """Same lifecycle for the neuter pool's own Postgres container."""
    handle = start_container(f"t-pg-vol-{os.getpid()}", 98)
    volumes = _volumes_of(handle.name)
    assert volumes, "no volume to track: the test would pass vacuously"

    stop_container(handle)

    _assert_volumes_gone(volumes)
