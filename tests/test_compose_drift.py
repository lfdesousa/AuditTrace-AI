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
