"""B7 step 2 — compose drift guards (`feedback_no_more_drifts`).

Each test enforces a cross-template invariant the compose stack
shares with the chart/kind paths. Run via ``make test`` (no
cluster needed; pure file reads).

Drift classes covered (step 2):

* **Mock-LLM script parity** — the Python source served by the
  in-cluster mock-LLM (``tests/integration/fixtures/mock-llm-configmap.yaml``)
  and the compose mock-LLM (``tests/integration/fixtures/compose/mock-llm/server.py``)
  MUST be byte-identical. Q6 of the B7 plan (`docs/architecture/
  b7-docker-compose-revive-plan.md` §10.6) explicitly chose
  "duplicate the script + add a drift-guard test" over symlinking
  (cross-platform concern + clearer intent). This test is the
  load-bearing half of that decision.

Anchors:
- `feedback_no_more_drifts`
- `docs/architecture/b7-docker-compose-revive-plan.md` §7 step 11
  (will expand this file with more drift classes as B7 lands).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
KIND_CONFIGMAP_PATH = (
    REPO_ROOT / "tests" / "integration" / "fixtures" / "mock-llm-configmap.yaml"
)
COMPOSE_SCRIPT_PATH = (
    REPO_ROOT
    / "tests"
    / "integration"
    / "fixtures"
    / "compose"
    / "mock-llm"
    / "server.py"
)


def _extract_kind_script() -> str:
    """Pull the embedded Python source out of the kind ConfigMap.

    The ConfigMap stores the script as a multi-line YAML scalar
    under ``data["server.py"]``. PyYAML preserves the string
    verbatim (modulo the YAML indentation strip that the parser
    handles automatically), so this returns the script as it
    appears at runtime inside the kind pod.
    """
    with KIND_CONFIGMAP_PATH.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if doc.get("kind") != "ConfigMap":
        raise AssertionError(
            f"Expected ConfigMap at {KIND_CONFIGMAP_PATH}, got kind={doc.get('kind')!r}"
        )
    data = doc.get("data", {})
    script = data.get("server.py")
    if not isinstance(script, str):
        raise AssertionError(
            f"ConfigMap data.server.py not a string at {KIND_CONFIGMAP_PATH} — "
            f"got {type(script).__name__}"
        )
    return script


def _extract_compose_script() -> str:
    """Read the standalone .py the compose service mounts."""
    return COMPOSE_SCRIPT_PATH.read_text(encoding="utf-8")


class TestMockLlmScriptParity:
    """The compose mock-LLM script and the kind ConfigMap mock-LLM
    script MUST be byte-identical. Drift would mean one path emits
    a different ``/v1/chat/completions`` response than the other —
    silently breaking the symmetric-test posture the B7 step-4
    GHA workflow will rely on (compose vs kind running the SAME
    contract).

    Q6 decision (B7 plan §10.6) — duplicate the file rather than
    symlink because:
      - Linux symlinks across the project work but Windows dev
        environments break on them
      - The kind ConfigMap is a YAML wrapper; the compose mount
        is a bare .py file. Symlinking a .py into YAML-data is
        not portable.
      - Drift cost is one test; symlink-fragility cost is
        operator hours debugging "why does kind work but
        compose doesn't?" silently.
    """

    def test_compose_script_exists(self) -> None:
        assert COMPOSE_SCRIPT_PATH.exists(), (
            f"Compose mock-LLM script missing at {COMPOSE_SCRIPT_PATH}. "
            "B7 step 2 expects this file alongside the kind ConfigMap. "
            "Restore from git history or re-port from the kind fixture."
        )

    def test_kind_configmap_exists(self) -> None:
        assert KIND_CONFIGMAP_PATH.exists(), (
            f"Kind mock-LLM ConfigMap missing at {KIND_CONFIGMAP_PATH}. "
            "PR-B11's mock-LLM fixture is missing from the tree."
        )

    def test_compose_and_kind_mock_llm_scripts_are_byte_identical(self) -> None:
        kind_script = _extract_kind_script()
        compose_script = _extract_compose_script()

        if kind_script == compose_script:
            return  # passes

        # Produce a useful diff in the assertion message so the
        # operator can immediately see what diverged.
        import difflib

        diff = "\n".join(
            difflib.unified_diff(
                kind_script.splitlines(),
                compose_script.splitlines(),
                fromfile=str(KIND_CONFIGMAP_PATH) + "::data.server.py",
                tofile=str(COMPOSE_SCRIPT_PATH),
                lineterm="",
            )
        )
        raise AssertionError(
            "Drift: kind mock-LLM ConfigMap and compose mock-LLM script "
            "have diverged. Either re-sync one to match the other (the "
            "kind file is conventionally the source of truth since the "
            "mock was born there in PR-B11) or update both intentionally. "
            "Diff:\n" + diff
        )


class TestComposeMockLlmServiceWiring:
    """The compose ``mock-llm`` service must be present (in the
    docker-compose.yml services) and gated under the ``mock-llm``
    profile so the default-empty-profile invocation doesn't bring
    it up. memory-server's LLM URL env defaults must point at the
    in-network ``mock-llm`` service (not the host's loopback)
    so the default-profile + mock-llm-profile combination works
    out of the box for CI.
    """

    COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

    def _compose_doc(self) -> dict:
        with self.COMPOSE_FILE.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    def test_mock_llm_service_present(self) -> None:
        doc = self._compose_doc()
        services = doc.get("services", {})
        assert "mock-llm" in services, (
            "docker-compose.yml is missing the `mock-llm` service required "
            "by B7 step 2. Operators activating COMPOSE_PROFILES=mock-llm "
            "would get a runtime DNS miss + memory-server lifespan failure. "
            "Restore the service definition + the volume mount of "
            "tests/integration/fixtures/compose/mock-llm/server.py."
        )

    def test_mock_llm_service_gated_by_profile(self) -> None:
        doc = self._compose_doc()
        svc = doc["services"]["mock-llm"]
        profiles = svc.get("profiles")
        assert profiles, (
            "mock-llm service is not gated by any compose profile — it "
            "would run on every plain `docker compose up` and conflict "
            "with operators using a host-running llama-server. Add "
            'profiles: ["mock-llm", "ci"] to the service.'
        )
        assert "mock-llm" in profiles, (
            f"mock-llm service profile list ({profiles!r}) does not "
            "include 'mock-llm' — operators activating that profile "
            "wouldn't bring up the service. Fix the profile list."
        )

    def test_memory_server_llm_url_defaults_to_mock(self) -> None:
        doc = self._compose_doc()
        env = doc["services"]["memory-server"].get("environment", [])
        # `environment:` is a list of `KEY=val` strings in our compose.
        defaults = {}
        for entry in env:
            if isinstance(entry, str) and "=" in entry:
                k, v = entry.split("=", 1)
                defaults[k.strip()] = v
        llama = defaults.get("AUDITTRACE_LLAMA_URL", "")
        assert "mock-llm" in llama, (
            f"AUDITTRACE_LLAMA_URL default ({llama!r}) does not resolve "
            "to the in-compose mock-llm service. After B7 step 2 the "
            "default must point at the mock so CI activates with just "
            "COMPOSE_PROFILES=mock-llm. Real-LLM operators override via "
            "their `.env`. Expected substring: 'mock-llm'."
        )
        # The env value should use ${VAR:-default} substitution so
        # operators can override without editing the compose file.
        assert llama.startswith("${"), (
            f"AUDITTRACE_LLAMA_URL ({llama!r}) is hardcoded instead of "
            "env-substituted. Use ${VAR:-default} form so a host-LLM "
            "operator can override via their .env without conflicts."
        )


# ─────────────────────────────────────────────────────────────────────
# B7 step 3 — .env file coverage drift guards
# ─────────────────────────────────────────────────────────────────────

ENV_CI_PATH = REPO_ROOT / ".env.ci"
ENV_DEV_REAL_LLM_PATH = REPO_ROOT / ".env.dev-real-llm.example"


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal .env-style parser: KEY=VALUE per line, ignore comments
    + blank lines. Doesn't handle quoting or multi-line values — fine
    for our deterministic-shape .env files."""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Strip inline trailing comments after the value.
        # Use the first '#' that follows a space — `foo=bar # comment`.
        if " #" in line:
            line = line.split(" #", 1)[0].rstrip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _compose_required_env_vars() -> set[str]:
    """Compose `${VAR}` references WITHOUT a `:-default` fallback —
    i.e. the load-bearing vars that compose fails to substitute
    cleanly if unset. Vars with `${VAR:-default}` are tunable knobs
    that operators don't need to declare in their .env files; their
    defaults are explicit and sensible.

    The test asserts both .env files declare the load-bearing set.
    A missing knob doesn't break compose; a missing load-bearing
    var produces an empty substitution and the downstream service
    crashes 30s later with a confusing error."""
    text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    # Match `${VAR}` but NOT `${VAR:-...}`. Negative lookahead on `:-`.
    return set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)(?!:-)\}", text))


class TestEnvFileCoverage:
    """B7 step 3 — `.env.ci` and `.env.dev-real-llm.example` must
    declare every env-var the compose file substitutes. Catches the
    operator gotcha "compose silently substitutes blank for an
    unset var and a service breaks 30s into startup with a cryptic
    error."

    Anchors:
      - docs/architecture/b7-docker-compose-revive-plan.md §5.
      - feedback_no_more_drifts.
    """

    # COMPOSE_PROFILES is special — only meaningful at the docker
    # compose CLI level (NOT a service env var substituted in any
    # service definition). Exempt from the strict-coverage rule.
    _EXEMPT: frozenset[str] = frozenset()

    def test_env_ci_file_exists(self) -> None:
        assert ENV_CI_PATH.exists(), (
            f".env.ci missing at {ENV_CI_PATH}. B7 step 3 commits this "
            "file as the canonical CI parameterisation surface. "
            "Restore from git history."
        )

    def test_env_dev_real_llm_example_exists(self) -> None:
        assert ENV_DEV_REAL_LLM_PATH.exists(), (
            f".env.dev-real-llm.example missing at {ENV_DEV_REAL_LLM_PATH}."
            " B7 step 3 commits this as the operator template."
        )

    def test_env_ci_covers_every_compose_substitution(self) -> None:
        required = _compose_required_env_vars() - self._EXEMPT
        declared = set(_parse_env_file(ENV_CI_PATH).keys())
        missing = required - declared
        assert not missing, (
            ".env.ci does not declare every env var that docker-compose.yml "
            f"substitutes. Missing: {sorted(missing)}. Each compose "
            "${VAR:-default} reference needs a corresponding line in "
            ".env.ci, even if the value is left empty (to suppress the "
            "compose 'variable is not set' warning in CI logs)."
        )

    def test_env_dev_example_covers_every_compose_substitution(self) -> None:
        required = _compose_required_env_vars() - self._EXEMPT
        declared = set(_parse_env_file(ENV_DEV_REAL_LLM_PATH).keys())
        missing = required - declared
        assert not missing, (
            ".env.dev-real-llm.example does not declare every env var "
            "that docker-compose.yml substitutes. Missing: "
            f"{sorted(missing)}. Add a placeholder ###CHANGE### line "
            "for each so operators copy-and-edit the template without "
            "discovering missing vars at runtime."
        )

    def test_env_ci_has_no_change_placeholder_values(self) -> None:
        """The CI file should NEVER contain the dev-template's
        ###CHANGE### sentinel — those values would break the test
        env at runtime. Catches a likely copy-paste mistake when
        someone duplicates the template."""
        kvs = _parse_env_file(ENV_CI_PATH)
        tainted = [k for k, v in kvs.items() if "###CHANGE###" in v]
        assert not tainted, (
            f".env.ci contains ###CHANGE### placeholder values for: "
            f"{tainted}. Replace with deterministic test values. "
            "###CHANGE### is the dev-template's TODO marker, NOT a "
            "valid CI value — anything substituted with this string "
            "would break compose at runtime."
        )

    def test_env_ci_activates_mock_llm_profile(self) -> None:
        """COMPOSE_PROFILES must include mock-llm in .env.ci so the
        CI workflow doesn't have to pass `--profile mock-llm` on the
        command line. Step 4 will rely on this."""
        kvs = _parse_env_file(ENV_CI_PATH)
        profiles = kvs.get("COMPOSE_PROFILES", "")
        assert "mock-llm" in profiles, (
            f"COMPOSE_PROFILES in .env.ci ({profiles!r}) does not include "
            "'mock-llm'. Without it, `docker compose --env-file .env.ci "
            "up -d` leaves the mock-llm service dormant and memory-server "
            "tries to reach host.docker.internal:11435 — which doesn't "
            "exist in CI runners."
        )


# ─────────────────────────────────────────────────────────────────────
# B7 step 5 — shared test-script drift guards
# ─────────────────────────────────────────────────────────────────────

COMPOSE_SCRIPTS_DIR = REPO_ROOT / "tests" / "integration" / "compose"
E2E_COMPOSE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "e2e-compose.yml"

# The set of shared scripts step 5 commits. Adding a new test script
# means adding it here AND wiring it into the workflow — the drift
# guards below catch either side dropping out of sync.
_EXPECTED_SCRIPTS: tuple[str, ...] = (
    "test-health.sh",
    "test-chat-completion.sh",
    "test-models.sh",
)


class TestComposeTestScripts:
    """B7 step 5 — every assertion the e2e-compose workflow uses
    must live in a shared script under tests/integration/compose/
    so operators can re-run the same checks locally. Catches:

      - Workflow inlines new bash instead of using the shared script
        (regression of the operator-rerun discipline)
      - Script committed but not executable (chmod silently dropped)
      - Workflow references a script that doesn't exist (typo)

    Anchors:
      - docs/architecture/b7-docker-compose-revive-plan.md §7 step 5
      - feedback_no_more_drifts
    """

    def test_compose_scripts_dir_exists(self) -> None:
        assert COMPOSE_SCRIPTS_DIR.is_dir(), (
            f"tests/integration/compose/ missing at {COMPOSE_SCRIPTS_DIR}. "
            "B7 step 5 commits shared test scripts here."
        )

    @pytest.mark.parametrize("script", _EXPECTED_SCRIPTS)
    def test_expected_script_exists(self, script: str) -> None:
        path = COMPOSE_SCRIPTS_DIR / script
        assert path.exists(), (
            f"Expected compose test script missing: {path}. "
            "Either restore it or remove the reference from "
            "tests/test_compose_drift.py::_EXPECTED_SCRIPTS."
        )

    @pytest.mark.parametrize("script", _EXPECTED_SCRIPTS)
    def test_expected_script_is_executable(self, script: str) -> None:
        import os

        path = COMPOSE_SCRIPTS_DIR / script
        assert os.access(path, os.X_OK), (
            f"Compose test script not executable: {path}. "
            "Run `chmod +x` on the file; git stores the executable "
            "bit so the commit + re-clone propagates correctly."
        )

    @pytest.mark.parametrize("script", _EXPECTED_SCRIPTS)
    def test_workflow_references_script(self, script: str) -> None:
        """e2e-compose.yml must invoke each shared script. Catches
        the regression where a developer copies a script's contents
        back into the workflow inline (defeating the share)."""
        if not E2E_COMPOSE_WORKFLOW.exists():
            return  # pre-step-4 state — exempt
        text = E2E_COMPOSE_WORKFLOW.read_text(encoding="utf-8")
        ref = f"tests/integration/compose/{script}"
        assert ref in text, (
            f"e2e-compose.yml does not reference {ref}. The shared script "
            "exists but the workflow inlines its assertions instead of "
            "delegating. Replace the inline bash with "
            f"`bash {ref}`."
        )

    @pytest.mark.parametrize("script", _EXPECTED_SCRIPTS)
    def test_embedded_python_parses(self, script: str) -> None:
        """The shell scripts embed Python via `python3 -c '...'`.
        Without ast-parsing the embedded source, a SyntaxError only
        surfaces when the script actually runs — i.e. when CI hits
        the step. This test catches it at PR time.

        Caught regression (2026-05-15): test-chat-completion.sh had
        `f"... {d.get(\\"usage\\")!r}"` — escaped double quotes
        inside an f-string expression are a Python SyntaxError.
        Inside shell single-quoted `python3 -c '...'`, the right
        form is plain `d.get("usage")` (no shell escape needed).
        """
        import ast

        path = COMPOSE_SCRIPTS_DIR / script
        text = path.read_text(encoding="utf-8")
        # Extract content between `python3 -c '` and the matching
        # closing single quote on its own line. Brittle but precise
        # for the convention these scripts follow.
        lines = text.splitlines()
        py_lines: list[str] = []
        in_py = False
        for line in lines:
            if "python3 -c '" in line and not in_py:
                in_py = True
                continue
            if in_py and line.strip() == "'":
                break
            if in_py:
                py_lines.append(line)
        if not py_lines:
            return  # script doesn't embed Python — exempt
        source = "\n".join(py_lines)
        try:
            ast.parse(source)
        except SyntaxError as e:
            raise AssertionError(
                f"Embedded Python in {path} has a SyntaxError: {e!r}. "
                "Common causes:\n"
                '  - escaped quotes (`\\"`) inside f-string expressions — '
                "use plain double quotes inside shell single-quoted "
                "python3 -c '...' blocks\n"
                "  - mismatched braces inside f-string `{...}` parts\n"
                f"\nProblematic snippet:\n{source}"
            ) from e

    def test_workflow_has_no_inline_curl_command(self) -> None:
        """Catches the regression where a `curl` command is inlined
        into the workflow YAML instead of being moved into a shared
        script. The parametrized `test_workflow_references_script`
        test (above) catches "script not used"; this catches the
        weaker form "script used AND new bash inlined alongside."

        Curl commands inside the shared scripts are fine — this
        test only inspects the workflow file, not the .sh files.

        Exception: the curl in step "Wait for /health" is a polling
        loop with retry logic that's awkward to extract; allow that
        one specific pattern."""
        if not E2E_COMPOSE_WORKFLOW.exists():
            return
        text = E2E_COMPOSE_WORKFLOW.read_text(encoding="utf-8")
        # Strip comments + blank lines so we focus on actual commands.
        non_comment_lines = [
            line
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        # Curl commands that aren't part of the health-poll retry loop.
        # The health-poll's `curl -sf --max-time 5 -k .../health` is
        # the one allowed inline curl (retry semantics don't translate
        # cleanly to a one-shot script).
        suspicious = [
            line
            for line in non_comment_lines
            if "curl " in line and "/health" not in line  # poll-loop exception
        ]
        assert not suspicious, (
            "e2e-compose.yml has inline `curl` command(s) outside the "
            "health-poll retry loop. Move to a shared script under "
            "tests/integration/compose/ and delegate from the workflow "
            "via `bash tests/integration/compose/<script>.sh`. "
            "Offending lines:\n" + "\n".join(suspicious[:5])
        )


# ─────────────────────────────────────────────────────────────────────
# B7 steps 6-9 — profile gating drift guards
# ─────────────────────────────────────────────────────────────────────

# Services that are part of the default-up stack (no profile gate).
# Adding a new core service means adding it here AND keeping it
# without a `profiles:` key in compose. Removing one means deleting
# from here AND putting it under a profile.
_CORE_SERVICES: frozenset[str] = frozenset(
    {
        "memory-server",
        "postgres",
        "chromadb",
        "keycloak",
        "redis",
        "rabbitmq",
        "minio",
        "traefik",
    }
)

# Documented opt-in profiles + the services each enables. Asserted
# below as the load-bearing "operators know what to expect" claim.
_EXPECTED_PROFILES: dict[str, frozenset[str]] = {
    "mock-llm": frozenset({"mock-llm"}),
    "vault": frozenset({"vault"}),
    "obs": frozenset({"otel-collector", "tempo", "loki", "grafana"}),
    "langfuse": frozenset({"langfuse-web", "langfuse-postgres"}),
    "content-control": frozenset({"cc-clamd", "cc-control-plane"}),
}


class TestComposeProfileGating:
    """B7 step 11 — every compose service belongs to EITHER the
    core default-up list OR a documented profile. Catches:

      - New service added without a profile gate → would run on
        every `docker compose up` and bloat the default-CI cost
      - Service moved to a new profile name that operators don't
        know about (drift between AGENTS.md table and the file)
      - Documented profile that no service actually uses (orphan
        profile name in AGENTS.md)

    Anchors:
      - docs/architecture/b7-docker-compose-revive-plan.md §3 (Q1)
      - AGENTS.md "Compose profiles" table
      - feedback_no_more_drifts
    """

    COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

    def _compose_doc(self) -> dict:
        with self.COMPOSE_FILE.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    def test_every_service_is_core_or_profiled(self) -> None:
        doc = self._compose_doc()
        services = doc.get("services", {})
        unaccounted = []
        for name, svc in services.items():
            if name in _CORE_SERVICES:
                continue
            profiles = svc.get("profiles") or []
            if not profiles:
                unaccounted.append(name)
        assert not unaccounted, (
            f"Compose services missing a profile gate (and not in _CORE_SERVICES): "
            f"{unaccounted}. Either add them to _CORE_SERVICES (if they should "
            "run by default) or wire them under a `profiles: [...]` key. "
            "Bloating the default-up stack costs CI RAM."
        )

    def test_no_orphan_core_service_in_compose(self) -> None:
        doc = self._compose_doc()
        services = set(doc.get("services", {}).keys())
        orphans = _CORE_SERVICES - services
        assert not orphans, (
            f"Compose missing services declared core in _CORE_SERVICES: "
            f"{sorted(orphans)}. Either restore the service or remove "
            "the name from the expected core set."
        )

    @pytest.mark.parametrize(
        "profile,expected_services",
        sorted(_EXPECTED_PROFILES.items()),
    )
    def test_profile_has_documented_services(
        self, profile: str, expected_services: frozenset[str]
    ) -> None:
        doc = self._compose_doc()
        services = doc.get("services", {})
        actual = {
            name
            for name, svc in services.items()
            if profile in (svc.get("profiles") or [])
        }
        # `mock-llm` may also include the `ci` profile alias; that
        # doesn't matter for this membership check.
        missing = expected_services - actual
        extra = actual - expected_services
        assert not missing, (
            f"Profile {profile!r} missing services: {sorted(missing)}. "
            "Either restore the service definition or update "
            "_EXPECTED_PROFILES + AGENTS.md."
        )
        assert not extra, (
            f"Profile {profile!r} has extra services not in the docs: "
            f"{sorted(extra)}. Update AGENTS.md's profile table "
            "AND tests/test_compose_drift.py::_EXPECTED_PROFILES."
        )

    def test_agents_md_documents_every_profile(self) -> None:
        """AGENTS.md's profile table must mention every profile name
        from _EXPECTED_PROFILES. Catches the drift where a service
        is added under a new profile but the docs don't catch up."""
        agents_md = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        missing = [p for p in _EXPECTED_PROFILES if f"`{p}`" not in agents_md]
        assert not missing, (
            f"AGENTS.md is missing documentation for profile(s): {missing}. "
            "Add a row to the 'Compose profiles' table under the "
            "'docker-compose stack' subsection."
        )


# ─────────────────────────────────────────────────────────────────────
# B7 step 11 — obs config file existence
# ─────────────────────────────────────────────────────────────────────


class TestObsConfigFiles:
    """B7 step 7 wired the obs profile to mount config files from
    config/compose/{otel-collector,tempo,loki,grafana}/. The
    services would refuse to start if those files are missing;
    this guard catches the deletion at PR time."""

    CONFIG_ROOT = REPO_ROOT / "config" / "compose"

    @pytest.mark.parametrize(
        "rel_path",
        [
            "otel-collector/otel-collector-config.yaml",
            "tempo/tempo.yaml",
            "loki/loki-config.yaml",
            "grafana/provisioning/datasources/audittrace.yaml",
        ],
    )
    def test_obs_config_file_exists(self, rel_path: str) -> None:
        path = self.CONFIG_ROOT / rel_path
        assert path.exists(), (
            f"Obs profile config file missing: {path}. The "
            "{otel-collector,tempo,loki,grafana} services mount it "
            "via docker-compose.yml; without it the service fails to start."
        )

    @pytest.mark.parametrize(
        "rel_path",
        [
            "otel-collector/otel-collector-config.yaml",
            "tempo/tempo.yaml",
            "loki/loki-config.yaml",
            "grafana/provisioning/datasources/audittrace.yaml",
        ],
    )
    def test_obs_config_file_parses_as_yaml(self, rel_path: str) -> None:
        path = self.CONFIG_ROOT / rel_path
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as fh:
                yaml.safe_load(fh)
        except yaml.YAMLError as e:
            raise AssertionError(
                f"Obs profile config {path} is not valid YAML: {e!r}"
            ) from e


# ─────────────────────────────────────────────────────────────────────
# B7 step 9 — content-control fixtures
# ─────────────────────────────────────────────────────────────────────

_CC_FIXTURE_FILES = (
    "eicar.txt",
    "clean-memo.md",
)


class TestContentControlFixtures:
    """B7 step 9 commits compose-side fixtures for the
    content-control profile's eventual scan-pipeline E2E test.
    Fixtures must exist on PR time so a future step 9b can drive
    them without re-generating."""

    FIXTURES_DIR = REPO_ROOT / "tests" / "integration" / "compose" / "fixtures"

    @pytest.mark.parametrize("name", _CC_FIXTURE_FILES)
    def test_fixture_exists(self, name: str) -> None:
        path = self.FIXTURES_DIR / name
        assert path.exists(), (
            f"Content-control fixture missing: {path}. B7 step 9 commits "
            "these for the eventual scan-pipeline E2E test. Restore from "
            "git history or regenerate."
        )

    def test_eicar_fixture_has_canonical_signature(self) -> None:
        """The EICAR fixture must contain the canonical 68-byte
        EICAR signature string so any conforming AV (clamd
        included) flags it as malware."""
        path = self.FIXTURES_DIR / "eicar.txt"
        if not path.exists():
            return
        content = path.read_text(encoding="utf-8")
        assert "EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in content, (
            f"eicar.txt at {path} does not contain the canonical "
            "EICAR signature. Replace with the standard string: "
            r"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
        )
