"""Tests for the TOKEN-GUARD agent-def drift guard (ties #429 / TH-D #434).

SPEC ``spec-token-guard-kill-show-20260811.md``, layer 3: a mechanical guard
that fails any agent def still teaching the RAW ``audittrace-login --show``
form (the pre-fix, raw-print contract) or a direct read of the token STORE
(``tokens.json``). Layer 1 (killing the leak at its source in
``scripts/audittrace-login``, see ``tests/test_audittrace_login_show.py``) is
the REAL fix; this guard exists to catch drift back to teaching the stale,
dangerous pattern in an agent def — see
`feedback_policies_must_be_mechanically_inviolable`: "a comment/prose
instruction is not a guardrail."

Falsifiability is the point of every test in this file: several tests prove
the guard is non-vacuous by NEUTERING it (monkeypatching its own detection
pattern to something that can never match) and showing the exact synthetic
bad agent-def content that was previously flagged now passes silently — i.e.
the guard is what makes the other tests RED, not an unrelated assertion.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_GUARD_PATH = (
    Path(__file__).parent.parent / "scripts" / "check-agent-def-token-guard.py"
)


def _load_guard():
    """Import the guard script by path — it is a script, not a package module."""
    spec = importlib.util.spec_from_file_location("token_guard", _GUARD_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["token_guard"] = mod
    spec.loader.exec_module(mod)
    return mod


guard = _load_guard()


# ── synthetic fixtures — never depend on the real (gitignored/private) files ──

BAD_DEF_RAW_SHOW = """\
## STEP 2 — Recall prior lessons

```bash
cd bruno/audittrace
TOKEN=$(../../scripts/audittrace-login --show 2>/dev/null | tail -1)
bru run memory/semantic/01-list.bru --env cluster \\
  --env-var accessToken="$TOKEN"
```
"""

BAD_DEF_TOKENS_JSON_CAT = """\
## Debugging

If recall looks stale, sanity-check the token file directly:

```bash
cat ~/.config/audittrace/tokens.json | jq .access_expires_at
```
"""

GOOD_DEF_IN_PROCESS = """\
## STEP 2 — Recall prior lessons (ADR-059, READ side)

```bash
.venv/bin/python -c "
import os
from scripts.deploy.memory import recall_deploy_lessons
items = recall_deploy_lessons(
    'query',
    front_door=os.environ.get('AUDITTRACE_FRONT_DOOR', 'https://audittrace.local'),
)
print(len(items))
"
```
"""

GOOD_DEF_SHOW_UNSAFE_NOT_TAUGHT_TO_AGENT = """\
This is a plain SCRIPT (not an agent def) that legitimately needs the raw
Bearer value:

```bash
BEARER="$(scripts/audittrace-login --show-unsafe)"
```
"""

GOOD_DEF_REDACTED_SHOW_DESCRIBED = """\
- **SESSION-OPEN HUMAN-AUTH GATE:** guarantee a valid token + that the human
  is logged in via `scripts/audittrace-login --show` — since TOKEN-GUARD
  (2026-08-11) this prints a REDACTED `token_fingerprint=<8 hex> exp=<epoch>`
  line, never the raw JWT. Never use `--show-unsafe` here.
"""


# ── find_violations: the core detection unit ──────────────────────────────


class TestFindViolationsRawShow:
    def test_flags_raw_show_capture(self) -> None:
        violations = guard.find_violations(BAD_DEF_RAW_SHOW)
        assert violations, "raw `--show` capture must be flagged"
        assert any("--show" in v for v in violations)

    def test_allows_show_unsafe(self) -> None:
        violations = guard.find_violations(GOOD_DEF_SHOW_UNSAFE_NOT_TAUGHT_TO_AGENT)
        assert violations == []

    def test_allows_show_described_as_redacted(self) -> None:
        violations = guard.find_violations(GOOD_DEF_REDACTED_SHOW_DESCRIBED)
        assert violations == []

    def test_allows_in_process_helper_pattern(self) -> None:
        violations = guard.find_violations(GOOD_DEF_IN_PROCESS)
        assert violations == []

    def test_line_number_reported_matches_source(self) -> None:
        text = "line one\nline two\naudittrace-login --show\nline four"
        violations = guard.find_violations(text)
        assert len(violations) == 1
        assert "line 3" in violations[0]

    def test_unrelated_show_flag_on_other_tool_not_flagged(self) -> None:
        # No "audittrace-login" on the line/window at all — different tool's
        # --show flag must never trigger this guard.
        violations = guard.find_violations("some-other-cli --show 2>/dev/null")
        assert violations == []


class TestFindViolationsTokensJsonRead:
    def test_flags_cat_tokens_json(self) -> None:
        violations = guard.find_violations(BAD_DEF_TOKENS_JSON_CAT)
        assert violations
        assert any("tokens.json" in v for v in violations)

    def test_flags_jq_tokens_json(self) -> None:
        violations = guard.find_violations(
            "jq -r '.access_token' ~/.config/audittrace/tokens.json"
        )
        assert violations

    def test_flags_python_open_tokens_json(self) -> None:
        violations = guard.find_violations(
            'token = open("~/.config/audittrace/tokens.json").read()'
        )
        assert violations

    def test_allows_prose_mention_of_tokens_json(self) -> None:
        text = (
            "Tokens land in ~/.config/audittrace/tokens.json (mode 600). The "
            "sanctioned in-process path reads the token from "
            "~/.config/audittrace/tokens.json inside the process and never "
            "exposes it."
        )
        violations = guard.find_violations(text)
        assert violations == []


class TestFindViolationsFalsifiability:
    """Prove the guard is non-vacuous: neuter its detection pattern and show
    the exact synthetic bad content it used to catch now slips through."""

    def test_neutering_raw_show_pattern_lets_bad_def_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sanity: the guard catches it with its real pattern.
        assert guard.find_violations(BAD_DEF_RAW_SHOW) != []

        # Neuter: replace the detection regex with one that can never match.
        monkeypatch.setattr(guard, "RAW_SHOW_PATTERN", guard.re.compile(r"(?!)"))
        neutered = guard.find_violations(BAD_DEF_RAW_SHOW)
        assert neutered == [], (
            "expected the neutered guard to let the bad def pass (RED) — if "
            "this assertion fails, some OTHER check is catching the "
            "violation and the raw-show pattern isn't actually load-bearing"
        )

    def test_neutering_tokens_json_pattern_lets_bad_def_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert guard.find_violations(BAD_DEF_TOKENS_JSON_CAT) != []
        monkeypatch.setattr(
            guard, "TOKENS_JSON_READ_PATTERN", guard.re.compile(r"(?!)")
        )
        neutered = guard.find_violations(BAD_DEF_TOKENS_JSON_CAT)
        assert neutered == []


# ── scan_paths: filesystem layer ───────────────────────────────────────────


class TestScanPaths:
    def test_scans_and_reports_only_files_with_violations(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad-def.md"
        bad.write_text(BAD_DEF_RAW_SHOW)
        good = tmp_path / "good-def.md"
        good.write_text(GOOD_DEF_IN_PROCESS)

        results = guard.scan_paths([bad, good])

        assert set(results.keys()) == {bad}
        assert results[bad]

    def test_missing_path_is_silently_skipped(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.md"
        results = guard.scan_paths([missing])
        assert results == {}

    def test_directory_path_is_silently_skipped(self, tmp_path: Path) -> None:
        results = guard.scan_paths([tmp_path])
        assert results == {}


# ── default_scan_targets: real-location resolution (best-effort) ──────────


class TestDefaultScanTargets:
    def test_collects_md_files_from_both_locations(self, tmp_path: Path) -> None:
        public_dir = tmp_path / "public-agents"
        public_dir.mkdir()
        (public_dir / "audittrace-builder.md").write_text("x")
        (public_dir / "not-agent.txt").write_text("x")  # non-.md ignored

        private_root = tmp_path / "private-repo"
        private_agents = private_root / "sdlc" / "agents"
        private_agents.mkdir(parents=True)
        (private_agents / "audittrace-reviewer.md").write_text("x")
        opencode_dir = private_agents / "opencode"
        opencode_dir.mkdir()
        (opencode_dir / "audittrace-builder.md").write_text("x")

        targets = guard.default_scan_targets(
            public_agents_dir=public_dir, private_dir=private_root
        )

        names = {p.name for p in targets}
        assert names == {
            "audittrace-builder.md",
            "audittrace-reviewer.md",
        }
        assert len(targets) == 3  # the two top-level + the opencode/ one
        assert all(p.suffix == ".md" for p in targets)

    def test_missing_locations_contribute_nothing(self, tmp_path: Path) -> None:
        targets = guard.default_scan_targets(
            public_agents_dir=tmp_path / "nope",
            private_dir=tmp_path / "also-nope",
        )
        assert targets == []


# ── main(): CLI entrypoint ─────────────────────────────────────────────────


class TestMain:
    def test_exits_1_and_prints_violations_for_bad_paths(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad = tmp_path / "bad-def.md"
        bad.write_text(BAD_DEF_RAW_SHOW)

        rc = guard.main(["--paths", str(bad)])

        assert rc == 1
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert str(bad) in out

    def test_exits_0_for_clean_paths(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        good = tmp_path / "good-def.md"
        good.write_text(GOOD_DEF_IN_PROCESS)

        rc = guard.main(["--paths", str(good)])

        assert rc == 0
        out = capsys.readouterr().out
        assert "clean" in out

    def test_exits_0_when_no_targets_found_anywhere(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(guard, "default_scan_targets", lambda: [])

        rc = guard.main([])

        assert rc == 0
        out = capsys.readouterr().out
        assert "best-effort" in out

    def test_falsifiability_of_the_cli_exit_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neuter find_violations itself (simulating "the guard was
        removed") and prove main() would then exit 0 on a bad def — i.e. the
        CLI's exit code is genuinely driven by the detection function, not a
        hardcoded/decorative success path."""
        bad = tmp_path / "bad-def.md"
        bad.write_text(BAD_DEF_RAW_SHOW)

        assert guard.main(["--paths", str(bad)]) == 1  # real behaviour: RED

        monkeypatch.setattr(guard, "find_violations", lambda text: [])
        assert guard.main(["--paths", str(bad)]) == 0  # neutered: silently GREEN
