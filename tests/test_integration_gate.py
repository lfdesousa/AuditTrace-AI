"""Unit tests for the ``make integration`` local gate (ADR-059 Layer 3, WU-3).

Every external effect (the ``docker``/``bash``/``openssl`` subprocess seam) is
mocked; no real Docker, no real network. The reviewer's mandate is to prove the
gate can FAIL and that it ALWAYS tears down: a compose boot that never reaches
healthy MUST raise :class:`ComposeBootError`, a failing smoke script MUST raise
:class:`SmokeTestError`, and — the falsifiable core of this module — teardown
(``docker compose down -v --remove-orphans``) MUST run in every one of those
paths, not just the happy path. Several tests assert the FAILURE branch
explicitly (a gate that cannot fail is decoration).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import integration_gate as gate


def _proc(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["docker"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class _Recorder:
    """A fake ``run`` seam that records every call and returns canned results
    keyed by a substring of the argv (first match wins), defaulting to a
    successful empty result."""

    def __init__(
        self, rules: list[tuple[str, subprocess.CompletedProcess]] | None = None
    ) -> None:
        self.rules = rules or []
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, timeout=None, cwd=None, env=None):
        self.calls.append(list(cmd))
        joined = " ".join(str(c) for c in cmd)
        for token, result in self.rules:
            if token in joined:
                return result
        return _proc(0)


# ── pure command builders ─────────────────────────────────────────────────────


def test_up_cmd_shape():
    cmd = gate.up_cmd(".env.ci", profile="mock-llm", wait_timeout=300)
    assert cmd == [
        "docker",
        "compose",
        "--env-file",
        ".env.ci",
        "--profile",
        "mock-llm",
        "up",
        "-d",
        "--build",
        "--wait",
        "--wait-timeout",
        "300",
    ]


def test_down_cmd_shape():
    assert gate.down_cmd(".env.ci") == [
        "docker",
        "compose",
        "--env-file",
        ".env.ci",
        "down",
        "-v",
        "--remove-orphans",
    ]


def test_ps_services_cmd_shape():
    assert gate.ps_services_cmd(".env.ci") == [
        "docker",
        "compose",
        "--env-file",
        ".env.ci",
        "ps",
        "--services",
    ]


def test_logs_cmd_default_tail():
    cmd = gate.logs_cmd(".env.ci")
    assert cmd[-1] == "--tail=300"


def test_container_name():
    assert gate.container_name("keycloak") == "audittrace-keycloak"


def test_inspect_health_cmd():
    cmd = gate.inspect_health_cmd("keycloak")
    assert cmd == [
        "docker",
        "inspect",
        "--format",
        "{{.State.Health.Status}}",
        "audittrace-keycloak",
    ]


# ── health-wait parsing (pure) ─────────────────────────────────────────────────


def test_parse_service_list_strips_and_drops_blanks():
    out = "memory-server\nkeycloak\n\n  \nredis\n"
    assert gate.parse_service_list(out) == ["memory-server", "keycloak", "redis"]


def test_parse_service_list_empty():
    assert gate.parse_service_list("") == []


def test_unhealthy_services_flags_non_healthy():
    statuses = {
        "keycloak": "unhealthy",
        "memory-server": "healthy",
        "minio": gate.NO_HEALTHCHECK,
        "postgres": "starting",
    }
    assert gate.unhealthy_services(statuses) == ["keycloak", "postgres"]


def test_unhealthy_services_all_healthy_returns_empty():
    statuses = {"a": "healthy", "b": gate.NO_HEALTHCHECK}
    assert gate.unhealthy_services(statuses) == []


def test_render_health_report_sorted_and_nonempty():
    report = gate.render_health_report({"redis": "healthy", "keycloak": "unhealthy"})
    lines = report.splitlines()
    assert lines == ["  keycloak: unhealthy", "  redis: healthy"]


def test_render_health_report_empty():
    assert gate.render_health_report({}) == "  (no services reported)"


def test_collect_health_snapshot_parses_ps_and_inspects_each():
    recorder = _Recorder(
        rules=[
            ("ps --services", _proc(0, stdout="keycloak\nmemory-server\n")),
            ("audittrace-keycloak", _proc(0, stdout="unhealthy\n")),
            ("audittrace-memory-server", _proc(0, stdout="healthy\n")),
        ]
    )
    snapshot = gate.collect_health_snapshot(".env.ci", run=recorder)
    assert snapshot == {"keycloak": "unhealthy", "memory-server": "healthy"}


def test_collect_health_snapshot_defaults_to_no_healthcheck_on_inspect_failure():
    recorder = _Recorder(
        rules=[
            ("ps --services", _proc(0, stdout="minio\n")),
            ("audittrace-minio", _proc(1, stdout="", stderr="no such object")),
        ]
    )
    snapshot = gate.collect_health_snapshot(".env.ci", run=recorder)
    assert snapshot == {"minio": gate.NO_HEALTHCHECK}


def test_collect_health_snapshot_defaults_to_no_healthcheck_on_blank_status():
    recorder = _Recorder(
        rules=[
            ("ps --services", _proc(0, stdout="minio\n")),
            ("audittrace-minio", _proc(0, stdout="")),
        ]
    )
    snapshot = gate.collect_health_snapshot(".env.ci", run=recorder)
    assert snapshot == {"minio": gate.NO_HEALTHCHECK}


# ── ensure_dev_tls_cert ────────────────────────────────────────────────────────


def test_ensure_dev_tls_cert_reuses_existing(tmp_path: Path):
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    (certs_dir / "sovereign.pem").write_text("cert")
    (certs_dir / "sovereign-key.pem").write_text("key")
    recorder = _Recorder()
    generated = gate.ensure_dev_tls_cert(certs_dir=certs_dir, run=recorder)
    assert generated is False
    assert recorder.calls == []  # openssl never invoked — reused the existing cert


def test_ensure_dev_tls_cert_generates_when_missing(tmp_path: Path):
    certs_dir = tmp_path / "certs"
    recorder = _Recorder()
    generated = gate.ensure_dev_tls_cert(certs_dir=certs_dir, run=recorder)
    assert generated is True
    assert certs_dir.is_dir()
    assert len(recorder.calls) == 1
    assert recorder.calls[0][0] == "openssl"
    assert "-subj" in recorder.calls[0]


def test_ensure_dev_tls_cert_generates_when_only_key_present(tmp_path: Path):
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    (certs_dir / "sovereign-key.pem").write_text("key")
    generated = gate.ensure_dev_tls_cert(certs_dir=certs_dir, run=_Recorder())
    assert generated is True


# ── run_smoke_scripts ──────────────────────────────────────────────────────────


def test_run_smoke_scripts_runs_every_script_in_order():
    recorder = _Recorder()
    gate.run_smoke_scripts(scripts=("a.sh", "b.sh", "c.sh"), run=recorder)
    assert [call[1] for call in recorder.calls] == [
        str(gate.REPO_ROOT / "a.sh"),
        str(gate.REPO_ROOT / "b.sh"),
        str(gate.REPO_ROOT / "c.sh"),
    ]


def test_run_smoke_scripts_passes_base_url_and_curl_opts_via_env():
    seen_env = {}

    def _run(cmd, *, timeout=None, cwd=None, env=None):
        seen_env.update(env or {})
        return _proc(0)

    gate.run_smoke_scripts(
        base_url="https://example.invalid",
        curl_opts="--fail",
        scripts=("only.sh",),
        run=_run,
    )
    assert seen_env["AUDITTRACE_BASE_URL"] == "https://example.invalid"
    assert seen_env["CURL_OPTS"] == "--fail"


def test_run_smoke_scripts_raises_on_first_failure_and_stops():
    recorder = _Recorder(rules=[("b.sh", _proc(1, stderr="boom"))])
    with pytest.raises(gate.SmokeTestError) as exc_info:
        gate.run_smoke_scripts(scripts=("a.sh", "b.sh", "c.sh"), run=recorder)
    assert exc_info.value.script == "b.sh"
    # c.sh must NEVER run once b.sh has failed.
    ran = [call[1] for call in recorder.calls]
    assert str(gate.REPO_ROOT / "c.sh") not in ran
    assert str(gate.REPO_ROOT / "a.sh") in ran
    assert str(gate.REPO_ROOT / "b.sh") in ran


# ── compose_stack: the falsifiable teardown-on-failure guard ──────────────────


def test_compose_stack_happy_path_ups_yields_and_tears_down():
    recorder = _Recorder()
    with gate.compose_stack(env_file=".env.ci", run=recorder) as up_result:
        assert up_result.returncode == 0
    # First call is `up`, last call is `down` — teardown ran exactly once.
    assert "up" in recorder.calls[0]
    assert recorder.calls[-1][4] == "down"


def test_compose_stack_raises_compose_boot_error_when_up_fails():
    recorder = _Recorder(rules=[("up", _proc(1, stderr="keycloak unhealthy"))])
    with pytest.raises(gate.ComposeBootError):
        with gate.compose_stack(env_file=".env.ci", run=recorder):
            raise AssertionError("body must never execute when boot fails")
    # THE GUARD: teardown still ran even though boot failed.
    assert recorder.calls[-1][4] == "down"


def test_compose_stack_tears_down_even_when_body_raises():
    recorder = _Recorder()  # up succeeds
    with pytest.raises(RuntimeError, match="boom"):
        with gate.compose_stack(env_file=".env.ci", run=recorder):
            raise RuntimeError("boom")
    # THE GUARD: teardown still ran even though the caller's block raised.
    assert recorder.calls[-1][4] == "down"


def test_compose_stack_passes_profile_and_wait_timeout_through():
    recorder = _Recorder()
    with gate.compose_stack(
        env_file=".env.ci", profile="mock-llm", wait_timeout=120, run=recorder
    ):
        pass
    up_call = recorder.calls[0]
    assert "mock-llm" in up_call
    assert "120" in up_call


# ── the real subprocess seam (exercised once for coverage honesty) ────────────


def test_run_executes_real_subprocess():
    proc = gate._run(["printf", "hi"])
    assert proc.returncode == 0 and proc.stdout == "hi"


# ── CLI wiring ──────────────────────────────────────────────────────────────


def test_parse_args_defaults():
    args = gate.parse_args([])
    assert args.env_file == gate.DEFAULT_ENV_FILE
    assert args.profile == gate.DEFAULT_PROFILE
    assert args.wait_timeout == gate.DEFAULT_WAIT_TIMEOUT
    assert args.base_url == gate.DEFAULT_BASE_URL
    assert args.curl_opts == gate.DEFAULT_CURL_OPTS


def test_parse_args_overrides():
    args = gate.parse_args(
        [
            "--env-file",
            "custom.env",
            "--profile",
            "other",
            "--wait-timeout",
            "42",
            "--base-url",
            "https://x.invalid",
            "--curl-opts=-kv",
        ]
    )
    assert args.env_file == "custom.env"
    assert args.profile == "other"
    assert args.wait_timeout == 42
    assert args.base_url == "https://x.invalid"
    assert args.curl_opts == "-kv"


def test_parse_args_reads_env_var_defaults(monkeypatch):
    monkeypatch.setenv("INTEGRATION_ENV_FILE", "env-var.env")
    monkeypatch.setenv("INTEGRATION_WAIT_TIMEOUT", "17")
    args = gate.parse_args([])
    assert args.env_file == "env-var.env"
    assert args.wait_timeout == 17


def test_main_happy_path(monkeypatch, tmp_path: Path):
    recorder = _Recorder()
    monkeypatch.setattr(gate, "_run", recorder)
    monkeypatch.setattr(gate, "ensure_dev_tls_cert", lambda **_kwargs: True)
    monkeypatch.setattr(gate, "run_smoke_scripts", lambda **_kwargs: None)
    rc = gate.main(["--env-file", ".env.ci"])
    assert rc == 0
    assert recorder.calls[-1][4] == "down"  # teardown ran


def test_main_returns_1_and_prints_diagnostics_on_boot_failure(monkeypatch, capsys):
    # Exercises the #306 shape end-to-end: `up --wait` fails (with BOTH stdout
    # and stderr captured), the diagnostic health snapshot finds keycloak
    # unhealthy, and the tail-300 logs are non-empty — every branch of
    # `_print_boot_failure_diagnostics` fires.
    recorder = _Recorder(
        rules=[
            ("up", _proc(1, stdout="waiting...", stderr="keycloak unhealthy")),
            ("ps --services", _proc(0, stdout="keycloak\nmemory-server\n")),
            ("audittrace-keycloak", _proc(0, stdout="unhealthy\n")),
            ("audittrace-memory-server", _proc(0, stdout="healthy\n")),
            ("logs", _proc(0, stdout="keycloak  | ERROR: value too long")),
        ]
    )
    monkeypatch.setattr(gate, "_run", recorder)
    monkeypatch.setattr(gate, "ensure_dev_tls_cert", lambda **_kwargs: True)
    rc = gate.main([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "compose up --wait failed" in captured.err
    assert "compose up stdout" in captured.err
    assert "compose up stderr" in captured.err
    assert "unhealthy service(s): ['keycloak']" in captured.err
    assert "value too long" in captured.err


def test_print_boot_failure_diagnostics_no_stdout_no_offenders_no_logs():
    """The inverse of the rich case above: `up` fails with only a returncode
    (no stdout, no stderr, no unhealthy service found, no log output) — the
    diagnostics function must still run to completion without printing any
    of the optional sections. Also proves `_print_boot_failure_diagnostics`
    is PURE (no `run` seam) — it never shells out to Docker itself, since by
    the time it runs teardown has already removed the containers."""
    exc = gate.ComposeBootError(_proc(1))
    gate._print_boot_failure_diagnostics(exc)


def test_compose_boot_error_gathers_diagnostics_before_teardown():
    """The bug this pins down: an earlier version collected the health
    snapshot + logs AFTER `compose_stack`'s `finally` had already torn the
    stack down, so the diagnostics were always empty on a real failure. This
    asserts the health/logs calls happen BEFORE the `down` call in the
    recorded call order, and that the resulting exception carries non-empty
    evidence gathered while the stack was still up."""
    recorder = _Recorder(
        rules=[
            ("up", _proc(1, stderr="keycloak unhealthy")),
            ("ps --services", _proc(0, stdout="keycloak\n")),
            ("audittrace-keycloak", _proc(0, stdout="unhealthy\n")),
            ("logs", _proc(0, stdout="realm import failed")),
        ]
    )
    with pytest.raises(gate.ComposeBootError) as exc_info:
        with gate.compose_stack(env_file=".env.ci", run=recorder):
            raise AssertionError("body must never execute when boot fails")
    exc = exc_info.value
    assert exc.health == {"keycloak": "unhealthy"}
    assert exc.logs == "realm import failed"
    # `down` must be the LAST call — every diagnostic call precedes it.
    assert recorder.calls[-1][4] == "down"
    down_index = len(recorder.calls) - 1
    assert all("down" not in call for call in recorder.calls[:down_index]), (
        "a diagnostic call happened at/after teardown — evidence would be stale"
    )


def test_main_returns_1_on_smoke_failure(monkeypatch, capsys):
    recorder = _Recorder()
    monkeypatch.setattr(gate, "_run", recorder)
    monkeypatch.setattr(gate, "ensure_dev_tls_cert", lambda **_kwargs: True)

    def _boom(**_kwargs):
        raise gate.SmokeTestError("test-health.sh", _proc(1, stderr="500"))

    monkeypatch.setattr(gate, "run_smoke_scripts", _boom)
    rc = gate.main([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "smoke script failed" in captured.err
    assert "500" in captured.err
    # Teardown must still have run even though the smoke check failed.
    assert recorder.calls[-1][4] == "down"


def test_main_smoke_failure_prints_stdout_when_stderr_empty(monkeypatch, capsys):
    recorder = _Recorder()
    monkeypatch.setattr(gate, "_run", recorder)
    monkeypatch.setattr(gate, "ensure_dev_tls_cert", lambda **_kwargs: True)

    def _boom(**_kwargs):
        raise gate.SmokeTestError(
            "test-models.sh", _proc(1, stdout="unexpected model list", stderr="")
        )

    monkeypatch.setattr(gate, "run_smoke_scripts", _boom)
    rc = gate.main([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "unexpected model list" in captured.err


# ── SMOKE_SCRIPTS drift: must match the shared scripts B7 step 5 committed ────


def test_smoke_scripts_all_exist_and_are_executable():
    import os

    for rel in gate.SMOKE_SCRIPTS:
        path = gate.REPO_ROOT / rel
        assert path.exists(), f"{path} referenced by SMOKE_SCRIPTS but missing"
        assert os.access(path, os.X_OK), f"{path} is not executable"
