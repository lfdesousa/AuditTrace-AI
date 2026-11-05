"""scripts/ci/kind-load-image.sh and its use in the content-control kind smoke.

Unit tests. MOCKS (declared): `skopeo` and `kind` are stub executables on a
scratch PATH. They record their argv and exit with a chosen code; no
registry or cluster is contacted. The live proof is the
integration-content-control CI job itself (and the local reproduction in
the release evidence, 2026-11-05).
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

from tests._hermetic_env import override_sentinel

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ci" / "kind-load-image.sh"
WORKFLOW = REPO / ".github" / "workflows" / "integration-content-control.yml"
IMG = "ghcr.io/lfdesousa/audittrace-chroma:1.5.7-frozen-sep26"

_STUB = """#!/bin/bash
printf '%s\\n' "$*" >> "{log}"
if [ "$(basename "$0")" = skopeo ]; then
  for a in "$@"; do
    case "$a" in
      docker-archive:*) p="${{a#docker-archive:}}"; p="${{p%%:*}}"; : > "$p" ;;
    esac
  done
  exit "${{STUB_SKOPEO_RC:-0}}"
fi
exit "${{STUB_KIND_RC:-0}}"
"""


def _bin(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for name in ("skopeo", "kind"):
        tool = bin_dir / name
        tool.write_text(_STUB.format(log=log))
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return bin_dir, log


def _run(
    tmp_path: Path, *args: str, skopeo_rc: int = 0, kind_rc: int = 0
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir, log = _bin(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "HOME": str(home),
        "STUB_SKOPEO_RC": str(skopeo_rc),
        "STUB_KIND_RC": str(kind_rc),
    }
    with override_sentinel(shim_dir=str(bin_dir), kubeconfig=None, required=False):
        proc = subprocess.run(
            ["bash", str(SCRIPT), *args],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def test_the_image_is_copied_amd64_only_into_an_archive_and_loaded(
    tmp_path: Path,
) -> None:
    proc, calls = _run(tmp_path, IMG, "integration-cc")
    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 2
    skopeo, kind = calls
    assert skopeo.startswith("--override-os linux --override-arch amd64 copy ")
    assert f"docker://{IMG}" in skopeo
    archive = re.search(r"docker-archive:(\S+?\.tar):", skopeo)
    assert archive is not None
    assert skopeo.endswith(f":{IMG}")
    assert kind == f"load image-archive {archive.group(1)} --name integration-cc"
    assert f"loaded {IMG}" in proc.stdout


def test_the_docker_login_file_is_passed_when_present(tmp_path: Path) -> None:
    docker_dir = tmp_path / "home" / ".docker"
    docker_dir.mkdir(parents=True)
    (docker_dir / "config.json").write_text("{}")
    bin_dir, log = _bin(tmp_path)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
    }
    with override_sentinel(shim_dir=str(bin_dir), kubeconfig=None, required=False):
        proc = subprocess.run(
            ["bash", str(SCRIPT), IMG, "c"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    assert proc.returncode == 0, proc.stderr
    assert f"--authfile {docker_dir / 'config.json'}" in log.read_text()


def test_a_failed_copy_fails_closed_and_nothing_is_loaded(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, IMG, "integration-cc", skopeo_rc=1)
    assert proc.returncode == 1
    assert len(calls) == 1
    assert calls[0].startswith("--override-os linux")


def test_a_failed_load_fails_closed(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, IMG, "integration-cc", kind_rc=1)
    assert proc.returncode == 1
    assert len(calls) == 2
    assert "loaded" not in proc.stdout


def test_missing_arguments_are_refused(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, IMG)
    assert proc.returncode != 0
    assert "usage: kind-load-image.sh IMAGE CLUSTER" in proc.stderr
    assert calls == []


def test_the_scratch_archive_is_removed_on_exit(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, IMG, "integration-cc")
    assert proc.returncode == 0
    archive = re.search(r"docker-archive:(\S+?\.tar):", calls[0])
    assert archive is not None
    assert not Path(archive.group(1)).exists()


def test_the_kind_smoke_loads_every_chart_image_through_the_script_fail_closed() -> (
    None
):
    text = WORKFLOW.read_text()
    assert text.count('scripts/ci/kind-load-image.sh "$img" integration-cc') == 2
    assert "::warning::failed to" not in text
    assert "docker pull" not in text.replace("docker pull below", "")
    # Only the two images this job builds itself are loaded with docker-image.
    direct = re.findall(r"kind load docker-image (\S+)", text)
    assert direct == ["audittrace-memory-server:ci", "audittrace-llm-stub:ci"]


def test_the_script_is_strict_and_executable() -> None:
    body = SCRIPT.read_text()
    assert "set -euo pipefail" in body
    assert os.access(SCRIPT, os.X_OK)
