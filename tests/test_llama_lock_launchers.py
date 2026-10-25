"""LC-5 / M3-WU-4 -- the three launcher locks, asserted on the EFFECTIVE argv.

For every one of the five llama-server launchers
(``scripts/start-{chat,embed,summarizer,fleet,mistral}-llama.sh``) the REAL
script runs with a stub ``llama-server`` that records the argv it was exec'd
with. A ``grep`` of the script text would not prove anything: the assertions are
on what the binary would actually receive.

Locks (per endpoint):
  1. ``--host <the cni0 IPv4>`` -- from the kernel (a stubbed ``ip``), polled for
     a bounded time, never 0.0.0.0, never loopback;
  2. ``--api-key-file <path>`` -- the path on argv, never the value;
  3. ``--no-webui``.
Plus: no model / quantisation / performance flag differs from the unit's
pre-LC-5 EFFECTIVE ExecStart (``tests/fixtures/llama_live_execstart/``, captured
from ``systemctl show -p ExecStart`` with drop-ins resolved).

``TestNeuterEachLock`` is the per-lock, per-endpoint table: the shared lib is
mutated in a TMP COPY (the tracked files are hashed before and after), each
neuter turns exactly its own lock RED while the other two stay GREEN.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

from tests._llama_sandbox import (
    CNI0_ADDR,
    FIXTURES_DIR,
    KEY_VALUE,
    LIVE_HOME,
    SCRIPTS_DIR,
    UNITS,
    Sandbox,
    copy_scripts_tree,
    make_sandbox,
)

LOCK_LIB = SCRIPTS_DIR / "llama-lock-flags.lib.sh"
LAUNCHERS = {unit: SCRIPTS_DIR / name for unit, (name, _port) in UNITS.items()}
TRACKED = [LOCK_LIB, *LAUNCHERS.values()]


@pytest.fixture(autouse=True)
def _tracked_files_never_mutated():
    """Every test mutates tmp copies only; a test that touched a tracked
    launcher would leave the host's units neutered."""

    def digest(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    before = {p: digest(p) for p in TRACKED}
    yield
    assert {p: digest(p) for p in TRACKED} == before, (
        "a test mutated a tracked launcher"
    )


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return make_sandbox(tmp_path)


# ───────────────────────────── argv helpers ──────────────────────────────


def _pairs(tokens: list[str]) -> list[tuple[str, str | None]]:
    """['--a','1','--b'] -> [('--a','1'),('--b',None)] (a value never starts
    with '--'), sorted so flag ORDER does not matter (it does not to llama)."""
    out: list[tuple[str, str | None]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        assert tok.startswith("--"), f"unexpected positional token {tok!r}"
        if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
            out.append((tok, tokens[i + 1]))
            i += 2
        else:
            out.append((tok, None))
            i += 1
    return sorted(out, key=lambda p: (p[0], p[1] or ""))


def _without_locks(pairs: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    return [p for p in pairs if p[0] not in ("--host", "--api-key-file", "--no-webui")]


def _flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def _assert_lock_host(argv: list[str]) -> None:
    host = _flag(argv, "--host")
    assert host == CNI0_ADDR, (
        f"lock 1 (bind) failed: --host is {host!r}, not {CNI0_ADDR}"
    )


def _assert_lock_api_key(argv: list[str], key_file: Path) -> None:
    assert _flag(argv, "--api-key-file") == str(key_file), (
        "lock 2 (api-key) failed: --api-key-file missing or not the configured key file"
    )
    assert "--api-key" not in argv, "lock 2 failed: literal --api-key on argv"
    assert KEY_VALUE not in argv, "lock 2 failed: the key VALUE is on argv"


def _assert_lock_no_webui(argv: list[str]) -> None:
    assert "--no-webui" in argv, "lock 3 (no-webui) failed: --no-webui missing"


def _run_launcher(
    script: Path, sandbox: Sandbox, args: list[str] | None = None, **env: str
) -> tuple[int, list[str], str]:
    result = sandbox.run(["bash", str(script), *(args or [])], **env)
    return result.returncode, sandbox.captured_argv(), result.stderr


# ───────────────────── effective invocation, happy path ─────────────────────


@pytest.mark.parametrize("unit", sorted(UNITS))
class TestEffectiveInvocation:
    def test_all_three_locks_on_the_effective_argv(self, unit, sandbox):
        rc, argv, stderr = _run_launcher(LAUNCHERS[unit], sandbox)
        assert rc == 0, stderr
        assert argv, "the stub llama-server was never exec'd"
        _assert_lock_host(argv)
        _assert_lock_api_key(argv, sandbox.key_file)
        _assert_lock_no_webui(argv)
        assert "0.0.0.0" not in argv

    def test_no_model_or_performance_flag_differs_from_the_pre_lc5_unit(
        self, unit, sandbox
    ):
        """Parity with the golden fixture: the unit's EFFECTIVE pre-LC-5
        ExecStart (drop-ins resolved) minus ``--host 0.0.0.0`` equals the
        launcher's argv minus the three locks. Binary and every model/perf
        flag, value for value."""
        rc, argv, stderr = _run_launcher(LAUNCHERS[unit], sandbox)
        assert rc == 0, stderr
        golden = (FIXTURES_DIR / f"{unit}.argv").read_text().splitlines()
        golden = [t.replace(LIVE_HOME, str(sandbox.home)) for t in golden]
        assert argv[0] == golden[0], "the launcher runs a different llama-server binary"
        got = _without_locks(_pairs(argv[1:]))
        want = [p for p in _pairs(golden[1:]) if p != ("--host", "0.0.0.0")]
        assert got == want

    def test_fails_closed_without_a_key_file(self, unit, sandbox):
        sandbox.key_file.unlink()
        rc, argv, stderr = _run_launcher(LAUNCHERS[unit], sandbox)
        assert rc != 0 and argv == []
        assert "llama API key file not found" in stderr

    def test_fails_closed_without_the_cni0_address(self, unit, sandbox):
        rc, argv, stderr = _run_launcher(LAUNCHERS[unit], sandbox, IP_STUB_ADDR="")
        assert rc != 0 and argv == []
        assert "no IPv4 address on cni0" in stderr

    @pytest.mark.parametrize("bad", ["0.0.0.0", "127.0.0.1", "127.9.9.9"])
    def test_refuses_wildcard_and_loopback_addresses(self, unit, sandbox, bad):
        rc, argv, stderr = _run_launcher(LAUNCHERS[unit], sandbox, IP_STUB_ADDR=bad)
        assert rc != 0 and argv == []
        assert "refusing to bind" in stderr

    def test_check_mode_validates_everything_and_never_execs(self, unit, sandbox):
        rc, argv, stderr = _run_launcher(LAUNCHERS[unit], sandbox, ["--check"])
        assert rc == 0, stderr
        assert argv == [], "--check must stop before exec"

    def test_check_mode_still_fails_on_a_missing_model(self, unit, sandbox):
        for model in (sandbox.home / "models").iterdir():
            model.unlink()
        rc, argv, stderr = _run_launcher(LAUNCHERS[unit], sandbox, ["--check"])
        assert rc != 0 and argv == []
        assert "model not found" in stderr


# ─────────────────────────── bounded poll for cni0 ───────────────────────────


class TestBoundedBindPoll:
    def test_waits_for_cni0_in_two_second_steps_then_binds(self, sandbox):
        """cni0 appears ~1 s after k3s reports up (measured at boot): the
        launcher polls instead of failing on the first miss."""
        rc, argv, stderr = _run_launcher(
            LAUNCHERS["llama-embed-server"],
            sandbox,
            LLAMA_BIND_POLL_MAX_SECONDS="120",
            IP_STUB_AFTER="2",
            IP_STUB_COUNT=str(sandbox.root / "ip.count"),
        )
        assert rc == 0, stderr
        _assert_lock_host(argv)
        assert sandbox.sleeps() == ["2", "2"]

    def test_the_bind_interface_is_read_by_name_and_defaults_to_cni0(self, sandbox):
        """Lock 1 reads the address of ONE named interface (default cni0).
        Pointing the launcher at another interface changes what it binds;
        leaving the default while only another interface has an address
        refuses to start."""
        script = LAUNCHERS["llama-embed-server"]
        rc, argv, _ = _run_launcher(
            script, sandbox, IP_STUB_IFACE="flannel.1", LLAMA_BIND_IFACE="flannel.1"
        )
        assert rc == 0 and _flag(argv, "--host") == CNI0_ADDR
        sandbox.capture.unlink()
        rc, argv, stderr = _run_launcher(script, sandbox, IP_STUB_IFACE="flannel.1")
        assert rc != 0 and argv == [] and "no IPv4 address on cni0" in stderr

    def test_gives_up_after_the_bound(self, sandbox):
        rc, argv, stderr = _run_launcher(
            LAUNCHERS["llama-embed-server"],
            sandbox,
            LLAMA_BIND_POLL_MAX_SECONDS="6",
            IP_STUB_ADDR="",
        )
        assert rc != 0 and argv == []
        assert sandbox.sleeps() == ["2", "2", "2"], (
            "the poll must be bounded at max/step sleeps"
        )
        assert "after 6s" in stderr

    @pytest.mark.parametrize(
        ("max_s", "step"), [("abc", "2"), ("-1", "2"), ("10", "0"), ("10", "x")]
    )
    def test_rejects_nonsense_poll_settings(self, sandbox, max_s, step):
        rc, argv, stderr = _run_launcher(
            LAUNCHERS["llama-embed-server"],
            sandbox,
            LLAMA_BIND_POLL_MAX_SECONDS=max_s,
            LLAMA_BIND_POLL_STEP_SECONDS=step,
        )
        assert rc != 0 and argv == []
        assert "must be integers" in stderr


# ──────────────────────── per-lock, per-endpoint neuters ────────────────────────
#
# One neuter per lock, applied to a TMP COPY of the shared lib and run through
# EACH of the five launchers (15 rows). The targeted lock's check must go RED and
# the other two must stay GREEN, proving the guard is specific.

_ANCHOR = 'LLAMA_LOCK_FLAGS=(--host "$LLAMA_BIND_ADDR" --api-key-file "$LLAMA_API_KEY_FILE" --no-webui)'
_NEUTERS = {
    "host": 'LLAMA_LOCK_FLAGS=(--host 0.0.0.0 --api-key-file "$LLAMA_API_KEY_FILE" --no-webui)',
    "api_key": 'LLAMA_LOCK_FLAGS=(--host "$LLAMA_BIND_ADDR" --no-webui)',
    "no_webui": 'LLAMA_LOCK_FLAGS=(--host "$LLAMA_BIND_ADDR" --api-key-file "$LLAMA_API_KEY_FILE")',
}


def _check(lock: str, argv: list[str], key_file: Path) -> None:
    if lock == "host":
        _assert_lock_host(argv)
    elif lock == "api_key":
        _assert_lock_api_key(argv, key_file)
    else:
        _assert_lock_no_webui(argv)


@pytest.mark.parametrize("unit", sorted(UNITS))
@pytest.mark.parametrize("lock", sorted(_NEUTERS))
def test_neutering_one_lock_turns_only_that_lock_red(lock, unit, sandbox):
    pristine = LOCK_LIB.read_text()
    assert _ANCHOR in pristine, "neuter anchor drifted from the real lib"
    tree = copy_scripts_tree(sandbox.root / "tree")
    (tree / LOCK_LIB.name).write_text(pristine.replace(_ANCHOR, _NEUTERS[lock]))
    rc, argv, stderr = _run_launcher(tree / LAUNCHERS[unit].name, sandbox)
    assert rc == 0, f"the neutered launcher should still start: {stderr}"
    with pytest.raises(AssertionError):
        _check(lock, argv, sandbox.key_file)
    for other in _NEUTERS:
        if other != lock:
            _check(other, argv, sandbox.key_file)


_GUARD_NEUTERS = {
    # name -> (anchor in the lib, replacement)
    "refuse_wildcard_and_loopback": (
        'if [[ ! "$LLAMA_BIND_ADDR" =~ ^([0-9]{1,3}\\.){3}[0-9]{1,3}$ || "$LLAMA_BIND_ADDR" == 0.* || "$LLAMA_BIND_ADDR" == 127.* ]]; then',
        "if false; then",
    ),
    "poll_is_bounded": ("if (( waited >= max )); then", "if false; then"),
    "key_file_required": (
        'if [[ ! -f "$LLAMA_API_KEY_FILE" ]]; then',
        "if false; then",
    ),
}


@pytest.mark.parametrize("guard", sorted(_GUARD_NEUTERS))
def test_neutering_a_fail_closed_guard_is_caught(guard, sandbox):
    """The three fail-closed guards (loopback/wildcard refusal, the poll bound,
    the key-file requirement) each have a test that fails when the guard is
    removed from a tmp copy of the lib."""
    anchor, replacement = _GUARD_NEUTERS[guard]
    pristine = LOCK_LIB.read_text()
    assert anchor in pristine, "guard anchor drifted from the real lib"
    tree = copy_scripts_tree(sandbox.root / "tree")
    (tree / LOCK_LIB.name).write_text(pristine.replace(anchor, replacement))
    script = tree / LAUNCHERS["llama-embed-server"].name

    if guard == "refuse_wildcard_and_loopback":
        rc, argv, _ = _run_launcher(script, sandbox, IP_STUB_ADDR="127.0.0.1")
        survived = rc == 0 and argv != []
    elif guard == "key_file_required":
        sandbox.key_file.unlink()
        rc, argv, _ = _run_launcher(script, sandbox)
        survived = rc == 0 and argv != []
    else:
        try:
            sandbox.run(
                ["bash", str(script)],
                timeout=3,
                LLAMA_BIND_POLL_MAX_SECONDS="6",
                IP_STUB_ADDR="",
            )
            survived = False
        except subprocess.TimeoutExpired:
            survived = True  # an unbounded poll never returns
    assert survived, (
        f"neutering the {guard} guard was NOT detected by the launcher tests"
    )


def test_the_pristine_guards_do_not_survive_the_same_scenarios(sandbox):
    """Control for the table above: with the real lib every scenario is refused."""
    rc, argv, _ = _run_launcher(
        LAUNCHERS["llama-embed-server"], sandbox, IP_STUB_ADDR="127.0.0.1"
    )
    assert rc != 0 and argv == []
    sandbox.key_file.unlink()
    rc, argv, _ = _run_launcher(LAUNCHERS["llama-embed-server"], sandbox)
    assert rc != 0 and argv == []


# ───────────────────────── the key generator (S2) ─────────────────────────

GENERATOR = SCRIPTS_DIR / "generate-llama-api-key.sh"
_KEY_RE = re.compile(r"^[A-Za-z0-9]{48}$")


class TestKeyGeneratorRecipe:
    def test_200_generated_keys_all_match_the_alphabet_and_length(self, sandbox):
        """Length and character class only: the contents are never printed or
        compared, and a failing run reports counts, not values."""
        bad = 0
        for i in range(200):
            target = sandbox.root / "keys" / f"k{i}.txt"
            result = sandbox.run(["bash", str(GENERATOR), str(target)])
            if result.returncode != 0 or not _KEY_RE.match(target.read_text().strip()):
                bad += 1
        assert bad == 0, (
            f"{bad}/200 generated keys violated ^[A-Za-z0-9]{{48}}$ or the generator failed"
        )

    def test_key_file_is_mode_0600_and_value_is_not_echoed(self, sandbox):
        target = sandbox.root / "keys" / "one.txt"
        result = sandbox.run(["bash", str(GENERATOR), str(target)])
        assert result.returncode == 0
        assert oct(target.stat().st_mode & 0o777) == "0o600"
        assert target.read_text().strip() not in result.stdout + result.stderr

    def test_never_overwrites_an_existing_key(self, sandbox):
        before = sandbox.key_file.read_bytes()
        result = sandbox.run(["bash", str(GENERATOR), str(sandbox.key_file)])
        assert result.returncode != 0
        assert sandbox.key_file.read_bytes() == before

    def test_neuter_32_byte_draw_is_caught(self, sandbox):
        """A 32-byte draw yields only 38-43 alphanumerics (measured 0/200
        matches). With the draw cut to 32 the generator must refuse (non-zero
        exit) -- so the 200/200 test above would go RED."""
        pristine = GENERATOR.read_text()
        anchor = "head -c 96 /dev/urandom"
        assert anchor in pristine
        copy = sandbox.root / "gen32.sh"
        copy.write_text(pristine.replace(anchor, "head -c 32 /dev/urandom"))
        bad = 0
        for i in range(20):
            target = sandbox.root / "keys32" / f"k{i}.txt"
            result = sandbox.run(["bash", str(copy), str(target)])
            if result.returncode != 0 or not target.exists():
                bad += 1
        assert bad >= 19, (
            f"the 32-byte neuter produced {20 - bad}/20 valid keys; the recipe test would NOT go red"
        )
