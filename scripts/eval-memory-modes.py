#!/usr/bin/env python3
"""Benchmark the AuditTrace-AI memory proxy in ``inject`` vs ``tools`` mode.

This script is the quantitative-measurement follow-up for ADR-025 Phase 5
("tool routing status"). It fires a fixed set of probe prompts through the
chat-completions proxy under each memory mode and records:

* Wall-clock latency per probe.
* Token accounting from the OpenAI-compatible ``usage`` block.
* In ``tools`` mode: which memory tools were actually invoked (read from
  the ``tool_calls`` audit table), and whether that matches the expected
  tool for the probe.

Design constraints (user-stated, non-negotiable):

* **Sequential**: probes run one at a time. No asyncio, no threads.
* **Two runs per prompt**: the script flips ``AUDITTRACE_MEMORY_MODE`` in
  ``.env``, restarts the ``memory-server`` container, waits for
  ``/health``, then runs all prompts in that mode. Two restarts total.
* **Bounded blast radius**: original ``.env`` is restored on exit via
  ``try/finally``, including on ``KeyboardInterrupt``.

The script intentionally does **not** import from the ``audittrace``
package so it can run against any deployed stack where the repo layout
may differ.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import signal
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

import requests
import urllib3

# Self-signed Traefik cert in dev — suppress the per-request warning spam.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REPO_ROOT = Path("/home/lfdesousa/work/AuditTrace-AI")
ENV_FILE = REPO_ROOT / ".env"
SECRET_FILE = REPO_ROOT / "secrets" / "dev_client_secret.txt"
MINT_SCRIPT = REPO_ROOT / "scripts" / "mint-dev-jwt.sh"
OUTPUT_DIR = REPO_ROOT / "tmp"

PROXY_URL = "https://localhost/v1/chat/completions"
HEALTH_URL = "https://localhost/health"
MEMORY_CONTAINER = "audittrace-server"
POSTGRES_CONTAINER = "audittrace-postgres"
POSTGRES_DB = "audittrace"
POSTGRES_USER = "audittrace"

REQUEST_TIMEOUT_S = 1800  # 30 min — allows long <think> reasoning (ADR-034)
HEALTH_POLL_TIMEOUT_S = 120
HEALTH_POLL_INTERVAL_S = 2
CHECKPOINT_EVERY = 10
MODE_ENV_KEY = "AUDITTRACE_MEMORY_MODE"
ANCHOR_LINE = "KEYCLOAK_ADMIN_PASSWORD=admin"

logger = logging.getLogger("eval-memory-modes")


# ---------------------------------------------------------------------------
# Prompt set
# ---------------------------------------------------------------------------

# Each tuple is (prompt_text, expected_tool_or_None, category).
# ``expected_tool`` is None for the "no tool" control bucket and for
# ambiguous prompts where multiple tools are plausibly correct — the
# accuracy score for ambiguous prompts is computed specially (see
# ``score_tool_selection``).
PROMPTS: list[tuple[str, str | None, str]] = [
    # --- recall_decisions (ADR / architectural history) — 25 ---
    ("What did ADR-025 decide about memory-as-tools?", "recall_decisions", "decisions"),
    (
        "Why did we reject Langchain for the tool-call loop?",
        "recall_decisions",
        "decisions",
    ),
    ("Summarise ADR-024 on proxy pass-through.", "recall_decisions", "decisions"),
    ("What ADRs cover multi-user identity?", "recall_decisions", "decisions"),
    (
        "Which ADR documents the four-layer memory port?",
        "recall_decisions",
        "decisions",
    ),
    (
        "What decision did we make about KV cache compression?",
        "recall_decisions",
        "decisions",
    ),
    ("Why is AUDITTRACE_MEMORY_MODE a kill switch?", "recall_decisions", "decisions"),
    (
        "Recall the reasoning behind transparent proxy augmentation.",
        "recall_decisions",
        "decisions",
    ),
    ("What architectural choice did ADR-018 settle?", "recall_decisions", "decisions"),
    ("Which ADR covers full agentic trace capture?", "recall_decisions", "decisions"),
    (
        "What did we decide about async server architecture?",
        "recall_decisions",
        "decisions",
    ),
    (
        "Recall prior decisions about the embedding server.",
        "recall_decisions",
        "decisions",
    ),
    (
        "Why did we pick TOML for tools config overrides?",
        "recall_decisions",
        "decisions",
    ),
    ("What ADR accepted the memory-as-tools pattern?", "recall_decisions", "decisions"),
    (
        "Summarise the ADR on Langfuse trace decoupling.",
        "recall_decisions",
        "decisions",
    ),
    (
        "What prior decision governs tool_calls audit writes?",
        "recall_decisions",
        "decisions",
    ),
    (
        "Which ADR accepted Keycloak-delegated identity?",
        "recall_decisions",
        "decisions",
    ),
    (
        "Recall our decision on streaming SSE response tails.",
        "recall_decisions",
        "decisions",
    ),
    (
        "What ADR authorises the ToolResultCache Redis pattern?",
        "recall_decisions",
        "decisions",
    ),
    (
        "Why did we keep langchain-core but drop langchain-community?",
        "recall_decisions",
        "decisions",
    ),
    ("What does ADR-026 say about scope naming?", "recall_decisions", "decisions"),
    (
        "Recall our reasoning on synchronous vs async audit writes.",
        "recall_decisions",
        "decisions",
    ),
    ("Why was the iteration cap defaulted to 5?", "recall_decisions", "decisions"),
    (
        "What decision covers ambient context token budget?",
        "recall_decisions",
        "decisions",
    ),
    (
        "Summarise prior ADR content on tool registry design.",
        "recall_decisions",
        "decisions",
    ),
    # --- recall_skills (how-to / workflow / conventions) — 25 ---
    ("How do I mint a dev JWT for smoke testing?", "recall_skills", "skills"),
    (
        "What's the convention for writing a Structurizr DSL component?",
        "recall_skills",
        "skills",
    ),
    ("Show me the Terraform naming conventions we use.", "recall_skills", "skills"),
    ("How do I add a new memory tool handler?", "recall_skills", "skills"),
    ("What's the workflow for creating a new ADR?", "recall_skills", "skills"),
    ("How do C4 component diagrams work in this repo?", "recall_skills", "skills"),
    ("Walk me through running the E2E test suite.", "recall_skills", "skills"),
    (
        "How do I regenerate requirements.txt after editing pyproject.toml?",
        "recall_skills",
        "skills",
    ),
    (
        "What's the procedure for rotating the dev client secret?",
        "recall_skills",
        "skills",
    ),
    ("How do I run pre-commit hooks locally?", "recall_skills", "skills"),
    ("How do we format Python code in this project?", "recall_skills", "skills"),
    ("Show me how to configure a new Traefik route.", "recall_skills", "skills"),
    ("How do I seed ChromaDB with the memory corpus?", "recall_skills", "skills"),
    (
        "What's the Bruno collection structure for smoke tests?",
        "recall_skills",
        "skills",
    ),
    ("How do I export the C4 static site for review?", "recall_skills", "skills"),
    ("Walk me through adding a new Keycloak scope.", "recall_skills", "skills"),
    (
        "How does the Langfuse span nesting convention work here?",
        "recall_skills",
        "skills",
    ),
    (
        "What's the procedure for running docker compose up from scratch?",
        "recall_skills",
        "skills",
    ),
    ("How do I write a new test_memory_tool_handlers case?", "recall_skills", "skills"),
    ("Show me the pattern for wrapping a ChromaDB query.", "recall_skills", "skills"),
    ("How do I add a new tool to the MEMORY_TOOL_REGISTRY?", "recall_skills", "skills"),
    ("What's the workflow for generating a client keypair?", "recall_skills", "skills"),
    (
        "How does the MinIO healthcheck work in this compose file?",
        "recall_skills",
        "skills",
    ),
    ("Show me how to write a Google-style docstring here.", "recall_skills", "skills"),
    (
        "How do I update the workspace.dsl after adding a service?",
        "recall_skills",
        "skills",
    ),
    # --- recall_recent_sessions (temporal / recent work) — 15 ---
    ("What did we work on yesterday?", "recall_recent_sessions", "recent_sessions"),
    (
        "Summarise recent changes to the memory proxy.",
        "recall_recent_sessions",
        "recent_sessions",
    ),
    (
        "What was the last thing we committed to main?",
        "recall_recent_sessions",
        "recent_sessions",
    ),
    (
        "Remind me what we did in last week's sessions.",
        "recall_recent_sessions",
        "recent_sessions",
    ),
    (
        "What was the latest session focused on?",
        "recall_recent_sessions",
        "recent_sessions",
    ),
    (
        "Recap our most recent work on observability.",
        "recall_recent_sessions",
        "recent_sessions",
    ),
    (
        "What did we change in the E2E suite recently?",
        "recall_recent_sessions",
        "recent_sessions",
    ),
    (
        "Summarise our recent discussion about tool routing.",
        "recall_recent_sessions",
        "recent_sessions",
    ),
    (
        "What were we debugging in the last session?",
        "recall_recent_sessions",
        "recent_sessions",
    ),
    (
        "What was the most recent mypy cleanup about?",
        "recall_recent_sessions",
        "recent_sessions",
    ),
    (
        "Recall the context of our last conversation.",
        "recall_recent_sessions",
        "recent_sessions",
    ),
    (
        "What have we shipped in the past few days?",
        "recall_recent_sessions",
        "recent_sessions",
    ),
    (
        "What were the recent CI fixes about?",
        "recall_recent_sessions",
        "recent_sessions",
    ),
    (
        "Summarise the last MinIO-related session.",
        "recall_recent_sessions",
        "recent_sessions",
    ),
    (
        "What did we discuss most recently about the PhD trajectory?",
        "recall_recent_sessions",
        "recent_sessions",
    ),
    # --- recall_semantic (open-ended knowledge / RAG) — 15 ---
    (
        "Explain how OpenAI-compatible tool calling works at the protocol level.",
        "recall_semantic",
        "semantic",
    ),
    (
        "What does KV cache compression actually do to a transformer?",
        "recall_semantic",
        "semantic",
    ),
    (
        "How does Keycloak's client_credentials grant differ from password grant?",
        "recall_semantic",
        "semantic",
    ),
    (
        "Describe the trade-offs of MoE models like Qwen3.5-35B-A3B.",
        "recall_semantic",
        "semantic",
    ),
    (
        "What is the CAP theorem and how does it apply to Postgres replication?",
        "recall_semantic",
        "semantic",
    ),
    (
        "Explain how Traefik picks a backend for a given Host header.",
        "recall_semantic",
        "semantic",
    ),
    (
        "What does FastAPI dependency injection buy us over plain Starlette?",
        "recall_semantic",
        "semantic",
    ),
    (
        "Describe the JWT validation flow for a bearer token in general terms.",
        "recall_semantic",
        "semantic",
    ),
    (
        "How does ChromaDB implement approximate nearest-neighbour search?",
        "recall_semantic",
        "semantic",
    ),
    (
        "Explain the difference between episodic and semantic memory in cognitive science.",
        "recall_semantic",
        "semantic",
    ),
    ("What is OAuth2 PKCE and when would we need it?", "recall_semantic", "semantic"),
    (
        "Describe how OpenTelemetry propagates trace context across processes.",
        "recall_semantic",
        "semantic",
    ),
    (
        "Explain how server-sent events differ from websockets.",
        "recall_semantic",
        "semantic",
    ),
    ("What is Redis's RESP protocol?", "recall_semantic", "semantic"),
    (
        "Describe how C4 diagrams differ from UML component diagrams.",
        "recall_semantic",
        "semantic",
    ),
    # --- ambiguous (multiple tools plausibly correct) — 10 ---
    ("How did we decide to structure our Structurizr workspace?", None, "ambiguous"),
    (
        "What recent decisions affect how I should write a new tool handler?",
        None,
        "ambiguous",
    ),
    (
        "Recap our architectural choices from the last week of sessions.",
        None,
        "ambiguous",
    ),
    ("What's the background on the current Traefik routing setup?", None, "ambiguous"),
    ("How do the ADRs inform our Terraform naming practice?", None, "ambiguous"),
    ("What recent work touched the memory tool registry?", None, "ambiguous"),
    (
        "Explain the design thinking behind our ambient context builder.",
        None,
        "ambiguous",
    ),
    ("How has the memory-as-tools pattern evolved across sessions?", None, "ambiguous"),
    ("What should I know before editing the chat proxy route?", None, "ambiguous"),
    (
        "How do prior decisions constrain how I add a new Keycloak scope?",
        None,
        "ambiguous",
    ),
    # --- control / no-tool (pure reasoning) — 10 ---
    ("What is 2 + 2?", None, "control"),
    ("Reverse the string 'hello'.", None, "control"),
    ("Spell the word 'necessary' letter by letter.", None, "control"),
    ("What is the capital of France?", None, "control"),
    ("List the first five prime numbers.", None, "control"),
    ("Translate 'good morning' into Spanish.", None, "control"),
    ("What is 7 times 8?", None, "control"),
    ("Count the vowels in the word 'encyclopaedia'.", None, "control"),
    ("Write a haiku about rain.", None, "control"),
    ("What colour do you get if you mix red and blue?", None, "control"),
]


# ---------------------------------------------------------------------------
# Result schema
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    """One probe outcome, serialised as one JSONL line."""

    mode: str
    index: int
    category: str
    expected_tool: str | None
    prompt: str
    started_at: str
    latency_s: float | None
    http_status: int | None
    error: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    content_chars: int | None
    tools_called: list[str] = field(default_factory=list)
    tool_audit_rows: list[dict[str, Any]] = field(default_factory=list)
    tool_selection_correct: bool | None = None  # tools mode only


# ---------------------------------------------------------------------------
# .env mode switch + container health
# ---------------------------------------------------------------------------


def read_env_file(path: Path) -> str:
    """Return the raw contents of ``.env``."""
    return path.read_text(encoding="utf-8")


def write_env_mode(original: str, mode: str) -> str:
    """Return a new ``.env`` body with ``AUDITTRACE_MEMORY_MODE=<mode>`` set.

    If the line already exists it is replaced in place; otherwise the line
    is inserted immediately after the ``KEYCLOAK_ADMIN_PASSWORD=admin``
    anchor (as specified by the operator).

    Args:
        original: The unmodified ``.env`` body.
        mode: Either ``"inject"`` or ``"tools"``.

    Returns:
        The rewritten ``.env`` body.
    """
    new_line = f"{MODE_ENV_KEY}={mode}"
    lines = original.splitlines()
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{MODE_ENV_KEY}="):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        # Insert after the anchor line; fall back to append.
        anchor_idx = next(
            (i for i, ln in enumerate(lines) if ln.strip() == ANCHOR_LINE),
            None,
        )
        if anchor_idx is None:
            lines.append(new_line)
        else:
            lines.insert(anchor_idx + 1, new_line)
    # Preserve trailing newline if the original had one.
    body = "\n".join(lines)
    if original.endswith("\n"):
        body += "\n"
    return body


def restart_memory_server() -> None:
    """Recreate the memory-server container so it picks up the new env."""
    logger.info("restarting memory-server container")
    subprocess.run(
        [
            "docker",
            "compose",
            "up",
            "-d",
            "--no-deps",
            "memory-server",
        ],
        cwd=str(REPO_ROOT),
        check=True,
    )


def wait_for_health(deadline_s: float = HEALTH_POLL_TIMEOUT_S) -> None:
    """Block until ``/health`` returns 200 or ``deadline_s`` elapses.

    Raises:
        RuntimeError: If the service never comes up within the deadline.
    """
    start = time.monotonic()
    last_err: str | None = None
    while time.monotonic() - start < deadline_s:
        try:
            resp = requests.get(HEALTH_URL, verify=False, timeout=5)
            if resp.status_code == 200:
                logger.info(
                    "memory-server healthy after %.1fs", time.monotonic() - start
                )
                return
            last_err = f"status={resp.status_code}"
        except requests.RequestException as exc:
            last_err = str(exc)
        time.sleep(HEALTH_POLL_INTERVAL_S)
    raise RuntimeError(f"memory-server did not become healthy: {last_err}")


def switch_mode(mode: str, original_env: str) -> None:
    """Rewrite ``.env``, restart the container, and wait for health."""
    new_body = write_env_mode(original_env, mode)
    ENV_FILE.write_text(new_body, encoding="utf-8")
    logger.info("switched .env AUDITTRACE_MEMORY_MODE=%s", mode)
    restart_memory_server()
    wait_for_health()


# ---------------------------------------------------------------------------
# JWT minting (docker exec pattern from scripts/mint-dev-jwt.sh)
# ---------------------------------------------------------------------------


def mint_jwt() -> str:
    """Mint a fresh dev JWT via the in-container ``mint-dev-jwt.sh`` helper.

    Copies the mint script into the memory-server container on every call
    (cheap; it's a few KB) so the flow does not depend on prior state, then
    invokes it with ``CLIENT_SECRET`` passed via ``-e``.

    Returns:
        The raw access token on success.

    Raises:
        RuntimeError: On any subprocess failure.
    """
    if not SECRET_FILE.exists():
        raise RuntimeError(f"client secret file not found: {SECRET_FILE}")
    secret = SECRET_FILE.read_text(encoding="utf-8").strip()

    subprocess.run(
        ["docker", "cp", str(MINT_SCRIPT), f"{MEMORY_CONTAINER}:/tmp/"],
        check=True,
        capture_output=True,
    )
    proc = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"CLIENT_SECRET={secret}",
            MEMORY_CONTAINER,
            "bash",
            "/tmp/mint-dev-jwt.sh",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    token = proc.stdout.strip()
    if not token:
        raise RuntimeError("mint-dev-jwt.sh produced empty output")
    return token


# ---------------------------------------------------------------------------
# tool_calls audit lookup
# ---------------------------------------------------------------------------


def query_tool_audit(since_utc: str) -> list[dict[str, Any]]:
    """Return the ``tool_calls`` rows started after ``since_utc``.

    The ``since_utc`` parameter must be an ISO-8601 string parseable by
    Postgres ``timestamptz``. Rows are returned oldest-first so the caller
    can correlate multiple calls in a single probe.

    Args:
        since_utc: An ISO-8601 timestamp taken immediately before the probe.

    Returns:
        A list of dicts with ``tool_name``, ``duration_ms``, ``error``,
        ``granted_scope``.
    """
    sql = (
        "SELECT tool_name, COALESCE(duration_ms, 0), COALESCE(error, ''), "
        "COALESCE(granted_scope, '') "
        f"FROM tool_calls WHERE started_at >= '{since_utc}' "
        "ORDER BY started_at ASC;"
    )
    try:
        proc = subprocess.run(
            [
                "docker",
                "exec",
                POSTGRES_CONTAINER,
                "psql",
                "-U",
                POSTGRES_USER,
                "-d",
                POSTGRES_DB,
                "-tAF",
                "|",
                "-c",
                sql,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("tool_calls lookup failed: %s", exc)
        return []

    rows: list[dict[str, Any]] = []
    for raw in proc.stdout.strip().splitlines():
        if not raw:
            continue
        parts = raw.split("|")
        if len(parts) < 4:
            continue
        rows.append(
            {
                "tool_name": parts[0],
                "duration_ms": int(parts[1]) if parts[1].isdigit() else 0,
                "error": parts[2],
                "granted_scope": parts[3],
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Proxy probe
# ---------------------------------------------------------------------------


class TokenHolder:
    """Minimal re-mint-on-401 wrapper around the dev JWT."""

    def __init__(self) -> None:
        self._token: str | None = None

    def get(self) -> str:
        if self._token is None:
            self._token = mint_jwt()
        return self._token

    def refresh(self) -> str:
        logger.info("re-minting JWT after 401")
        self._token = mint_jwt()
        return self._token


def fire_probe(
    token_holder: TokenHolder,
    mode: str,
    index: int,
    prompt: str,
    expected_tool: str | None,
    category: str,
) -> ProbeResult:
    """Send one chat-completions request and collect everything measurable.

    Args:
        token_holder: JWT minting state (re-mints on 401).
        mode: ``"inject"`` or ``"tools"`` — recorded on the result only.
        index: Zero-based prompt index within the run.
        prompt: The user-turn content to send.
        expected_tool: The tool name the prompt *should* trigger in tools
            mode, or ``None`` for no-tool / ambiguous prompts.
        category: Human-readable bucket name for the results table.

    Returns:
        A fully populated :class:`ProbeResult`.
    """
    # `started_at` is used both for the audit lookup and for the result
    # row. Taken just before the request goes out to minimise the window
    # in which concurrent traffic (if any) could pollute the audit read.
    started_at_dt = datetime.now(UTC)
    started_at = started_at_dt.isoformat()
    payload = {
        "model": "qwen3.5-35b-a3b",
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "temperature": 0.0,
    }
    result = ProbeResult(
        mode=mode,
        index=index,
        category=category,
        expected_tool=expected_tool,
        prompt=prompt,
        started_at=started_at,
        latency_s=None,
        http_status=None,
        error=None,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        content_chars=None,
    )

    def _post(tok: str) -> requests.Response:
        return requests.post(
            PROXY_URL,
            headers={
                "Authorization": f"Bearer {tok}",
                "Content-Type": "application/json",
            },
            json=payload,
            verify=False,
            timeout=REQUEST_TIMEOUT_S,
        )

    t0 = time.monotonic()
    try:
        resp = _post(token_holder.get())
        if resp.status_code == 401:
            resp = _post(token_holder.refresh())
        result.latency_s = time.monotonic() - t0
        result.http_status = resp.status_code
        if resp.status_code != 200:
            result.error = resp.text[:500]
        else:
            body = resp.json()
            usage = body.get("usage", {}) or {}
            result.prompt_tokens = usage.get("prompt_tokens")
            result.completion_tokens = usage.get("completion_tokens")
            result.total_tokens = usage.get("total_tokens")
            choices = body.get("choices") or [{}]
            content = (choices[0].get("message") or {}).get("content") or ""
            result.content_chars = len(content)
    except requests.RequestException as exc:
        result.latency_s = time.monotonic() - t0
        result.error = f"{type(exc).__name__}: {exc}"

    # Audit lookup only matters in tools mode — but we also run it in
    # inject mode to confirm the legacy path truly writes zero rows.
    rows = query_tool_audit(started_at)
    result.tool_audit_rows = rows
    result.tools_called = [r["tool_name"] for r in rows]
    if mode == "tools":
        result.tool_selection_correct = score_tool_selection(
            expected_tool, result.tools_called, category
        )
    return result


def score_tool_selection(
    expected: str | None, called: list[str], category: str
) -> bool:
    """Return True iff the observed tool calls match the expectation.

    Rules:
    * ``expected is not None`` (concrete expected tool): pass iff that
      tool name appears in ``called``. Extra tools are tolerated — the
      model often calls a companion tool, which is fine as long as the
      primary was hit.
    * ``expected is None`` and ``category == "control"``: pass iff
      ``called`` is empty. Controls are the "did not over-fire" test.
    * ``expected is None`` and ``category == "ambiguous"``: pass iff at
      least one memory tool was called (any of the four). Ambiguous
      prompts test "did the model route *somewhere* sensible".
    """
    memory_tools = {
        "recall_decisions",
        "recall_skills",
        "recall_recent_sessions",
        "recall_semantic",
    }
    if expected is not None:
        return expected in called
    if category == "control":
        return len(called) == 0
    if category == "ambiguous":
        return any(t in memory_tools for t in called)
    return False


# ---------------------------------------------------------------------------
# Run orchestration + reporting
# ---------------------------------------------------------------------------


def flush_jsonl(path: Path, results: list[ProbeResult]) -> None:
    """Atomically overwrite the JSONL checkpoint with all results so far."""
    tmp = path.with_suffix(path.suffix + ".part")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")
    tmp.replace(path)


def run_mode(
    mode: str,
    prompts: list[tuple[str, str | None, str]],
    token_holder: TokenHolder,
    output_path: Path,
    all_results: list[ProbeResult],
    dry_run: bool,
) -> list[ProbeResult]:
    """Execute every prompt in ``prompts`` sequentially against ``mode``.

    Args:
        mode: The memory mode label for the result rows.
        prompts: The ordered ``(prompt, expected_tool, category)`` triples.
        token_holder: Shared JWT holder across modes.
        output_path: JSONL checkpoint target.
        all_results: Accumulator for prior-mode results (appended to).
        dry_run: If True, skip the network entirely and log intent only.

    Returns:
        The list of results produced by this mode alone.
    """
    mode_results: list[ProbeResult] = []
    total = len(prompts)
    for i, (prompt, expected, category) in enumerate(prompts):
        if dry_run:
            logger.info(
                "[DRY] mode=%s [%d/%d] cat=%s expected=%s prompt=%r",
                mode,
                i + 1,
                total,
                category,
                expected,
                prompt[:60],
            )
            continue
        logger.info(
            "mode=%s [%d/%d] cat=%s expected=%s",
            mode,
            i + 1,
            total,
            category,
            expected,
        )
        result = fire_probe(
            token_holder=token_holder,
            mode=mode,
            index=i,
            prompt=prompt,
            expected_tool=expected,
            category=category,
        )
        mode_results.append(result)
        all_results.append(result)
        # Streaming per-probe summary on stdout for live visibility.
        print(
            f"  -> http={result.http_status} lat={_fmt(result.latency_s, '.2f')}s "
            f"tok={result.total_tokens} "
            f"tools={','.join(result.tools_called) or '-'} "
            f"correct={result.tool_selection_correct}",
            flush=True,
        )
        if (i + 1) % CHECKPOINT_EVERY == 0:
            flush_jsonl(output_path, all_results)
    if not dry_run:
        flush_jsonl(output_path, all_results)
    return mode_results


def _fmt(val: Any, spec: str) -> str:
    if val is None:
        return "n/a"
    return format(val, spec)


def summarise(results: list[ProbeResult]) -> None:
    """Print the comparison table and per-category accuracy to stdout."""
    by_mode: dict[str, list[ProbeResult]] = {"inject": [], "tools": []}
    for r in results:
        by_mode.setdefault(r.mode, []).append(r)

    def agg(mode: str, pick: str) -> list[float]:
        vals = [getattr(r, pick) for r in by_mode[mode] if getattr(r, pick) is not None]
        return [float(v) for v in vals]

    def mean(xs: list[float]) -> str:
        return f"{statistics.mean(xs):.2f}" if xs else "n/a"

    def p95(xs: list[float]) -> str:
        if not xs:
            return "n/a"
        xs_sorted = sorted(xs)
        idx = min(len(xs_sorted) - 1, int(round(0.95 * (len(xs_sorted) - 1))))
        return f"{xs_sorted[idx]:.2f}"

    def error_rate(mode: str) -> str:
        rows = by_mode[mode]
        if not rows:
            return "n/a"
        errs = sum(1 for r in rows if r.error or (r.http_status or 0) >= 400)
        return f"{100 * errs / len(rows):.1f}%"

    tools_rows = by_mode["tools"]
    scored = [r for r in tools_rows if r.tool_selection_correct is not None]
    acc_overall = (
        f"{100 * sum(1 for r in scored if r.tool_selection_correct) / len(scored):.1f}%"
        if scored
        else "n/a"
    )

    header = f"{'':32s}{'inject':>15s}{'tools':>15s}"
    print()
    print("=" * len(header))
    print(header)
    print("=" * len(header))
    rows = [
        (
            "mean prompt_tokens",
            mean(agg("inject", "prompt_tokens")),
            mean(agg("tools", "prompt_tokens")),
        ),
        (
            "mean completion_tokens",
            mean(agg("inject", "completion_tokens")),
            mean(agg("tools", "completion_tokens")),
        ),
        (
            "mean total_tokens",
            mean(agg("inject", "total_tokens")),
            mean(agg("tools", "total_tokens")),
        ),
        (
            "mean latency_s",
            mean(agg("inject", "latency_s")),
            mean(agg("tools", "latency_s")),
        ),
        (
            "p95 latency_s",
            p95(agg("inject", "latency_s")),
            p95(agg("tools", "latency_s")),
        ),
        (
            "mean content_chars",
            mean(agg("inject", "content_chars")),
            mean(agg("tools", "content_chars")),
        ),
        ("tool_selection_accuracy", "n/a", acc_overall),
        ("error_rate", error_rate("inject"), error_rate("tools")),
        ("n", str(len(by_mode["inject"])), str(len(by_mode["tools"]))),
    ]
    for name, a, b in rows:
        print(f"{name:32s}{a:>15s}{b:>15s}")
    print("=" * len(header))

    # Per-category accuracy breakdown (tools mode only).
    print()
    print("tools-mode accuracy by category:")
    cats: dict[str, list[ProbeResult]] = {}
    for r in tools_rows:
        cats.setdefault(r.category, []).append(r)
    for cat in sorted(cats):
        cat_scored = [r for r in cats[cat] if r.tool_selection_correct is not None]
        if not cat_scored:
            print(f"  {cat:20s} n=0  n/a")
            continue
        ok = sum(1 for r in cat_scored if r.tool_selection_correct)
        pct = 100 * ok / len(cat_scored)
        print(
            f"  {cat:20s} n={len(cat_scored):3d}  {pct:5.1f}%  ({ok}/{len(cat_scored)})"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode-order",
        choices=["tools-first", "inject-first"],
        default="tools-first",
        help="Which mode runs first. Default: tools-first.",
    )
    parser.add_argument(
        "--n-per-mode",
        type=int,
        default=100,
        help="How many prompts to run in each mode (useful: --n-per-mode 10 for a smoke).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would run without touching .env, docker, or the proxy.",
    )
    parser.add_argument(
        "--category",
        default=None,
        help=(
            "Restrict the prompt set to one category "
            "(decisions | skills | recent_sessions | semantic | ambiguous | control). "
            "Default: no filter (first N from the full set, which is 25 decisions first)."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=REQUEST_TIMEOUT_S,
        help=f"Per-request client timeout in seconds (default: {REQUEST_TIMEOUT_S}).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python log level (default: INFO).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    global REQUEST_TIMEOUT_S  # noqa: PLW0603
    REQUEST_TIMEOUT_S = args.timeout

    if shutil.which("docker") is None:
        logger.error("docker binary not found on PATH")
        return 2

    if args.category:
        filtered = [p for p in PROMPTS if p[2] == args.category]
        if not filtered:
            valid = sorted({p[2] for p in PROMPTS})
            logger.error(
                "unknown category %r; valid values: %s", args.category, ", ".join(valid)
            )
            return 2
        prompts = filtered[: args.n_per_mode]
        logger.info(
            "category filter: %s (%d available, %d selected)",
            args.category,
            len(filtered),
            len(prompts),
        )
    else:
        prompts = PROMPTS[: args.n_per_mode]
    if len(prompts) < args.n_per_mode:
        logger.warning(
            "only %d prompts defined; requested %d",
            len(prompts),
            args.n_per_mode,
        )

    mode_sequence = (
        ["tools", "inject"] if args.mode_order == "tools-first" else ["inject", "tools"]
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_path = OUTPUT_DIR / f"eval-memory-modes-{ts}.jsonl"
    logger.info("results will be written to %s", output_path)

    if args.dry_run:
        logger.info("dry run: skipping env mutation and network")
        all_results: list[ProbeResult] = []
        token_holder = TokenHolder()
        for mode in mode_sequence:
            run_mode(
                mode, prompts, token_holder, output_path, all_results, dry_run=True
            )
        logger.info(
            "dry run complete; %d prompts * %d modes = %d probes",
            len(prompts),
            len(mode_sequence),
            len(prompts) * len(mode_sequence),
        )
        return 0

    original_env = read_env_file(ENV_FILE)
    env_hash = hashlib.sha256(original_env.encode("utf-8")).hexdigest()[:12]
    logger.info("original .env snapshot sha256[0:12]=%s", env_hash)

    all_results = []
    token_holder = TokenHolder()

    def _restore(*_: Any) -> None:
        logger.info("restoring original .env")
        ENV_FILE.write_text(original_env, encoding="utf-8")

    # Graceful SIGINT: restore .env, flush results, then exit non-zero.
    def _on_sigint(_signum: int, _frame: FrameType | None) -> None:
        logger.warning("SIGINT received; restoring .env and flushing")
        _restore()
        flush_jsonl(output_path, all_results)
        sys.exit(130)

    signal.signal(signal.SIGINT, _on_sigint)

    try:
        for mode in mode_sequence:
            switch_mode(mode, original_env)
            run_mode(
                mode, prompts, token_holder, output_path, all_results, dry_run=False
            )
        summarise(all_results)
        flush_jsonl(output_path, all_results)
        logger.info("done; %d probes written to %s", len(all_results), output_path)
        return 0
    finally:
        _restore()
        # Best-effort bring the service back up with the original mode.
        try:
            restart_memory_server()
            wait_for_health()
        except Exception as exc:
            logger.warning("failed to restore memory-server after run: %s", exc)


if __name__ == "__main__":
    sys.exit(main())
