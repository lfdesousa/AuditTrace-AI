"""MODEL-MMPROJ-PERMANENT -- the chat launcher's vision projector, on the EFFECTIVE argv.

``scripts/start-chat-llama.sh`` passes ``--mmproj <projector>`` to llama-server. The
projector path is a launcher variable (``MMPROJ_PATH``, like ``MODEL_PATH``); the file
is pinned by size and sha256 (``MMPROJ_SIZE_BYTES`` / ``MMPROJ_SHA256``; the defaults
are the pinned upstream file) and the launcher fails closed, on start and on
``--check``, when the file is absent or is not the pinned file.

Every assertion is on what the stub ``llama-server`` was exec'd with, or on the
launcher's exit code and stderr. This file brings its own stub projector and passes
its pins explicitly, so it does not depend on what ``tests/_llama_sandbox.py`` provides.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

from tests._llama_sandbox import (
    FIXTURES_DIR,
    LIVE_HOME,
    SCRIPTS_DIR,
    UNITS,
    Sandbox,
    make_apply_sandbox,
    make_sandbox,
    sha_tree,
)

CHAT = SCRIPTS_DIR / "start-chat-llama.sh"
APPLY = SCRIPTS_DIR / "apply-llama-endpoint-locks.sh"
PROJECTOR_NAME = "mmproj-Qwen3.8-27B-F16.gguf"

#: The pinned upstream file: HF unsloth/Qwen3.8-27B-GGUF, file mmproj-F16.gguf.
PINNED_REVISION = "f1bfb127c64f7072bdd2cad55f258b9c8b2910fe"
PINNED_SHA256 = "cbb841a9ee0636b2ec172f5bb8df2ea8dfeb01e90fe7c6126581d662a0b4e43e"
PINNED_SIZE = 927607488

#: sha256 of the golden fixture after this change (the pre-LC-5 capture plus the
#: projector pair; equal to the live unit's argv with the lock flags mapped back).
EXPECTED_FIXTURE_SHA256 = (
    "19ea131a44d892746367ea3b4bfe9ff80c368ae013b3ee6b2cafa25e3e986c52"
)
#: The four other fixtures do not move.
EXPECTED_OTHER_FIXTURES: dict[str, str] = {
    "llama-embed-server.argv": "2f44b3f965365ed31d8a1075bb18f5774800db1dac30973789620cf25b6938da",
    "llama-fleet.argv": "7692f89df54ea62bd1b8b95dcba55d00af4164f959482391ec887d2dce23ccb9",
    "llama-mistral-server.argv": "9ad9d1a91a717cb62fa0385636215e5810e6d33af208927c5cb3a4993378f2eb",
    "llama-summarizer.argv": "ac6dd6432a064e83e455c18b162d7dddeeccd022a5c90bfaf870c76b2737cfab",
}

STUB_BYTES = b"stub projector, not a GGUF\n"
STUB_SHA256 = hashlib.sha256(STUB_BYTES).hexdigest()
STUB_PINS = {"MMPROJ_SHA256": STUB_SHA256, "MMPROJ_SIZE_BYTES": str(len(STUB_BYTES))}
PROJECTOR_VARS = ("MMPROJ_PATH", "MMPROJ_SHA256", "MMPROJ_SIZE_BYTES")


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return make_sandbox(tmp_path)


def _projector(sandbox: Sandbox, data: bytes = STUB_BYTES) -> Path:
    path = sandbox.home / "models" / PROJECTOR_NAME
    path.write_bytes(data)
    return path


def _run(
    sandbox: Sandbox, script: Path, args: tuple[str, ...] = (), **env: str
) -> tuple[int, list[str], str, str]:
    """Run a launcher. Projector variables are passed ONLY when given here, so a
    test that omits them exercises the launcher's own defaults."""
    full = sandbox.env()
    for name in PROJECTOR_VARS:
        full.pop(name, None)
    full.update(env)
    # Own session, as ``Sandbox.run`` does: a timeout must kill the whole process
    # group, or a poll loop in a command-substitution subshell would be orphaned.
    with subprocess.Popen(  # noqa: S603 - argv is test-controlled
        ["bash", str(script), *args],
        env=full,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=sandbox.root,
        start_new_session=True,
    ) as proc:
        try:
            out, err = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise
    return proc.returncode, sandbox.captured_argv(), out, err


def _flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


class TestProjectorOnTheEffectiveArgv:
    def test_default_path_is_under_home_models(self, sandbox: Sandbox) -> None:
        path = _projector(sandbox)
        rc, argv, _, stderr = _run(sandbox, CHAT, **STUB_PINS)
        assert rc == 0, stderr
        assert _flag(argv, "--mmproj") == str(path)

    def test_mmproj_path_overrides_the_default(self, sandbox: Sandbox) -> None:
        other = sandbox.root / "elsewhere" / "projector.gguf"
        other.parent.mkdir()
        other.write_bytes(STUB_BYTES)
        rc, argv, _, stderr = _run(sandbox, CHAT, MMPROJ_PATH=str(other), **STUB_PINS)
        assert rc == 0, stderr
        assert _flag(argv, "--mmproj") == str(other)

    def test_mmproj_appears_exactly_once(self, sandbox: Sandbox) -> None:
        _projector(sandbox)
        rc, argv, _, stderr = _run(sandbox, CHAT, **STUB_PINS)
        assert rc == 0, stderr
        assert argv.count("--mmproj") == 1

    @pytest.mark.parametrize("unit", sorted(u for u in UNITS if u != "llama-server"))
    def test_no_other_launcher_passes_a_projector(
        self, sandbox: Sandbox, unit: str
    ) -> None:
        """PIN (green before and after): the projector belongs to the chat unit only."""
        _projector(sandbox)
        rc, argv, _, stderr = _run(sandbox, SCRIPTS_DIR / UNITS[unit][0], **STUB_PINS)
        assert rc == 0, stderr
        assert "--mmproj" not in argv


@pytest.mark.parametrize("args", [(), ("--check",)], ids=["start", "check"])
class TestFailsClosed:
    def test_refuses_when_the_projector_is_absent(
        self, sandbox: Sandbox, args: tuple[str, ...]
    ) -> None:
        missing = sandbox.home / "models" / PROJECTOR_NAME
        missing.unlink(missing_ok=True)  # the shared sandbox may provide a stub
        rc, argv, _, stderr = _run(sandbox, CHAT, args, **STUB_PINS)
        assert rc == 1 and argv == []
        assert f"error: projector not found at {missing}" in stderr

    def test_default_pin_refuses_a_file_of_another_size(
        self, sandbox: Sandbox, args: tuple[str, ...]
    ) -> None:
        """No pin variable is set: the launcher's OWN defaults must be the pinned
        size, named by value in the refusal."""
        _projector(sandbox)
        rc, argv, _, stderr = _run(sandbox, CHAT, args)
        assert rc == 1 and argv == []
        assert f"expected {PINNED_SIZE} bytes, found {len(STUB_BYTES)}" in stderr, (
            stderr
        )

    def test_default_pin_refuses_other_bytes_of_the_expected_size(
        self, sandbox: Sandbox, args: tuple[str, ...]
    ) -> None:
        """Size satisfied (overridden to the stub's), sha256 left at the launcher's
        default: the hash is checked on its own, and the default is the pinned hash."""
        _projector(sandbox)
        rc, argv, _, stderr = _run(
            sandbox, CHAT, args, MMPROJ_SIZE_BYTES=str(len(STUB_BYTES))
        )
        assert rc == 1 and argv == []
        assert f"expected {PINNED_SHA256}, found {STUB_SHA256}" in stderr, stderr

    def test_a_wrong_size_is_refused_before_any_hash_is_computed(
        self, sandbox: Sandbox, args: tuple[str, ...]
    ) -> None:
        """The unit restarts a refusing launcher every few seconds, for ever. A
        truncated or partial download (the likely damage) must cost one ``stat``
        per start, never a hash of the whole file."""
        real = shutil.which("sha256sum")
        assert real is not None
        log = sandbox.root / "sha256sum.calls"
        stub = sandbox.stubs / "sha256sum"
        stub.write_text(
            f'#!/usr/bin/env bash\necho called >> "{log}"\nexec "{real}" "$@"\n'
        )
        stub.chmod(0o755)
        _projector(sandbox)
        rc, argv, _, stderr = _run(sandbox, CHAT, args)
        assert rc == 1 and argv == []
        assert "projector size mismatch" in stderr
        assert not log.exists(), "the hash ran although the size already differed"
        # control: with the size satisfied the same stub IS reached
        rc, _, _, stderr = _run(
            sandbox, CHAT, args, MMPROJ_SIZE_BYTES=str(len(STUB_BYTES))
        )
        assert rc == 1 and "projector sha256 mismatch" in stderr
        assert log.exists(), "control failed: the stub sha256sum was never called"

    def test_refuses_one_flipped_byte(
        self, sandbox: Sandbox, args: tuple[str, ...]
    ) -> None:
        flipped = bytes([STUB_BYTES[0] ^ 1]) + STUB_BYTES[1:]
        _projector(sandbox, flipped)
        rc, argv, _, stderr = _run(sandbox, CHAT, args, **STUB_PINS)
        assert rc == 1 and argv == []
        assert "projector sha256 mismatch" in stderr


class TestCheckMode:
    def test_ok_line_reports_the_projector_and_stays_parseable(
        self, sandbox: Sandbox
    ) -> None:
        path = _projector(sandbox)
        rc, argv, stdout, stderr = _run(sandbox, CHAT, ("--check",), **STUB_PINS)
        assert rc == 0, stderr
        assert argv == [], "--check must stop before exec"
        binary = sandbox.home / "build/llama.cpp/build-vulkan-new/bin/llama-server"
        model = sandbox.home / "models" / "Qwen_Qwen3.8-27B-Q4_K_M.gguf"
        assert stdout.splitlines() == [
            f"launcher ok: binary={binary} model={model} mmproj={path} "
            f"bind=10.42.0.1 port=11435"
        ]
        # the apply script's preflight extracts the binary with this expression
        found = re.search(r".*binary=([^ ]*) .*", stdout)
        assert found is not None and found.group(1) == str(binary)


class TestApplyScript:
    """The operator's step: `apply-llama-endpoint-locks.sh --apply` from the merged
    checkout, on a host that still carries the temporary assessment drop-in."""

    def test_preflight_refuses_an_absent_projector_and_changes_nothing(
        self, tmp_path: Path
    ) -> None:
        sb = make_apply_sandbox(tmp_path)
        (sb.home / "models" / PROJECTOR_NAME).unlink(missing_ok=True)
        before = sha_tree(sb.systemd_dir)
        result = sb.apply(APPLY, "--apply")
        assert result.returncode == 2, result.stdout + result.stderr
        assert "llama-server" in result.stderr
        assert "projector not found" in result.stderr
        assert sha_tree(sb.systemd_dir) == before
        assert sb.mutating_calls() == [], "a refused preflight must not touch systemd"

    def test_assessment_drop_in_is_renamed_and_only_the_chat_unit_restarts(
        self, tmp_path: Path
    ) -> None:
        """PIN (green before and after): the state of the host at merge time is
        five installed units plus one ExecStart-resetting drop-in on the chat unit."""
        sb = make_apply_sandbox(tmp_path)
        assert sb.apply(APPLY, "--apply").returncode == 0
        dropin = sb.systemd_dir / "llama-server.service.d" / "90-mmproj-assess.conf"
        dropin.parent.mkdir(exist_ok=True)
        dropin.write_text(
            "[Service]\nExecStart=\nExecStart=/somewhere/private/launcher.sh\n"
        )
        (sb.state / "calls.log").write_text("")
        again = sb.apply(APPLY, "--apply")
        assert again.returncode == 0, again.stdout + again.stderr
        assert not dropin.exists()
        assert dropin.with_name(dropin.name + ".disabled-by-wu4-locks").is_file()
        assert [c for c in sb.mutating_calls() if c.startswith("restart")] == [
            "restart llama-server"
        ]
        assert again.stdout.count("already applied and verified -- not restarted") == 2


class TestGoldenFixture:
    def test_chat_fixture_is_the_measured_argv(self) -> None:
        data = (FIXTURES_DIR / "llama-server.argv").read_bytes()
        assert hashlib.sha256(data).hexdigest() == EXPECTED_FIXTURE_SHA256

    def test_chat_fixture_carries_the_projector_pair_after_the_model(self) -> None:
        tokens = (FIXTURES_DIR / "llama-server.argv").read_text().splitlines()
        at = tokens.index("--model")
        assert tokens[at + 2 : at + 4] == [
            "--mmproj",
            f"{LIVE_HOME}/models/{PROJECTOR_NAME}",
        ]

    def test_the_other_fixtures_do_not_move(self) -> None:
        """PIN (green before and after)."""
        got = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(FIXTURES_DIR.glob("*.argv"))
            if p.name != "llama-server.argv"
        }
        assert got == EXPECTED_OTHER_FIXTURES


class TestProvenanceRecord:
    def test_launcher_names_the_upstream_file_and_revision(self) -> None:
        text = CHAT.read_text()
        assert f"unsloth/Qwen3.8-27B-GGUF@{PINNED_REVISION}" in text
        assert "mmproj-F16.gguf" in text

    def test_launcher_defaults_are_the_pinned_values(self) -> None:
        text = CHAT.read_text()
        assert f'MMPROJ_SHA256="${{MMPROJ_SHA256:-{PINNED_SHA256}}}"' in text
        assert f'MMPROJ_SIZE_BYTES="${{MMPROJ_SIZE_BYTES:-{PINNED_SIZE}}}"' in text
