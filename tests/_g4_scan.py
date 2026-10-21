"""G4-CP: an allow-listed chokepoint that denies by TOKEN (DEP-LOCK-AT ADDENDUM-1/-2).

G4 is a DRIFT detector, not a defence against a determined adversary (operator ruling
2026-10-21, option A). It never parses what a line DOES. It establishes that no
non-comment logical line in the scanned set carries an installer token except the
allow-listed lines (``tests/_g4_sanctioned.py``), and that the two chokepoint scripts
are byte-identical to their pins. The server (pip in ``--require-hashes`` mode) decides
what a sanctioned line installs; the token rule decides only which lines may exist.

Scope (S1): ``git ls-files --cached --others --exclude-standard`` (so UNTRACKED files are
scanned) minus ``istio-*/``, ``docs/``, ``tests/``, ``*.md`` and the five locks. Files are
read as BYTES; nothing is dropped silently (S1'): an undecodable file must be in the
binary list, an archive must be pinned by path and sha256.

Units (S2'): physical lines with backslash-newline AND the next line's leading blanks
deleted; every hit is mapped back to its physical line for the comment rule. A comment
(a COMMENT token in ``.py``, a line whose first non-blank characters are ``#`` or ``//``
elsewhere) is exempt, except a ``#!`` shebang line, which never is (S2'').

Declared residuals (ADR-065): adversarial obfuscation, token-free execution paths,
unscanned paths, conftest-time installs, fetched code. The installer-name list below is
an ENUMERATION; an installer outside it is a residual.
"""

from __future__ import annotations

import hashlib
import io
import re
import subprocess
import tokenize
import tomllib
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from tests._g4_sanctioned import ARCHIVES, BINARIES, HASH_PINNED, SANCTIONED

ROOT = Path(__file__).resolve().parent.parent

EXCLUDED_PREFIXES = ("istio-", "docs/", "tests/", ".git/")
LOCK_FILES = frozenset(
    {
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-gate.txt",
        "bff/requirements.txt",
        "images/llm-stub/requirements.txt",
    }
)

# ----------------------------------------------------------------------------- the grammar
PIP_RX = r"pip(?![ei])"  # pip, pip3.12, -mpip, -Impip, pipx, PIP_*, pip.conf, pip._internal; not pipe/pipeline/ensurepip-as-word
UV_RX = r"(?<![A-Za-z0-9])uvx?(?![A-Za-z0-9])"  # uv, uvx, UV_*, .venv/bin/uv; not uvicorn/uvloop
# An ENUMERATION (ADR-065): installers outside this list are a declared residual.
# `hatch` is deliberately absent: "escape hatch" appears in ten files.
ADVISORY_RX = (
    r"\b(?:pipenv|poetry|conda|easy_install|nox|tox)\b|setup\.py\s+install"
    r"|pyproject-build|--upgrade-deps"
    r"|\b(?:pdm|rye|pixi|micromamba|mamba)\b"
    # r4 (B1-r4a): the build front-end in list/exec spelling (`"-m", "build"`), and the
    # installs our own locked tools perform without a pip/uv token (mypy, virtualenv).
    r"""|-m["',\s]*build\b"""
    r"|--install-types"
    r"|\bvirtualenv\b[^\n]*--(?:download|upgrade-embed-wheels)"
)
LOCK_RX = r"requirements[\w.\-]*\.(?:txt|in)"  # a lock name is a token: COPY extras.txt requirements.txt is RED
TOKEN_RX = re.compile(
    f"(?P<pip>{PIP_RX})|(?P<uv>{UV_RX})|(?P<adv>{ADVISORY_RX})|(?P<lock>{LOCK_RX})",
    re.IGNORECASE,
)

INSTALLER_CONFIG_BASENAMES = frozenset(
    {
        "pip.conf",
        "pip.ini",
        "uv.toml",
        "uv.lock",
        "tox.ini",
        "noxfile.py",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        "environment.yml",
        "environment.yaml",
        "setup.py",
        "setup.cfg",
    }
)
PIN_LINE_RX = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.\-]*(\[[^\]]*\])?==[A-Za-z0-9_.+!*-]+$"
)
ARCHIVE_RX = re.compile(r"\.(?:tar(?:\.\w+)?|tgz|zip|whl)$")
DOCKERFILE_RX = re.compile(r"(?:^|/)(?:Dockerfile[^/]*|[^/]+\.Dockerfile)$")
ESCAPE_DIRECTIVE_RX = re.compile(r"^\s*#\s*escape\s*=", re.IGNORECASE)
BUILD_PIN_RX = re.compile(r"[\w.\-]+==[\w.+!*-]+")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ----------------------------------------------------------------------------- the file set
def scan_files(root: Path = ROOT) -> dict[str, bytes]:
    """Bytes of every in-scope file, tracked or untracked-but-not-ignored."""
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    out: dict[str, bytes] = {}
    for raw in proc.stdout.split(b"\0"):
        rel = raw.decode()
        if not rel or rel.startswith(EXCLUDED_PREFIXES) or rel.endswith(".md"):
            continue
        if rel in LOCK_FILES:
            continue
        path = root / rel
        if path.is_file():
            out[rel] = path.read_bytes()
    return out


# ----------------------------------------------------------------------------- units
def join_units(text: str) -> list[tuple[str, list[tuple[int, int]]]]:
    """Units with a per-character (line, col) map back to the physical text."""
    lines = text.split("\n")
    units: list[tuple[str, list[tuple[int, int]]]] = []
    i = 0
    while i < len(lines):
        buf: list[str] = []
        pos: list[tuple[int, int]] = []
        pad = 0
        while True:
            raw = lines[i].rstrip("\r")
            cont = raw.endswith("\\") and i + 1 < len(lines)
            body = raw[:-1] if cont else raw
            for col, ch in enumerate(body[pad:], start=pad):
                buf.append(ch)
                pos.append((i + 1, col))
            if not cont:
                break
            i += 1
            nxt = lines[i]
            pad = len(nxt) - len(nxt.lstrip(" \t"))
        units.append(("".join(buf), pos))
        i += 1
    return units


def comment_spans(text: str) -> dict[int, list[tuple[int, int]]] | None:
    """row -> [(start col, end col)] of every COMMENT token; None if it does not tokenize.

    Only COMMENT spans are needed (a string or code hit is never exempt), which also keeps
    this independent of the interpreter's f-string token positions.
    """
    spans: dict[int, list[tuple[int, int]]] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                spans.setdefault(tok.start[0], []).append((tok.start[1], tok.end[1]))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return None
    return spans


def yaml_scalar_lines(text: str) -> list[str]:
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        return []
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            out.extend(node.split("\n"))
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for doc in docs:
        walk(doc)
    return out


def pin_exempt(rel: str, unit: str) -> bool:
    """An exact ``name==version`` line in pyproject.toml or a ``*.in`` is G3's domain."""
    if rel == "pyproject.toml" or rel.endswith(".in"):
        return bool(PIN_LINE_RX.match(unit.strip().strip(",").strip('"')))
    return False


def _exempt(
    rel: str,
    comments: dict[int, list[tuple[int, int]]] | None,
    lines: list[str],
    line: int,
    col: int,
) -> bool:
    stripped = lines[line - 1].lstrip()
    if stripped.startswith("#!"):
        # S2'': the kernel reads a shebang, whatever the tokenizer calls it
        return False
    if rel.endswith(".py") and comments is not None:
        return any(c0 <= col < c1 for c0, c1 in comments.get(line, ()))
    return stripped.startswith(("#", "//"))


# ----------------------------------------------------------------------------- the scan
def _decode(
    files: Mapping[str, bytes],
    bad: list[str],
    binaries: frozenset[str],
    archives: Mapping[str, str],
) -> dict[str, str]:
    texts: dict[str, str] = {}
    for rel, data in sorted(files.items()):
        base = rel.rsplit("/", 1)[-1]
        if base in INSTALLER_CONFIG_BASENAMES or rel.endswith(".ipynb"):
            bad.append(
                f"{rel}: installer configuration file is not allowed in the tree"
            )
        if ARCHIVE_RX.search(rel):
            if archives.get(rel) != sha256(data):
                bad.append(f"{rel}: archive is not pinned by path and sha256")
            continue
        try:
            texts[rel] = data.decode("utf-8")
        except UnicodeDecodeError:
            if rel not in binaries:
                bad.append(f"{rel}: undecodable file is not in the binary allow list")
    return texts


def _build_system(texts: Mapping[str, str], bad: list[str]) -> None:
    """S5: the source build of `.` runs no in-tree backend."""
    if "pyproject.toml" not in texts:
        return
    bs = tomllib.loads(texts["pyproject.toml"]).get("build-system", {})
    pins_ok = all(BUILD_PIN_RX.fullmatch(r) for r in bs.get("requires", []))
    if (
        bs.get("build-backend") != "setuptools.build_meta"
        or "backend-path" in bs
        or not pins_ok
    ):
        bad.append(
            "pyproject.toml: [build-system] is not the sanctioned exact-pin setuptools block"
        )


def _scan_text(
    rel: str,
    text: str,
    sanctioned: frozenset[tuple[str, str]],
    bad: list[str],
    unit_count: Counter[tuple[str, str]],
) -> None:
    lines = text.split("\n")
    if DOCKERFILE_RX.search(rel):
        for n, line in enumerate(lines, 1):
            if ESCAPE_DIRECTIVE_RX.match(line):
                bad.append(
                    f"{rel}:{n}: Dockerfile escape directive moves the continuation character"
                )
    comments = comment_spans(text) if rel.endswith(".py") else None
    seen: set[str] = set()
    for unit, pos in join_units(text):
        unit_count[(rel, unit)] += 1
        for match in TOKEN_RX.finditer(unit):
            line, col = pos[match.start()]
            if _exempt(rel, comments, lines, line, col) or pin_exempt(rel, unit):
                continue
            if unit in seen:
                break
            seen.add(unit)
            if (rel, unit) in sanctioned:
                break
            bad.append(
                f"{rel}:{line}: installer token {match.group(0)!r} outside the allow list: {unit.strip()[:100]}"
            )
            break
    if rel.endswith((".yml", ".yaml")):
        noncomment = [ln for ln in lines if not ln.lstrip().startswith("#")]
        for scalar in yaml_scalar_lines(text):
            if not TOKEN_RX.search(scalar) or (rel, scalar) in sanctioned:
                continue
            if any(scalar in physical for physical in noncomment):
                continue
            bad.append(
                f"{rel}:0: installer token in a YAML fold unit outside the allow list: {scalar.strip()[:100]}"
            )


def _hygiene(
    files: Mapping[str, bytes],
    sanctioned: frozenset[tuple[str, str]],
    pinned: Mapping[str, str],
    unit_count: Counter[tuple[str, str]],
    bad: list[str],
) -> None:
    """S4': an allow list cannot rot into a no-op."""
    for rel, unit in sorted(sanctioned):
        n = unit_count[(rel, unit)]
        if n == 0:
            bad.append(
                f"{rel}: stale SANCTIONED entry (no unit equals it): {unit.strip()[:80]}"
            )
        if n > 1:
            bad.append(
                f"{rel}: SANCTIONED entry occurs {n} times (duplicate sanctioned line)"
            )
        if not TOKEN_RX.search(unit):
            bad.append(
                f"{rel}: SANCTIONED entry carries no installer token (vacuous entry): {unit.strip()[:80]}"
            )
    for rel in sorted(pinned):
        if rel not in files:
            bad.append(f"{rel}: HASH_PINNED file is missing")


def violations(
    files: Mapping[str, bytes],
    *,
    sanctioned: frozenset[tuple[str, str]] = SANCTIONED,
    pinned: Mapping[str, str] = HASH_PINNED,
    binaries: frozenset[str] = BINARIES,
    archives: Mapping[str, str] = ARCHIVES,
) -> list[str]:
    """Every reason the scanned set is RED (empty list == GREEN). Pure: no I/O."""
    bad: list[str] = []
    texts = _decode(files, bad, binaries, archives)
    _build_system(texts, bad)
    unit_count: Counter[tuple[str, str]] = Counter()
    for rel, text in sorted(texts.items()):
        if rel in pinned:
            if sha256(files[rel]) != pinned[rel]:
                bad.append(f"{rel}: chokepoint file differs from its pinned sha256")
            continue
        _scan_text(rel, text, sanctioned, bad, unit_count)
    _hygiene(files, sanctioned, pinned, unit_count, bad)
    return bad


# ----------------------------------------------------------------------------- edits (for tests)
def apply_edit(files: Mapping[str, bytes], edit: tuple[Any, ...]) -> dict[str, bytes]:
    """Apply one in-memory edit: ('append', rel, text) | ('replace', rel, old, new) |
    ('bytes', rel, prefix, suffix)."""
    out = dict(files)
    kind, rel = edit[0], edit[1]
    if kind == "bytes":
        out[rel] = edit[2] + out.get(rel, b"") + edit[3]
        return out
    cur = out.get(rel, b"").decode("utf-8")
    if kind == "append":
        cur += ("" if cur.endswith("\n") or not cur else "\n") + edit[2] + "\n"
    elif kind == "replace":
        if cur.count(edit[2]) != 1:
            raise ValueError(
                f"{rel}: pattern must occur exactly once, found {cur.count(edit[2])}"
            )
        cur = cur.replace(edit[2], edit[3])
    else:
        raise ValueError(f"unknown edit kind {kind!r}")
    out[rel] = cur.encode("utf-8")
    return out
