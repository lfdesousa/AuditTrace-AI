"""``make integration`` — WU-3 of the ADR-059 mechanical-enforcement build
(Layer 3, the local integration gate).

PR #306 shipped a Keycloak ``varchar(255)`` realm-import failure past a GREEN
``make test``: the client ``description`` was 314 chars, Keycloak's
``CLIENT.DESCRIPTION`` column is ``varchar(255)``, realm import failed at
container boot, the ``keycloak`` container never turned healthy, and only
CI's compose job ("Build and start stack" / ``compose-e2e`` in
``.github/workflows/e2e-compose.yml``) caught it — ``make test`` validates
realm JSON *shape*, never boots Keycloak. This module gives builders that
same boot **locally**: it MIRRORS ``e2e-compose.yml`` (bring the full compose
stack up with the ``mock-llm`` profile, wait for every service healthy,
run the shared smoke scripts, tear down — always, on success or failure)
so the class of failure #306 exposed is caught before CI (invariant 3,
"mirror every CI gate locally"), not after.

Design notes:

* The pass/fail decision is ``docker compose up --wait``'s own exit code —
  compose already refuses to report "up" until every service with a
  healthcheck (Keycloak included) is healthy, and times out non-zero
  otherwise. This module does not reimplement that polling; it drives the
  command and diagnoses the result.
* :func:`compose_stack` is a context manager (PYTHON-ENGINEERING §1 —
  prefer ``with`` over hand-rolled ``try/finally`` at every call site): it
  owns ONE ``try/finally`` internally so teardown (``docker compose down -v
  --remove-orphans``) always runs, whether the stack booted, failed to
  boot, or the caller's smoke checks raised. That "teardown always runs" is
  the falsifiable guard this module's tests pin down.
* Every external effect funnels through the single monkeypatchable
  :func:`_run` seam (mirrors ``scripts/deploy/mesh.py`` / ``verify.py`` /
  ``runner.py`` — the same "real subprocess raises where a mock returns"
  discipline), so the orchestration logic is unit-testable without Docker.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import subprocess  # noqa: S404 - fixed argv lists, no shell, local dev/CI tooling
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_ENV_FILE = ".env.ci"
DEFAULT_PROFILE = "mock-llm"
DEFAULT_WAIT_TIMEOUT = 300
DEFAULT_BASE_URL = "https://localhost"
DEFAULT_CURL_OPTS = "-k"
DEFAULT_UP_TIMEOUT_SLACK = 60  # subprocess timeout beyond compose's own --wait-timeout
DEFAULT_SMOKE_TIMEOUT = 60

CONTAINER_PREFIX = "audittrace-"
NO_HEALTHCHECK = "<no-healthcheck>"

# The shared smoke scripts B7 step 5 committed for e2e-compose.yml — the SAME
# scripts operators can re-run standalone. Keeping this list in one place
# means this module and tests/test_compose_drift.py's _EXPECTED_SCRIPTS both
# name the canonical set; a divergence is caught by test_scripts_exist below.
SMOKE_SCRIPTS: tuple[str, ...] = (
    "tests/integration/compose/test-health.sh",
    "tests/integration/compose/test-chat-completion.sh",
    "tests/integration/compose/test-models.sh",
)


# ── external-effect indirection (monkeypatched in tests) ──────────────────────


def _run(
    cmd: Sequence[str],
    *,
    timeout: float | None = None,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Sole subprocess entry point. Every other function in this module calls
    through here (never ``subprocess.run`` directly) so tests can monkeypatch
    one seam."""
    logger.info("exec: %s", " ".join(str(c) for c in cmd))
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        list(cmd),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        cwd=cwd,
        env=env,
    )


# ── errors ──────────────────────────────────────────────────────────────────


class ComposeBootError(RuntimeError):
    """Raised when ``docker compose up --wait`` does not reach healthy.

    Carries the diagnostic ``health`` snapshot and ``logs`` tail GATHERED
    BEFORE teardown. This matters: :func:`compose_stack`'s ``finally`` tears
    the stack down (removing every container) before this exception ever
    reaches a caller's ``except`` block, so diagnostics collected *after*
    catching it would just be querying containers that no longer exist —
    a real bug this module's own dogfood run against the reintroduced #306
    defect caught (an earlier version queried post-teardown and printed
    "(no services reported)" with empty logs). Collecting the evidence while
    the stack is still up — inside :func:`compose_stack`, before its
    ``finally`` fires — and attaching it here is the fix.
    """

    def __init__(
        self,
        result: subprocess.CompletedProcess[str],
        *,
        health: dict[str, str] | None = None,
        logs: str = "",
    ) -> None:
        super().__init__(
            f"docker compose up --wait failed or timed out (exit={result.returncode})"
        )
        self.result = result
        self.health = health or {}
        self.logs = logs


class SmokeTestError(RuntimeError):
    """Raised when one of the shared smoke scripts exits non-zero."""

    def __init__(self, script: str, result: subprocess.CompletedProcess[str]) -> None:
        super().__init__(f"smoke script failed: {script} (exit={result.returncode})")
        self.script = script
        self.result = result


# ── pure command builders (no I/O — trivially unit-testable) ─────────────────


def compose_base_cmd(env_file: str) -> list[str]:
    return ["docker", "compose", "--env-file", env_file]


def up_cmd(env_file: str, *, profile: str, wait_timeout: int) -> list[str]:
    return [
        *compose_base_cmd(env_file),
        "--profile",
        profile,
        "up",
        "-d",
        "--build",
        "--wait",
        "--wait-timeout",
        str(wait_timeout),
    ]


def down_cmd(env_file: str) -> list[str]:
    return [*compose_base_cmd(env_file), "down", "-v", "--remove-orphans"]


def ps_services_cmd(env_file: str) -> list[str]:
    return [*compose_base_cmd(env_file), "ps", "--services"]


def logs_cmd(env_file: str, *, tail: int = 300) -> list[str]:
    return [*compose_base_cmd(env_file), "logs", f"--tail={tail}"]


def container_name(service: str) -> str:
    """Compose names containers ``<prefix><service>`` in this repo's
    ``docker-compose.yml`` (``container_name: audittrace-<service>`` on every
    service) — mirrors the health-summary step in e2e-compose.yml."""
    return f"{CONTAINER_PREFIX}{service}"


def inspect_health_cmd(service: str) -> list[str]:
    return [
        "docker",
        "inspect",
        "--format",
        "{{.State.Health.Status}}",
        container_name(service),
    ]


# ── pure parsing / reporting (the "health-wait parsing" the spec names) ──────


def parse_service_list(ps_output: str) -> list[str]:
    """Parse ``docker compose ps --services`` stdout into a service-name list.
    One service per line; blank lines and surrounding whitespace are dropped."""
    return [line.strip() for line in ps_output.splitlines() if line.strip()]


def unhealthy_services(statuses: dict[str, str]) -> list[str]:
    """Return the services that are NOT healthy, sorted for determinism.

    ``statuses`` maps service name -> the raw text
    ``docker inspect --format '{{.State.Health.Status}}'`` printed (already
    stripped), or :data:`NO_HEALTHCHECK` when the container declares no
    ``HEALTHCHECK`` (some images, e.g. ``minio``, ship without one — that is
    a documented exception, not an omission, so it is excluded from the
    "must report healthy" contract).
    """
    return sorted(
        name
        for name, status in statuses.items()
        if status not in ("healthy", NO_HEALTHCHECK)
    )


def render_health_report(statuses: dict[str, str]) -> str:
    """Human-readable per-service health summary, one line per service,
    sorted by name for a stable diff-able report."""
    lines = [f"  {name}: {statuses[name]}" for name in sorted(statuses)]
    return "\n".join(lines) if lines else "  (no services reported)"


# ── orchestration ─────────────────────────────────────────────────────────────


def ensure_dev_tls_cert(*, certs_dir: Path | None = None, run: Any = _run) -> bool:
    """Ensure ``certs/sovereign.pem`` + ``certs/sovereign-key.pem`` exist so
    Traefik can serve TLS. Reuses an operator-generated cert (e.g. ``mkcert``
    via ``certs/generate-certs.sh``, trusted in the system store) when
    present; otherwise generates the SAME throwaway 1-day self-signed cert
    ``e2e-compose.yml`` does, so a fresh clone can run ``make integration``
    without mkcert installed. Returns ``True`` iff a new cert was generated.
    """
    certs_dir = certs_dir or (REPO_ROOT / "certs")
    cert = certs_dir / "sovereign.pem"
    key = certs_dir / "sovereign-key.pem"
    if cert.exists() and key.exists():
        logger.info("reusing existing TLS cert at %s", cert)
        return False
    certs_dir.mkdir(parents=True, exist_ok=True)
    logger.info("generating ephemeral self-signed TLS cert at %s", cert)
    run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=localhost",
        ],
        timeout=30,
    )
    return True


def collect_health_snapshot(env_file: str, *, run: Any = _run) -> dict[str, str]:
    """Diagnostic snapshot: service name -> health status text. Best-effort —
    an inspect failure (container not created, no healthcheck) reports
    :data:`NO_HEALTHCHECK` rather than raising, since this is called for
    DIAGNOSIS after a failure, when the stack may be partially up."""
    ps_result = run(ps_services_cmd(env_file), timeout=30)
    services = parse_service_list(ps_result.stdout)
    statuses: dict[str, str] = {}
    for service in services:
        result = run(inspect_health_cmd(service), timeout=15)
        status = result.stdout.strip()
        statuses[service] = (
            status if (result.returncode == 0 and status) else NO_HEALTHCHECK
        )
    return statuses


def run_smoke_scripts(
    *,
    base_url: str = DEFAULT_BASE_URL,
    curl_opts: str = DEFAULT_CURL_OPTS,
    scripts: Sequence[str] = SMOKE_SCRIPTS,
    run: Any = _run,
) -> None:
    """Run the shared compose smoke scripts (B7 step 5) in order. Raises
    :class:`SmokeTestError` on the first non-zero exit — the SAME scripts
    ``e2e-compose.yml`` runs, so a local pass mirrors a CI pass."""
    env = {**os.environ, "AUDITTRACE_BASE_URL": base_url, "CURL_OPTS": curl_opts}
    for script in scripts:
        result = run(
            ["bash", str(REPO_ROOT / script)],
            timeout=DEFAULT_SMOKE_TIMEOUT,
            env=env,
        )
        if result.returncode != 0:
            raise SmokeTestError(script, result)


@contextlib.contextmanager
def compose_stack(
    *,
    env_file: str = DEFAULT_ENV_FILE,
    profile: str = DEFAULT_PROFILE,
    wait_timeout: int = DEFAULT_WAIT_TIMEOUT,
    run: Any = _run,
) -> Iterator[subprocess.CompletedProcess[str]]:
    """Bring the compose stack up; ALWAYS tear it down on exit, whether the
    boot succeeded, failed, or the caller's block raised.

    This is the falsifiable guard the spec's "teardown-on-failure" deliverable
    names: the ``finally`` below runs ``docker compose down -v
    --remove-orphans`` unconditionally. Remove it and a failed boot — or a
    failed smoke check inside the ``with`` block — would leave the stack
    (and its volumes) resident, which the tests in
    ``tests/test_integration_gate.py`` pin down directly.

    Raises :class:`ComposeBootError` if ``docker compose up --wait`` exits
    non-zero (a bad realm import, a service that never turns healthy, a
    build failure, ...) — the caller's ``with`` block never runs against a
    stack that isn't actually up. The error's diagnostic ``health``/``logs``
    are gathered HERE, before teardown, while the failed containers still
    exist to inspect (see :class:`ComposeBootError`'s docstring).
    """
    up_result = run(
        up_cmd(env_file, profile=profile, wait_timeout=wait_timeout),
        timeout=wait_timeout + DEFAULT_UP_TIMEOUT_SLACK,
    )
    try:
        if up_result.returncode != 0:
            health = collect_health_snapshot(env_file, run=run)
            logs = run(logs_cmd(env_file), timeout=30)
            raise ComposeBootError(up_result, health=health, logs=logs.stdout)
        yield up_result
    finally:
        run(down_cmd(env_file), timeout=60)


def _print_boot_failure_diagnostics(exc: ComposeBootError) -> None:
    """PURE presentation of an already-gathered :class:`ComposeBootError` —
    no subprocess calls here. The evidence (``exc.health`` / ``exc.logs``)
    was collected by :func:`compose_stack` BEFORE teardown; calling out to
    Docker again at this point would just query containers teardown has
    already removed (see :class:`ComposeBootError`'s docstring)."""
    print(  # noqa: T201 - CLI tool output, not library logging
        f"[integration] ERROR: {exc}",
        file=sys.stderr,
    )
    if exc.result.stdout:
        print(f"[integration] compose up stdout:\n{exc.result.stdout}", file=sys.stderr)
    if exc.result.stderr:
        print(f"[integration] compose up stderr:\n{exc.result.stderr}", file=sys.stderr)
    print("[integration] --- health summary ---", file=sys.stderr)
    print(render_health_report(exc.health), file=sys.stderr)
    offenders = unhealthy_services(exc.health)
    if offenders:
        print(
            f"[integration] unhealthy service(s): {offenders} — this is the "
            "same signal PR #306 hit (a bad Keycloak realm import). Check "
            "the logs below for the root cause.",
            file=sys.stderr,
        )
    if exc.logs:
        print(
            "[integration] --- recent logs (tail 300, all services) ---",
            file=sys.stderr,
        )
        print(exc.logs, file=sys.stderr)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "make integration — boot the full docker-compose stack (mock-llm "
            "profile), wait for every service healthy (Keycloak realm-import "
            "included), run the shared smoke suite, tear down. Mirrors "
            ".github/workflows/e2e-compose.yml locally."
        )
    )
    parser.add_argument(
        "--env-file",
        default=os.environ.get("INTEGRATION_ENV_FILE", DEFAULT_ENV_FILE),
        help=f"compose --env-file (default: {DEFAULT_ENV_FILE}, override via INTEGRATION_ENV_FILE)",
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get("INTEGRATION_PROFILE", DEFAULT_PROFILE),
        help=f"compose profile to activate (default: {DEFAULT_PROFILE})",
    )
    parser.add_argument(
        "--wait-timeout",
        type=int,
        default=int(os.environ.get("INTEGRATION_WAIT_TIMEOUT", DEFAULT_WAIT_TIMEOUT)),
        help=f"compose up --wait-timeout in seconds (default: {DEFAULT_WAIT_TIMEOUT})",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AUDITTRACE_BASE_URL", DEFAULT_BASE_URL),
        help=f"base URL for the smoke scripts (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--curl-opts",
        default=os.environ.get("CURL_OPTS", DEFAULT_CURL_OPTS),
        help=f"extra curl flags for the smoke scripts (default: {DEFAULT_CURL_OPTS})",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[integration] %(message)s")
    args = parse_args(argv)

    print("[integration] === audittrace local integration gate (make integration) ===")
    print(
        f"[integration] env-file={args.env_file} profile={args.profile} "
        f"wait-timeout={args.wait_timeout}s"
    )

    ensure_dev_tls_cert(run=_run)

    try:
        # `run=_run` is passed EXPLICITLY here rather than relying on
        # compose_stack's / run_smoke_scripts' own `run=_run` default
        # parameter: default-argument values are bound once, at function
        # *definition* time, not looked up dynamically — so a test that
        # monkeypatches the module-level `gate._run` name would silently
        # NOT reach a callee's default parameter, and `main()` would shell
        # out to a real subprocess despite the patch. Forwarding the
        # current module-level `_run` explicitly (a body-level reference,
        # resolved at CALL time) keeps the seam single and honest.
        with compose_stack(
            env_file=args.env_file,
            profile=args.profile,
            wait_timeout=args.wait_timeout,
            run=_run,
        ):
            print("[integration] compose stack healthy — running smoke checks ...")
            run_smoke_scripts(
                base_url=args.base_url, curl_opts=args.curl_opts, run=_run
            )
    except ComposeBootError as exc:
        _print_boot_failure_diagnostics(exc)
        return 1
    except SmokeTestError as exc:
        print(f"[integration] ERROR: {exc}", file=sys.stderr)
        if exc.result.stdout:
            print(exc.result.stdout, file=sys.stderr)
        if exc.result.stderr:
            print(exc.result.stderr, file=sys.stderr)
        return 1

    print("[integration] === all checks passed ===")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI entry point
    sys.exit(main())
