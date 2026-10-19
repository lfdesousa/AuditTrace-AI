"""G4 — every Python install path is hashed, wheel-only, or an exempt source build.

DEP-LOCK-AT (ADR-065), spec gate conditions C1, C4, C5. Offline: scans the
TRACKED files (``git ls-files``) the way a reviewer would grep them, except
``istio-*/`` (vendored upstream samples), ``docs/`` and ``*.md`` (prose), and
``tests/`` (these very fixtures contain the forbidden strings on purpose).

A *pip install statement* is a logical line segment (continuations joined,
split on ``&&``, ``||``, ``;``, ``|``; ``echo``/``printf`` segments and comment
lines are prose, not statements) whose verb is any of ``pip``, ``pip3``,
``python -m pip``, ``<path>/pip``, ``$PIP``/``"$PIP"``. The ONLY allowed forms:

1. HASHED:  ``--require-hashes`` AND ``--only-binary=:all:`` AND ``-r <file>``
   (no ``--upgrade``, no ``-e`` -- C1).
2. SOURCE BUILD:  ``--no-deps`` AND ``--no-build-isolation`` AND the ONLY
   positional targets are ``.`` / ``-e .`` / the ``audittrace-object-storage``
   VCS requirement (C4: ``--no-deps`` is NOT a blanket pass, so a future
   ``pip install --no-deps anything==1`` is RED).

``uv pip install|sync``, ``uvx``, ``pipx`` and ``uv tool install`` are
forbidden outright (C4(c)). The VCS commit SHA exists ONCE (pyproject.toml);
other files READ it (``${SHA}``) or repeat the same 40-hex value; any other ref
(``@v0.1.1``, a different SHA) is RED (N7, N7b).

Neuters (each applied individually, see the parametrised cases at the bottom):
N5 re-add ``pip install -e ".[dev]"`` to ci.yml; N5b add
``pip install --no-deps foo==1``; N6 re-add ``additional_dependencies`` to
pre-commit; N7 VCS pin back to ``@v0.1.1``; N7b a Dockerfile SHA differing from
pyproject.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

from tests._hermetic_env import hermetic_env
from tests.test_ci_make_targets_resolve_tools import parse_makefile

ROOT = Path(__file__).resolve().parent.parent

EXCLUDED_PREFIXES = ("istio-", "docs/", "tests/", ".git/")
LOCK_FILES = {
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-gate.txt",
    "bff/requirements.txt",
    "images/llm-stub/requirements.txt",
}

PIP_VERB = re.compile(
    r"""(?:^|[\s"'(@=])
        (?:pip3?
          |-m\s+pip3?                  # python -m pip / python3.12 -m pip / ${PY} -m pip
          |[\w./-]*/pip3?              # .venv/bin/pip, /usr/bin/pip3
          |"?\$\{?\w*PIP\w*\}?"?      # $PIP, "${PIP_BIN}", $UV_PIP ...
          |"?\$\(\w+\)"?/bin/pip3?   # $(GATE_VENV)/bin/pip (make)
        )
        \s+install\b""",
    re.X,
)
FORBIDDEN_VERB = re.compile(
    r"(?:^|[\s\"'(@])(?:uvx|pipx|uv\s+pip\s+(?:install|sync)|uv\s+sync"
    r"|uv\s+tool\s+(?:install|run)|uv\s+add|uv\s+run\b[^\n]*--with)\b"
)
VCS_OCCURRENCE = re.compile(r"audittrace-object-storage\.git@([^\s\"')]+)")
VCS_SPEC_PREFIX = "audittrace-object-storage @ git+https://github.com/lfdesousa/audittrace-object-storage.git@"


def tracked_text_files(root: Path = ROOT) -> dict[str, str]:
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    out: dict[str, str] = {}
    for raw in proc.stdout.split(b"\0"):
        rel = raw.decode()
        if not rel or rel.startswith(EXCLUDED_PREFIXES) or rel.endswith(".md"):
            continue
        if rel in LOCK_FILES:
            continue
        path = root / rel
        if not path.is_file():
            continue
        try:
            out[rel] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary
    return out


def statements(text: str) -> list[str]:
    """Logical segments: continuations joined, comments/echo dropped, split on && || ; |."""
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    out: list[str] = []
    for line in joined.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # `RUN`, make `@`, yaml `run:`/`- ` prefixes are noise for verb matching
        for seg in re.split(r"&&|\|\||;|\|", line):
            body = seg.strip().lstrip("@-").strip()
            if body.startswith(("echo ", "printf ", "#")):
                continue
            out.append(body)
    return out


_REQ_OPTS = ("-r", "--requirement", "-c", "--constraint")
_EDIT_OPTS = ("-e", "--editable")


def normalise_tokens(tokens: list[str]) -> list[str]:
    """Split ``--opt=value`` and attached short forms (``-rFILE``, ``-cFILE``,
    ``-eURL``) into separate tokens BEFORE classifying, so no spelling hides
    an option from the allowlist (review B1: ``--requirement=FILE`` and
    ``-rFILE`` next to ``.`` used to pass)."""
    out: list[str] = []
    for tok in tokens:
        if tok.startswith("--") and "=" in tok:
            opt, _, value = tok.partition("=")
            out += [opt, value]
        elif re.fullmatch(r"-[rce].+", tok):
            out += [tok[:2], tok[2:]]
        else:
            out.append(tok)
    return out


def classify(statement: str) -> str | None:
    """None if allowed (or not a pip statement); else the reason it is not."""
    if FORBIDDEN_VERB.search(statement):
        return (
            "forbidden installer (uvx / pipx / uv pip install|sync / "
            "uv tool install|run / uv add / uv run --with)"
        )
    m = PIP_VERB.search(statement)
    if not m:
        return None
    args = statement[m.end() :]
    try:
        tokens = normalise_tokens(shlex.split(args, comments=False, posix=True))
    except ValueError:
        return "unparseable pip install statement (fail closed)"
    if "--upgrade" in tokens or "-U" in tokens:
        return "pip install --upgrade (unpinned)"
    if "--require-hashes" in tokens:
        only_binary = any(
            a == "--only-binary" and b == ":all:" for a, b in zip(tokens, tokens[1:])
        )
        if not only_binary:
            return "hashed install without --only-binary=:all: (C1)"
        if not any(t in ("-r", "--requirement") for t in tokens) or any(
            t in _EDIT_OPTS for t in tokens
        ):
            return "hashed install must be `-r <lock>` and nothing editable"
        return None
    if "--no-deps" in tokens:
        if "--no-build-isolation" not in tokens:
            return "--no-deps source build without --no-build-isolation (C1)"
        if any(t in _REQ_OPTS for t in tokens):
            return "--no-deps source build must not take -r/-c requirement files (C4)"
        positional: list[str] = []
        for tok in tokens:
            if tok in _EDIT_OPTS or tok.startswith("-"):
                continue  # an editable target is the next token: kept as positional
            positional.append(tok)
        if positional and all(
            p == "." or p.startswith(VCS_SPEC_PREFIX) for p in positional
        ):
            return None
        return (
            "--no-deps is only allowed for `.` / `-e .` / the VCS requirement, "
            f"got {positional}"
        )
    return "unhashed pip install (needs --require-hashes ... -r <lock>, or an exempt --no-deps source build)"


def install_violations(files: dict[str, str]) -> list[str]:
    bad: list[str] = []
    for rel, text in sorted(files.items()):
        for stmt in statements(text):
            reason = classify(stmt)
            if reason:
                bad.append(f"{rel}: {reason}: {stmt[:140]}")
    return bad


def vcs_violations(files: dict[str, str], pyproject_text: str) -> list[str]:
    pinned = re.search(
        r"audittrace-object-storage\.git@([0-9a-f]{40})\"", pyproject_text
    )
    if not pinned:
        return [
            "pyproject.toml: audittrace-object-storage is not pinned to a 40-hex SHA"
        ]
    sha = pinned.group(1)
    bad: list[str] = []
    for rel, text in sorted(files.items()):
        for m in VCS_OCCURRENCE.finditer(text):
            value = m.group(1)
            if value == sha:
                continue
            reads_pyproject = value in ("${SHA}", "$SHA") and (
                "pyproject.toml" in text and "sed -n" in text
            )
            if not reads_pyproject:
                bad.append(f"{rel}: VCS ref {value!r} is not pyproject's {sha[:12]}…")
    return bad


def precommit_violations(config_text: str) -> list[str]:
    data = yaml.safe_load(config_text)
    bad: list[str] = []
    for repo in data["repos"]:
        url = repo["repo"]
        if "mirrors-mypy" in url or "ruff-pre-commit" in url:
            bad.append(f"pre-commit repo {url} builds its own unlocked environment")
        for hook in repo["hooks"]:
            if "additional_dependencies" in hook:
                bad.append(f"hook {hook['id']}: additional_dependencies is unhashed")
    local = {
        h["id"]: h for r in data["repos"] if r["repo"] == "local" for h in r["hooks"]
    }
    for hook_id, entry in (("ruff", "make lint-ruff"), ("mypy", "make typecheck")):
        h = local.get(hook_id)
        if h is None:
            bad.append(f"no local `{hook_id}` hook")
            continue
        want = {
            "entry": entry,
            "language": "system",
            "pass_filenames": False,
            "require_serial": True,
            "files": r"\.py$",
        }
        for key, value in want.items():
            if h.get(key) != value:
                bad.append(
                    f"hook {hook_id}: {key} is {h.get(key)!r}, want {value!r} (C5)"
                )
    return bad


def test_every_install_path_in_the_tracked_files_is_hashed_or_exempt() -> None:
    assert install_violations(tracked_text_files()) == []


def test_the_scan_actually_sees_the_install_paths() -> None:
    """Vacuity guard: the files that MUST contain a hashed install do, and the
    scan reached them (an empty scan would pass the violation test above)."""
    files = tracked_text_files()
    must = {
        "scripts/install-locked.sh": "--require-hashes",
        "Dockerfile": "-r requirements.txt",
        "Dockerfile.tests": "-r /tmp/requirements-dev.txt",
        "bff/Dockerfile": "-r requirements.txt",
        "images/llm-stub/Dockerfile": "-r /app/requirements.txt",
    }
    for rel, needle in must.items():
        stmts = [s for s in statements(files[rel]) if PIP_VERB.search(s)]
        assert any(needle in s and "--require-hashes" in s for s in stmts), rel
    for rel in (".github/workflows/ci.yml", ".github/workflows/test-count-badge.yml"):
        assert "bash scripts/install-locked.sh pip" in files[rel], rel
    assert (
        "scripts/setup.sh" in files and "install-locked.sh" in files["scripts/setup.sh"]
    )
    makefile = files["Makefile"]
    assert "install-locked.sh .venv/bin/pip" in makefile
    assert "install-locked.sh $(GATE_VENV)/bin/pip gate" in makefile


def test_the_chokepoint_uses_no_build_isolation_for_every_source_build() -> None:
    stmts = [
        s
        for s in statements(tracked_text_files()["scripts/install-locked.sh"])
        if PIP_VERB.search(s) and "--no-deps" in s
    ]
    assert len(stmts) == 2  # `-e .` and the VCS requirement
    assert all("--no-build-isolation" in s for s in stmts)


def test_the_vcs_commit_sha_exists_once_and_every_copy_agrees() -> None:
    files = tracked_text_files()
    assert vcs_violations(files, (ROOT / "pyproject.toml").read_text()) == []
    occurrences = [rel for rel, t in files.items() if VCS_OCCURRENCE.search(t)]
    assert "pyproject.toml" in occurrences


def test_precommit_hooks_are_local_system_and_unlocked_repos_are_gone() -> None:
    assert precommit_violations((ROOT / ".pre-commit-config.yaml").read_text()) == []


def test_the_ruff_hook_target_does_not_pull_in_semgrep() -> None:
    rules = parse_makefile((ROOT / "Makefile").read_text())
    deps, recipe = rules["lint-ruff"]
    assert deps == []
    assert "semgrep" not in recipe and "ruff check" in recipe
    assert "--fix" not in recipe  # check-only: the hook never rewrites staged files


# ───────────────────────── neuters N5 / N5b / N6 / N7 / N7b ─────────────────────────


@pytest.mark.parametrize(
    ("stmt", "why"),
    [
        ('pip install -e ".[dev]"', "N5 range resolution"),
        ("pip install --no-deps foo==1", "N5b --no-deps is not a blanket pass"),
        ("python -m pip install --upgrade pip", "upgrade pip"),
        (
            "pip install --require-hashes -r requirements.txt",
            "hashed but not wheels-only",
        ),
        ("pip install -r requirements.txt", "unhashed lock"),
        ("pip install --no-deps .", "source build with build isolation"),
        ('"$PIP" install requests', "variable pip, unhashed"),
        ("uv pip install foo", "uv installer"),
        ("pipx install semgrep==1.171.0", "pipx"),
        ("uvx ruff", "uvx"),
        (".venv/bin/pip3 install foo", "path pip3"),
        (
            "pip install --require-hashes --only-binary=:all: -e .",
            "hashed but editable",
        ),
    ],
)
def test_the_classifier_rejects_each_escape_hatch(stmt: str, why: str) -> None:
    assert classify(stmt), why


@pytest.mark.parametrize(
    "stmt",
    [
        "pip install --require-hashes --only-binary=:all: -r requirements.txt",
        'pip install --user --no-cache-dir --require-hashes --only-binary=:all: -r "$block"',
        "pip install --no-deps --no-build-isolation -e .",
        "pip install --no-deps --no-build-isolation --editable=.",
        "pip install --require-hashes --only-binary=:all: --requirement=requirements.txt",
        "pip install --user --no-deps --no-build-isolation .",
        f'$PIP install --no-deps --no-build-isolation "{VCS_SPEC_PREFIX}${{SHA}}"',
    ],
)
def test_the_classifier_accepts_the_sanctioned_forms(stmt: str) -> None:
    assert classify(stmt) is None


def test_statements_drops_comments_and_echo_but_splits_chained_installs() -> None:
    text = (
        '# pip install -e .\necho "pip install foo"\nRUN a && \\\n    pip install bar\n'
    )
    stmts = statements(text)
    assert not any("# pip" in s or s.startswith("echo") for s in stmts)
    assert any(s == "pip install bar" for s in stmts)


def _with(files: dict[str, str], rel: str, extra: str) -> dict[str, str]:
    mutated = dict(files)
    mutated[rel] = mutated[rel] + "\n" + extra + "\n"
    return mutated


def test_n5_readding_range_install_to_ci_yml_is_red() -> None:
    files = _with(
        tracked_text_files(),
        ".github/workflows/ci.yml",
        '          pip install -e ".[dev]"',
    )
    assert any("ci.yml" in v for v in install_violations(files))


def test_n5b_a_stray_no_deps_install_is_red() -> None:
    files = _with(
        tracked_text_files(), "scripts/setup.sh", "pip install --no-deps foo==1"
    )
    assert any("setup.sh" in v and "--no-deps" in v for v in install_violations(files))


def test_n5b_the_positional_allowlist_catches_a_correctly_flagged_stray_package() -> (
    None
):
    """Both required flags present, wrong target: only the C4 positional
    allowlist (not the flag checks) can turn this RED."""
    files = _with(
        tracked_text_files(),
        "scripts/setup.sh",
        "pip install --no-deps --no-build-isolation foo==1",
    )
    assert any(
        "only allowed for" in v and "foo==1" in v for v in install_violations(files)
    )


def test_n6_additional_dependencies_in_precommit_is_red() -> None:
    text = (ROOT / ".pre-commit-config.yaml").read_text()
    text = text.replace(
        "      - id: mypy\n",
        "      - id: mypy\n        additional_dependencies: [fastapi]\n",
        1,
    )
    assert any("additional_dependencies" in v for v in precommit_violations(text))


def test_n6b_a_mirrors_mypy_repo_is_red() -> None:
    text = (ROOT / ".pre-commit-config.yaml").read_text()
    text += (
        "  - repo: https://github.com/pre-commit/mirrors-mypy\n"
        "    rev: v2.3.1\n    hooks:\n      - id: mypy2\n"
    )
    assert any("mirrors-mypy" in v for v in precommit_violations(text))


def test_n6c_a_precommit_hook_that_passes_filenames_is_red() -> None:
    text = (ROOT / ".pre-commit-config.yaml").read_text()
    head, _, tail = text.partition("      - id: mypy\n")
    tail = tail.replace("        pass_filenames: false\n", "", 1)
    assert any(
        "pass_filenames" in v
        for v in precommit_violations(head + "      - id: mypy\n" + tail)
    )


def test_n7_vcs_pin_back_to_a_tag_is_red() -> None:
    files = tracked_text_files()
    pyproject = (ROOT / "pyproject.toml").read_text()
    sha = re.search(r"\.git@([0-9a-f]{40})\"", pyproject).group(1)  # type: ignore[union-attr]
    files = _with(
        files, "scripts/setup.sh", "git+https://x/audittrace-object-storage.git@v0.1.1"
    )
    assert any("'v0.1.1'" in v for v in vcs_violations(files, pyproject))
    bad_pyproject = pyproject.replace(sha, "v0.1.1")
    assert vcs_violations(files, bad_pyproject)


def test_n7b_a_dockerfile_sha_differing_from_pyproject_is_red() -> None:
    files = tracked_text_files()
    other = "0" * 40
    mutated = _with(
        files,
        "Dockerfile",
        f"RUN pip install git+https://x/audittrace-object-storage.git@{other}",
    )
    assert any(
        "Dockerfile" in v
        for v in vcs_violations(mutated, (ROOT / "pyproject.toml").read_text())
    )


def test_the_sha_variable_is_only_trusted_where_pyproject_is_read() -> None:
    files = {"x.sh": "pip install audittrace-object-storage.git@${SHA}"}
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert vcs_violations(files, pyproject)  # no `sed -n` + pyproject.toml in the file


# ───────────────────── security-lint resolves ONLY the gate venv (C7) ─────────────────────


def test_security_lint_fails_closed_without_the_gate_venv(tmp_path: Path) -> None:
    """A semgrep on PATH (pipx, a stray pip) is unhashed and NOT accepted: with
    no gate venv the target exits non-zero and says how to build it, even when
    a `semgrep` is first on PATH."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sentinel = tmp_path / "path-semgrep-ran"
    (fake_bin / "semgrep").write_text(f'#!/bin/sh\ntouch "{sentinel}"\n')
    (fake_bin / "semgrep").chmod(0o755)
    env = hermetic_env({"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp_path)})
    proc = subprocess.run(
        ["make", "security-lint", f"GATE_VENV={tmp_path / 'no-such-venv'}"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "make gate-venv" in proc.stdout + proc.stderr
    assert not sentinel.exists()


def test_security_lint_uses_the_gate_venv_semgrep(tmp_path: Path) -> None:
    venv_bin = tmp_path / "gate" / "bin"
    venv_bin.mkdir(parents=True)
    log = tmp_path / "argv.log"
    (venv_bin / "semgrep").write_text(f'#!/bin/sh\necho "$@" > "{log}"\n')
    (venv_bin / "semgrep").chmod(0o755)
    proc = subprocess.run(
        ["make", "security-lint", f"GATE_VENV={tmp_path / 'gate'}"],
        cwd=ROOT,
        env=hermetic_env({"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "--config ci/semgrep/rules.yml" in log.read_text()


def test_the_gate_venv_is_never_put_on_the_ci_path() -> None:
    """GITHUB_PATH PREPENDS: the gate venv's python/pip would shadow the runner
    python that G5 checks against the dev lock (found in the CI-shaped AC-2 run:
    run-tests.sh resolved `python` to the gate venv and refused the suite)."""
    for rel, text in tracked_text_files().items():
        if rel.startswith(".github/workflows/"):
            assert "gate-venv" not in "".join(
                ln for ln in text.splitlines() if "GITHUB_PATH" in ln
            ), rel
