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

import functools
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests._g4_install_scan import (
    VCS_SPEC_PREFIX,
    classify,
    install_violations,
    is_pip_install,
    statements,
)
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

VCS_OCCURRENCE = re.compile(r"audittrace-object-storage\.git@([^\s\"')]+)")


@functools.cache
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
        stmts = [s for s in statements(files[rel]) if is_pip_install(s)]
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
        if is_pip_install(s) and "--no-deps" in s
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


U = "requirements-unpinned.txt"
OK = "--no-deps --no-build-isolation"
EVIL = "git+https://evil.example/x.git"

# One rejecting case for EVERY spelling in the r1 verdict and in the r2
# reviewer probe (b1_probe.out / b1_pip_semantics-pip2621.out), plus the
# original neuters. A case that stops being refused turns this RED.
REJECTED = [
    # original neuters
    ('pip install -e ".[dev]"', "N5 range resolution"),
    ("pip install --no-deps foo==1", "N5b --no-deps is not a blanket pass"),
    ("python -m pip install --upgrade pip", "upgrade pip"),
    ("pip install --require-hashes -r requirements.txt", "hashed, not wheels-only"),
    ("pip install -r requirements.txt", "unhashed lock"),
    ("pip install --no-deps .", "source build with build isolation"),
    ('"$PIP" install requests', "variable pip, unhashed"),
    (".venv/bin/pip3 install foo", "path pip3"),
    (
        "pip install --require-hashes --only-binary=:all: -e .",
        "hashed but editable",
    ),
    # r1 (B1)
    (f"pip install {OK} --requirement={U} .", "r1 --requirement=FILE"),
    (f"pip install {OK} -r{U} .", "r1 attached -rFILE"),
    (f"pip install {OK} --editable={EVIL} .", "r1 --editable=URL"),
    ('"${PIP_BIN}" install fastapi', "r1 braced variable pip"),
    ("uv run --with fastapi python x.py", "r1 uv run --with"),
    # r2 brief list
    (f"pip install {OK} --requirement {U} .", "--requirement FILE"),
    (f"pip install {OK} -r={U} .", "-r=FILE"),
    (f"pip install {OK} '--requirement={U}' .", "quoted --requirement="),
    ("python -m pip install fastapi", "python -m pip"),
    ("pip3 install fastapi", "pip3"),
    ("uv pip install fastapi", "uv pip install"),
    ("pipx install semgrep", "pipx"),
    (f"pip install {OK} --constraint={U} .", "--constraint="),
    # r2 reviewer-new (N1..N38)
    (f"pip install {OK} -qr {U} .", "N1 clustered -qr FILE"),
    (f"pip install {OK} -qr{U} .", "N2 clustered -qrFILE"),
    (f"pip install {OK} -ve {EVIL} .", "N3 clustered -ve URL"),
    ("python3 -mpip install fastapi", "N4 -mpip"),
    ("pip3.12 install fastapi", "N5 pip3.12"),
    (".venv/bin/pip3.12 install fastapi", "N6 path pip3.12"),
    ("pip --no-cache-dir install fastapi", "N7 global option before verb"),
    ("pip -q install fastapi", "N8 short global option before verb"),
    ("python -m pip -q install fastapi", "N9 -m pip with global option"),
    ("$(PIP) install fastapi", "N10 make $(PIP)"),
    ("$VENV/bin/pip install fastapi", "N11 $VENV/bin/pip"),
    ("${VENV}/bin/pip install fastapi", "N12 ${VENV}/bin/pip"),
    ("X=`pip install fastapi`", "N13 backticks"),
    ('echo "$(pip install fastapi)"', "N14 command substitution"),
    ('subprocess.run(["pip", "install", "fastapi"], check=True)', "N15 py list"),
    (
        'subprocess.run([sys.executable, "-m", "pip", "install", "fastapi"])',
        "N16 py list -m pip",
    ),
    ('RUN ["pip", "install", "fastapi"]', "N17 Dockerfile exec form"),
    ('RUN ["python", "-m", "pip", "install", "fastapi"]', "N38 exec form -m pip"),
    ("uv --quiet pip install fastapi", "N18 uv global option, pip install"),
    ("uv --offline run --with fastapi x.py", "N19 uv global option, run --with"),
    ("uv -q tool install ruff", "N20 uv global option, tool install"),
    (f"PIP_REQUIREMENT={U} pip install {OK} .", "N21 PIP_REQUIREMENT"),
    (f"PIP_EDITABLE={EVIL} pip install {OK} .", "N22 PIP_EDITABLE"),
    (f"PIP_CONSTRAINT={U} pip install {OK} .", "PIP_CONSTRAINT"),
    (
        "PIP_CONFIG_FILE=/tmp/pip.conf pip install --require-hashes -r x",
        "PIP_CONFIG_FILE",
    ),
    (f"pip install {OK} --index-url=https://evil.example/simple .", "N23 index-url"),
    (f"pip install {OK} --find-links=./w .", "N24 find-links"),
    (
        "pip install --require-hashes --only-binary=:all: -r requirements.txt fastapi",
        "N25 hashed plus an extra positional",
    ),
    ("sudo pip install fastapi", "N29 sudo"),
    ("uv\tpip\tinstall fastapi", "N30 tabs"),
    ("pip install --no-index --find-links w fastapi", "N31 offline find-links"),
    ("python -m uv pip install fastapi", "N32 python -m uv"),
    ("uv tool run --from x y", "N33 uv tool run"),
    (
        "pip install --require-hashes --only-binary=:all: -r requirements.txt -qe git+https://x",
        "N34 hashed plus clustered -qe",
    ),
    ("uvx ruff", "uvx"),
    ("pip install 'unterminated", "unparseable (fail closed)"),
    (
        "pip install --global-opt=x --require-hashes --only-binary=:all: -r l",
        "unlisted option",
    ),
    (
        "pip --log /tmp/l install --require-hashes --only-binary=:all: -r l",
        "global option value",
    ),
    (
        "pip install --require-hashes --only-binary=:all: -r l --pre",
        "unlisted flag --pre",
    ),
    (
        "pip install --require-hashes --only-binary=:all: -r l --no-index",
        "unlisted flag --no-index",
    ),
    (
        "pip install --require-hashes --only-binary=:all: -r l --force-reinstall",
        "unlisted flag",
    ),
    (f"pip install {OK} --pre .", "unlisted flag on a source build"),
    (
        f"pip install {OK} --break-system-packages .",
        "unlisted flag on a source build 2",
    ),
    ("pip install --upgrade --require-hashes --only-binary=:all: -r l", "--upgrade"),
    ("pip install -U --require-hashes --only-binary=:all: -r l", "-U"),
    ("pip install --require-hashes --only-binary=:all: -r", "-r without a value"),
    ("uv add fastapi", "uv add"),
    ("uv sync", "uv sync"),
]


@pytest.mark.parametrize(("stmt", "why"), REJECTED, ids=[w for _, w in REJECTED])
def test_the_classifier_rejects_each_escape_hatch(stmt: str, why: str) -> None:
    assert classify(stmt), why


# The statements the classifier is given in the real scan path come out of
# `statements()`/`scan_units()`: forms that only exist at FILE level.
FILE_LEVEL_REJECTED = [
    (
        "x.yml",
        "jobs:\n  t:\n    steps:\n      - run: >\n          pip install --no-deps --no-build-isolation .\n          fastapi\n",
        "N35 YAML folded scalar folds two lines into one command",
    ),
    (
        "x.yml",
        "jobs:\n  t:\n    steps:\n      - run: |\n          pip -q install fastapi\n",
        "YAML literal block",
    ),
    (
        "x.yml",
        "env:\n  PIP_REQUIREMENT: requirements-extra.txt\n",
        "PIP_REQUIREMENT as a YAML env key",
    ),
    ("Dockerfile", "RUN <<EOT\npip install fastapi\nEOT\n", "N36 heredoc"),
    (
        "scripts/x.sh",
        "pip install --no-deps --no-build-isolation . \\\r\n  fastapi\n",
        "N37 CRLF continuation hides a second package",
    ),
    (
        "Dockerfile",
        'RUN ["python", "-m", "pip", "install", "fastapi"]\n',
        "N38 exec form",
    ),
    (
        "scripts/x.py",
        'import subprocess\nsubprocess.run(["pip", "install", "fastapi"])\n',
        "N15 py list",
    ),
    (
        "scripts/x.py",
        'import subprocess, sys\nsubprocess.run([sys.executable, "-m", "pip",\n    "install", "fastapi"])\n',
        "N16 py list spanning lines",
    ),
    (
        "scripts/x.py",
        'import os\nos.system("pip -q install fastapi")\n',
        "py string command",
    ),
    ("scripts/x.sh", "X=`pip install fastapi`\n", "N13 backticks"),
    (
        "scripts/x.sh",
        'echo "$(pip install fastapi)"\n',
        "N14 echo that executes a substitution",
    ),
    ("Makefile", "install:\n\t@$(PIP) install fastapi\n", "N10 Makefile $(PIP)"),
    (
        "Makefile",
        "install:\n\t$(PYTHON) -m pip -q install fastapi\n",
        "Makefile -m pip with option",
    ),
    (
        "scripts/x.sh",
        "PIP_REQUIREMENT=requirements-extra.txt pip install --no-deps --no-build-isolation .\n",
        "N21 env prefix",
    ),
    (
        "scripts/x.sh",
        "export PIP_CONFIG_FILE=/tmp/pip.conf\n",
        "PIP_CONFIG_FILE export",
    ),
]


@pytest.mark.parametrize(
    ("rel", "text", "why"),
    FILE_LEVEL_REJECTED,
    ids=[w for *_, w in FILE_LEVEL_REJECTED],
)
def test_the_file_scan_rejects_each_file_level_escape_hatch(
    rel: str, text: str, why: str
) -> None:
    assert install_violations({rel: text}), why


def test_a_python_markdown_hint_is_prose_not_a_command() -> None:
    """The backtick-span removal is scoped to `.py` literals (Python has no
    backtick substitution): the same text in a shell file is a command."""
    hint = 'msg = "Reinstall with `pip install pyhanko`."\n'
    assert install_violations({"src/x.py": hint}) == []
    assert install_violations(
        {"scripts/x.sh": "echo Reinstall with `pip install pyhanko`\n"}
    )


def test_a_python_file_that_does_not_parse_is_scanned_by_line() -> None:
    assert install_violations(
        {"scripts/broken.py": "def f(:\n    pip install fastapi\n"}
    )


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
        # the repo's own real shapes
        'RUN pip install --user --no-cache-dir --no-deps --no-build-isolation "'
        + VCS_SPEC_PREFIX
        + '${SHA}"',
        '$PIP install --require-hashes --only-binary=:all: -r "$work/uv.txt"',
        "$UV pip compile requirements.in --generate-hashes",
        "python -m pip install --require-hashes --only-binary=:all: -r requirements.txt",
        "pip --disable-pip-version-check install --require-hashes --only-binary=:all: -qr l",
        "pip3.12 install --require-hashes --only-binary=:all: -r requirements.txt",
        "uv --version",
        "pip freeze",
        "pip download -r requirements.txt",
        "bash scripts/install-locked.sh pip uv-only",
    ],
)
def test_the_classifier_accepts_the_sanctioned_forms(stmt: str) -> None:
    assert classify(stmt) is None, stmt


def test_b1r_a_requirements_file_that_spells_an_allowed_target_is_refused() -> None:
    """B1r is LOAD-BEARING (r2: 36/84 spellings differ without it). Under
    `--no-deps` the value of `-r` is a requirements FILE pip reads and installs
    unhashed; `.` and the VCS requirement are allowed *positional* targets, so
    only the explicit `-r` refusal stops a FILE path spelled like one of them."""
    spelled_vcs = f"{VCS_SPEC_PREFIX}cc21c553" + "0" * 32
    for stmt in (
        f"pip install {OK} -r .",
        f"pip install {OK} --requirement .",
        f"pip install {OK} --requirement=.",
        f'pip install {OK} -r "{spelled_vcs}" .',
        f'pip install {OK} -r "{spelled_vcs}"',
        f'pip install {OK} --requirement="{spelled_vcs}"',
        f"pip install {OK} -qr .",
    ):
        reason = classify(stmt)
        assert reason and "-r" in reason, stmt


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


@pytest.mark.parametrize(
    ("rel", "line"),
    [
        (".github/workflows/ci.yml", "          pip --no-cache-dir install fastapi"),
        ("Makefile", "\t$(PIP) install fastapi"),
        ("Dockerfile", 'RUN ["pip", "install", "fastapi"]'),
        ("scripts/setup.sh", "pip3.12 install fastapi"),
        ("scripts/setup.sh", "$VENV/bin/pip install fastapi"),
        ("Makefile", "\t$(PYTHON) -m pip -q install fastapi"),
        (
            "scripts/setup.sh",
            "pip install --no-deps --no-build-isolation -qrrequirements-extra.txt .",
        ),
        (
            "scripts/setup.sh",
            "PIP_REQUIREMENT=requirements-extra.txt pip install "
            "--no-deps --no-build-isolation .",
        ),
        (".github/workflows/ci.yml", "          python -mpip install fastapi"),
    ],
)
def test_r2_m4_every_spelling_appended_to_a_real_tracked_file_is_red(
    rel: str, line: str
) -> None:
    """The r2 reviewer's real-file neuters M4a-i: each spelling appended to the
    REAL tracked file went undetected. The same append now turns the real scan RED
    (and the unmodified tree stays GREEN, test above)."""
    files = _with(tracked_text_files(), rel, line)
    assert any(v.startswith(f"{rel}:") for v in install_violations(files)), line


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
