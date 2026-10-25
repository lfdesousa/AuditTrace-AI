"""Shared sandbox for the LC-5 / M3-WU-4 shell-script tests.

Everything the launchers and the apply script touch on the real host
(``ip``, ``systemctl``, ``ss``, ``sleep``, the llama binaries, the models, the
key file, ``/proc``, the CoreDNS ConfigMap) is replaced by a stub that lives
under ``tmp_path``. Nothing outside ``tmp_path`` is ever read or written, no
unit is installed, no process is started other than the stubs themselves.

The child environment goes through ``tests/_hermetic_env.hermetic_env`` so the
hermetic cluster-tool guard's env check (frozen shim dir first on ``PATH``) is
satisfied; the stub dir is inserted SECOND on ``PATH``. ``kubectl`` is never
named on ``PATH``: the apply script takes ``KUBECTL_BIN`` (an absolute path to a
stub).
"""

from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tests._hermetic_env import hermetic_env

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "llama_live_execstart"

#: unit -> (launcher basename, port). Mirrors the units in scripts/systemd/.
UNITS: dict[str, tuple[str, int]] = {
    "llama-server": ("start-chat-llama.sh", 11435),
    "llama-embed-server": ("start-embed-llama.sh", 11436),
    "llama-summarizer": ("start-summarizer-llama.sh", 11437),
    "llama-fleet": ("start-fleet-llama.sh", 11438),
    "llama-mistral-server": ("start-mistral-llama.sh", 11437),
}
ACTIVE = ("llama-embed-server", "llama-summarizer", "llama-server")
DORMANT = ("llama-fleet", "llama-mistral-server")

LIVE_HOME = "/home/lfdesousa"
MODELS = (
    "Qwen_Qwen3.8-27B-Q4_K_M.gguf",
    "nomic-embed-text-v1.5.Q8_0.gguf",
    "Mistral-7B-Instruct-v0.3-Q4_K_M.gguf",
    "mistralai_Mistral-Small-3.1-24B-Instruct-2503-Q4_K_M.gguf",
)
KEY_VALUE = "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7cF0hJ3kL6nP9rS2uV5xY8"[:48]
CNI0_ADDR = "10.42.0.1"

STUB_LLAMA = """#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then
  echo "ggml_vulkan: stub device line" >&2
  echo "version: 10288 (360e1349f)"
  exit 0
fi
{ printf '%s\\n' "$0"; printf '%s\\n' "$@"; } > "${CAPTURE_FILE:?}"
"""

STUB_IP = """#!/usr/bin/env bash
# `ip -4 -o addr show <iface>` (the only form the scripts use)
[[ "${!#}" == "${IP_STUB_IFACE:-cni0}" ]] || exit 0   # answers only for the configured interface
if [[ -n "${IP_STUB_AFTER:-}" ]]; then
  n="$(cat "${IP_STUB_COUNT:?}" 2>/dev/null || echo 0)"
  echo $(( n + 1 )) > "$IP_STUB_COUNT"
  (( n < IP_STUB_AFTER )) && exit 0
fi
if [[ -n "${IP_STUB_ADDR:-}" ]]; then
  echo "5: $4    inet ${IP_STUB_ADDR}/24 brd 10.42.0.255 scope global $4\\       valid_lft forever preferred_lft forever"
fi
"""

STUB_SLEEP = """#!/usr/bin/env bash
echo "$1" >> "${SLEEP_LOG:?}"
"""


def _write_exec(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def sha_tree(root: Path) -> dict[str, str]:
    """relative path -> sha256 of every file under root (symlinks followed)."""
    import hashlib

    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


@dataclass
class Sandbox:
    root: Path
    home: Path
    stubs: Path
    key_file: Path
    capture: Path
    sleep_log: Path

    # ---- environment -------------------------------------------------------
    def env(self, **overrides: str) -> dict[str, str]:
        base = hermetic_env()
        for name in list(base):
            if name.startswith(("LLAMA_", "IP_STUB", "SECRETS_DIR")) or name in (
                "PORT",
                "MODEL_PATH",
                "CHAT_TEMPLATE",
                "SYSTEMD_DIR",
                "BACKUP_ROOT",
                "PROC_ROOT",
                "KUBECTL_BIN",
            ):
                del base[name]
        parts = base["PATH"].split(os.pathsep)
        base["PATH"] = os.pathsep.join([parts[0], str(self.stubs), *parts[1:]])
        base.update(
            HOME=str(self.home),
            CAPTURE_FILE=str(self.capture),
            SLEEP_LOG=str(self.sleep_log),
            IP_STUB_ADDR=CNI0_ADDR,
            LLAMA_BIND_POLL_MAX_SECONDS="0",
        )
        base.update(overrides)
        return base

    def run(
        self, argv: list[str], timeout: int = 30, **overrides: str
    ) -> subprocess.CompletedProcess[str]:
        """Run in its OWN session so a timeout kills the whole process group:
        a neutered poll loop lives in a command-substitution subshell that
        `subprocess.run`'s kill of the direct child would orphan (and it would
        then spin forever against the no-op `sleep` stub)."""
        proc = subprocess.Popen(  # noqa: S603 - argv is test-controlled
            argv,
            env=self.env(**overrides),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self.root,
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)

    # ---- results -----------------------------------------------------------
    def captured_argv(self) -> list[str]:
        return self.capture.read_text().splitlines() if self.capture.exists() else []

    def sleeps(self) -> list[str]:
        return self.sleep_log.read_text().split() if self.sleep_log.exists() else []


def make_sandbox(tmp_path: Path) -> Sandbox:
    home = tmp_path / "home"
    stubs = tmp_path / "stubs"
    for rel in (
        "build/llama.cpp/build-vulkan-new/bin/llama-server",
        "opt/llamacpp/llama-server",
    ):
        _write_exec(home / rel, STUB_LLAMA)
    for model in MODELS:
        (home / "models").mkdir(parents=True, exist_ok=True)
        (home / "models" / model).write_text("")
    tmpl = (
        home
        / "build/llama.cpp/models/templates/Mistral-Small-3.2-24B-Instruct-2506.jinja"
    )
    tmpl.parent.mkdir(parents=True, exist_ok=True)
    tmpl.write_text("")
    key_file = home / "work/audittrace-private/secrets/llama-api-key.txt"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(KEY_VALUE + "\n")
    key_file.chmod(0o600)
    _write_exec(stubs / "ip", STUB_IP)
    _write_exec(stubs / "sleep", STUB_SLEEP)
    return Sandbox(
        root=tmp_path,
        home=home,
        stubs=stubs,
        key_file=key_file,
        capture=tmp_path / "capture.txt",
        sleep_log=tmp_path / "sleep.log",
    )


def copy_scripts_tree(dest: Path) -> Path:
    """Byte-identical copy of the whole scripts/ tree (launchers, libs, units,
    the apply script) -- the neuter tests mutate THIS copy, never the tracked
    files, and run the copy."""
    target = dest / "scripts"
    shutil.copytree(
        SCRIPTS_DIR,
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        symlinks=True,
    )
    return target


# ───────────────────── the apply-script sandbox (systemd, ss, /proc) ─────────────────────

#: pre-LC-5 unit bodies (abridged from `systemctl cat`): inline ExecStart on
#: 0.0.0.0, the chat unit carrying an ExecStart-resetting drop-in.
_OLD_UNIT = """[Unit]
Description=llama.cpp {unit} (pre-LC-5)
After=network.target

[Service]
Type=simple
User=lfdesousa
Group=lfdesousa
ExecStart={binary} --model {model} --port {port} --host 0.0.0.0
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
_OLD_DROPIN = """[Service]
ExecStart=
ExecStart={binary} --model /home/lfdesousa/models/Qwen_Qwen3.8-27B-Q4_K_M.gguf --host 0.0.0.0 --port 11435
"""

STUB_SYSTEMCTL = r"""#!/usr/bin/env bash
# Stateful systemctl stand-in. State lives under $SC_STATE; nothing real is touched.
set -u
echo "$*" >> "$SC_STATE/calls.log"
effective_execstart() {  # <unit> -> the last non-empty ExecStart= of the LOADED unit + drop-ins
  local u="$1" f val=""
  for f in "$SC_STATE/loaded/$u.service" "$SC_STATE/loaded/$u.service.d"/*.conf; do
    [[ -f "$f" ]] || continue
    while IFS= read -r line; do
      case "$line" in
        ExecStart=) val="" ;;
        ExecStart=*) val="${line#ExecStart=}" ;;
      esac
    done < "$f"
  done
  printf '%s' "$val"
}
unit_env_key() {  # <unit> -> LLAMA_API_KEY_FILE from the loaded unit
  sed -n 's/^Environment="LLAMA_API_KEY_FILE=\(.*\)"$/\1/p' "$SC_STATE/loaded/$1.service"
}
declare -A PORTS=([llama-server]=11435 [llama-embed-server]=11436 [llama-summarizer]=11437 [llama-fleet]=11438 [llama-mistral-server]=11437)
snapshot() {
  rm -rf "$SC_STATE/loaded"; mkdir -p "$SC_STATE/loaded"
  cp -a "$SYSTEMD_DIR"/. "$SC_STATE/loaded"/
}
case "${1:-}" in
  daemon-reload) snapshot ;;
  show)
    # show -p ExecStart <unit>   |   show -p MainPID --value <unit>
    prop="$3"; unit="${*: -1}"
    if [[ "$prop" == "ExecStart" ]]; then
      val="$(effective_execstart "$unit")"
      [[ -n "$val" ]] && echo "ExecStart={ path=${val%% *} ; argv[]=$val ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
    else
      cat "$SC_STATE/pid-$unit" 2>/dev/null || echo 0
    fi ;;
  restart)
    unit="$2"; port="${PORTS[$unit]}"
    val="$(effective_execstart "$unit")"; path="${val%% *}"
    pid=$(( 4000 + RANDOM % 4000 )); echo "$pid" > "$SC_STATE/pid-$unit"
    mkdir -p "$PROC_ROOT/$pid"
    if [[ "$path" == */start-*-llama.sh ]]; then
      binary="$HOME/build/llama.cpp/build-vulkan-new/bin/llama-server"
      addr="$IP_STUB_ADDR"
      flags=(--host "${SC_WRONG_HOST:-$addr}")
      [[ -z "${SC_OMIT_KEYFILE:-}" ]] && flags+=(--api-key-file "$(unit_env_key "$unit")")
      [[ -z "${SC_OMIT_NOWEBUI:-}" ]] && flags+=(--no-webui)
      [[ -n "${SC_LISTEN_ANY:-}" ]] && addr="0.0.0.0"
      printf '%s\0' "$binary" --model m "${flags[@]}" --port "$port" > "$PROC_ROOT/$pid/cmdline"
    else
      binary="${path:-/bin/false}"; addr="0.0.0.0"
      printf '%s\0' "$binary" --model m --host 0.0.0.0 --port "$port" > "$PROC_ROOT/$pid/cmdline"
    fi
    ln -sf "$HOME/build/llama.cpp/build-vulkan-new/bin/llama-server" "$PROC_ROOT/$pid/exe"
    echo "$addr:$port" > "$SC_STATE/listen-$port" ;;
  is-enabled)
    case "$2" in llama-fleet|llama-mistral-server) echo disabled ;; *) echo enabled ;; esac ;;
  is-active)
    if [[ -f "$SC_STATE/force-active-$2" ]]; then echo active
    else case "$2" in llama-fleet|llama-mistral-server) echo inactive ;; *) echo active ;; esac; fi ;;
  *) : ;;
esac
exit 0
"""

STUB_SS = r"""#!/usr/bin/env bash
# `ss -ltnH "sport = :<port>"`
port="${*: -1}"; port="${port##*:}"
f="$SC_STATE/listen-$port"
[[ -f "$f" ]] && echo "LISTEN 0 4096 $(cat "$f") 0.0.0.0:*"
exit 0
"""

STUB_NODEHOSTS = r"""#!/usr/bin/env bash
[[ "${KUBECTL_FAIL:-0}" == "1" ]] && exit 1
printf '%s\n' "${KUBECTL_NODEHOSTS:-10.42.0.1 host.audittrace.local}"
"""

STUB_RUNUSER = r"""#!/usr/bin/env bash
# runuser -u <user> -- <cmd...>  : log, then run as the current (test) user
echo "$2" >> "$SC_STATE/runuser.log"
shift 3
exec "$@"
"""


@dataclass
class ApplySandbox(Sandbox):
    systemd_dir: Path = Path()
    state: Path = Path()
    proc_root: Path = Path()
    backup_root: Path = Path()
    nodehosts_reader: Path = Path()

    def apply_env(self, **overrides: str) -> dict[str, str]:
        return self.env(
            SYSTEMD_DIR=str(self.systemd_dir),
            SC_STATE=str(self.state),
            PROC_ROOT=str(self.proc_root),
            BACKUP_ROOT=str(self.backup_root),
            KUBECTL_BIN=str(self.nodehosts_reader),
            LLAMA_APPLY_ALLOW_NONROOT="1",
            LLAMA_SERVICE_HOME=str(self.home),
            LLAMA_APPLY_VERIFY_TIMEOUT="2",
            LLAMA_APPLY_VERIFY_STEP="1",
            **overrides,
        )

    def apply(
        self, script: Path, *args: str, timeout: int = 90, **overrides: str
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.Popen(  # noqa: S603 - argv is test-controlled
            ["bash", str(script), *args],
            env=self.apply_env(**overrides),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self.root,
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)

    def calls(self) -> list[str]:
        f = self.state / "calls.log"
        return f.read_text().splitlines() if f.exists() else []

    def mutating_calls(self) -> list[str]:
        return [
            c
            for c in self.calls()
            if c.split()[0]
            in (
                "restart",
                "start",
                "stop",
                "enable",
                "disable",
                "daemon-reload",
                "mask",
            )
        ]

    def newest_backup(self) -> Path:
        dirs = sorted(self.backup_root.glob("*"))
        assert dirs, "no backup dir was created"
        return dirs[-1]


def make_apply_sandbox(tmp_path: Path) -> ApplySandbox:
    base = make_sandbox(tmp_path)
    systemd_dir = tmp_path / "etc-systemd-system"
    state = tmp_path / "sc-state"
    proc_root = tmp_path / "proc"
    for d in (systemd_dir, state, proc_root):
        d.mkdir(parents=True, exist_ok=True)
    reader = _write_exec(tmp_path / "stubs" / "nodehosts-reader", STUB_NODEHOSTS)
    _write_exec(base.stubs / "systemctl", STUB_SYSTEMCTL)
    _write_exec(base.stubs / "ss", STUB_SS)
    _write_exec(base.stubs / "runuser", STUB_RUNUSER)
    vk = f"{LIVE_HOME}/build/llama.cpp/build-vulkan-new/bin/llama-server"
    for unit, (_launcher, port) in UNITS.items():
        (systemd_dir / f"{unit}.service").write_text(
            _OLD_UNIT.format(
                unit=unit, binary=vk, model="/home/lfdesousa/models/m.gguf", port=port
            )
        )
    dropin = systemd_dir / "llama-server.service.d" / "20-qwen38-mtp-permanent.conf"
    dropin.parent.mkdir()
    dropin.write_text(_OLD_DROPIN.format(binary=vk))
    sb = ApplySandbox(
        root=base.root,
        home=base.home,
        stubs=base.stubs,
        key_file=base.key_file,
        capture=base.capture,
        sleep_log=base.sleep_log,
        systemd_dir=systemd_dir,
        state=state,
        proc_root=proc_root,
        backup_root=tmp_path / "backups",
        nodehosts_reader=reader,
    )
    # "boot": systemd has loaded the pre-LC-5 units and the three active ones run
    for step in ("daemon-reload", *(f"restart {u}" for u in ACTIVE)):
        subprocess.run(
            ["bash", str(base.stubs / "systemctl"), *step.split()],
            env=sb.apply_env(),
            check=True,
            capture_output=True,
        )
    (state / "calls.log").write_text("")  # the boot is not part of the run under test
    return sb
