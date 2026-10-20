"""G4 scanner: token-based, fail-closed detection of unhashed Python installs.

DEP-LOCK-AT (ADR-065), spec gate C1/C4/C5; review r1 B1 and r2 B1-r2/B1-r2v.
The scanner lives here so ``tests/test_dep_lock_install_paths.py`` stays a test
module. Round-1 matched the spelling ``pip\\s+install``; every new spelling
(global option before the verb, ``-mpip``, ``pip3.12``, ``$VENV/bin/pip``, list
and exec forms, clustered short options, ``PIP_REQUIREMENT``) walked past it.
The class is now closed from the other side:

* DETECT from tokens, over two tokenisations (a loose one that also splits
  list/exec/backtick/``$(...)`` forms, and ``shlex``): a command word is *pip*
  when its basename matches ``pip[0-9.]*`` or it is a variable naming PIP, and
  it is an install when the token ``install`` follows it anywhere on the
  statement (pip's global options sit in between and are skipped by
  construction).
* CLASSIFY by exclusion (rule of last resort): a detected pip install is GREEN
  only if it is EXACTLY one of the two sanctioned forms below, parsed from
  ``shlex`` tokens against a whitelist of options. Anything else (an unknown
  option, an extra positional, a list/exec form that ``shlex`` cannot read as
  one plain command, an unparseable line) is RED.

Sanctioned forms:

1. HASHED:  ``--require-hashes`` AND ``--only-binary=:all:`` AND ``-r <file>``,
   nothing editable, no other positional.
2. SOURCE BUILD:  ``--no-deps`` AND ``--no-build-isolation``, no ``-r`` file,
   and the ONLY targets are ``.`` / ``-e .`` / the ``audittrace-object-storage``
   VCS requirement.

``uvx``, ``pipx``, and ``uv`` with ``install|sync|add|tool`` or ``run --with``
are forbidden outright (``uv pip compile`` is the lock compiler and stays).
``PIP_REQUIREMENT``, ``PIP_CONSTRAINT``, ``PIP_EDITABLE`` and
``PIP_CONFIG_FILE`` assignments are refused in any scanned file.

DECLARED EXCLUSIONS (ADR-065 G4 scope): ``conda install``, ``easy_install`` and
``python setup.py install`` are not in the S2(c) verb list and are advisory
only; an installer reached through an arbitrary variable name (not containing
``PIP``/``UV``) or through ``eval``/encoding is out of reach of a static scan.
"""

from __future__ import annotations

import ast
import re
import shlex
from typing import Any

import yaml

VCS_SPEC_PREFIX = "audittrace-object-storage @ git+https://github.com/lfdesousa/audittrace-object-storage.git@"

PIP_ENV_ASSIGN = re.compile(r"\bPIP_(?:REQUIREMENT|CONSTRAINT|EDITABLE|CONFIG_FILE)\b")

_PIP_BASENAME = re.compile(r"pip[0-9.]*(?:\.exe)?")
_PIP_VAR = re.compile(r"\$\w*PIP\w*")
_UV_BASENAME = re.compile(r"uv(?:\.exe)?")
_UV_VAR = re.compile(r"\$\w*UV\w*")
_UV_FORBIDDEN_WORDS = frozenset({"install", "sync", "add", "tool"})

# Options a sanctioned pip install may carry. Any other token (an unknown option
# or a stray target) lands in ``positional`` and the exact-target rules refuse it.
_FLAGS_OK = frozenset(
    {
        "--user",
        "--no-cache-dir",
        "--require-hashes",
        "--no-deps",
        "--no-build-isolation",
        "--disable-pip-version-check",
        "--no-warn-script-location",
        "-q",
        "--quiet",
        "-v",
        "--verbose",
    }
)
_SHORT_WITH_VALUE = "rceiftC"


def _canon(tok: str) -> str:
    tok = re.sub(r"\$\((\w+)\)", r"$\1", tok)
    tok = re.sub(r"\$\{(\w+)\}", r"$\1", tok)
    return tok.strip("\"'`[](){},;")


def _basename(tok: str) -> str:
    return _canon(tok).rsplit("/", 1)[-1]


def is_pip(tok: str) -> bool:
    """A command word that runs pip: ``pip``, ``pip3.12``, ``<path>/pip``,
    ``-mpip`` (attached module form), ``$PIP``/``${PIP_BIN}``/``$(PIP)``/``$VENV_PIP``."""
    t = _canon(tok)
    if t.startswith("-m") and not t.startswith("--"):
        t = t[2:]
    base = t.rsplit("/", 1)[-1]
    return bool(_PIP_BASENAME.fullmatch(base) or _PIP_VAR.fullmatch(base))


def _is_uv(tok: str) -> bool:
    base = _basename(tok)
    return bool(_UV_BASENAME.fullmatch(base) or _UV_VAR.fullmatch(base))


def _loose_view(stmt: str) -> list[str]:
    s = re.sub(r"\$\((\w+)\)", r"$\1", stmt)
    s = re.sub(r"\$\{(\w+)\}", r"$\1", s)
    return [t for t in re.split(r"[\s,\[\](){};|&\"'`]+", s) if t]


def _shlex_view(stmt: str) -> list[str] | None:
    try:
        return shlex.split(stmt, comments=False, posix=True)
    except ValueError:
        return None


def _has_pip_install(view: list[str]) -> bool:
    return any(is_pip(t) and "install" in view[i + 1 :] for i, t in enumerate(view))


def forbidden_installer(view: list[str]) -> str | None:
    for i, tok in enumerate(view):
        if _basename(tok) in ("uvx", "pipx"):
            return "forbidden installer (uvx / pipx)"
        if _is_uv(tok):
            rest = view[i + 1 :]
            if _UV_FORBIDDEN_WORDS & set(rest):
                return "forbidden installer (uv install|sync|add|tool|pip install|pip sync)"
            if "run" in rest and any(w.startswith("--with") for w in rest):
                return "forbidden installer (uv run --with)"
    return None


def _expand(args: list[str]) -> list[str]:
    """Split ``--opt=value`` and expand clustered short options (``-qrFILE`` ->
    ``-q -r FILE``) BEFORE classifying, so no spelling hides an option."""
    out: list[str] = []
    for tok in args:
        if tok.startswith("--"):
            if "=" in tok:
                opt, _, value = tok.partition("=")
                out += [opt, value]
            else:
                out.append(tok)
        elif re.fullmatch(r"-[A-Za-z].*", tok):
            i = 1
            while i < len(tok):
                c = tok[i]
                out.append("-" + c)
                if c in _SHORT_WITH_VALUE:
                    if tok[i + 1 :]:
                        out.append(tok[i + 1 :])
                    break
                i += 1
        else:
            out.append(tok)
    return out


def _classify_pip_install(tokens: list[str]) -> str | None:
    # The pip token that carries the verb. Any OTHER pip-looking token is an
    # argument and meets the positional / option rules below.
    carriers = [
        i for i, t in enumerate(tokens) if is_pip(t) and "install" in tokens[i + 1 :]
    ]
    if not carriers:
        return (
            "pip install is not a plain `pip install <args>` command "
            "(list/exec/substitution form; fail closed)"
        )
    rest = tokens[carriers[0] + 1 :]
    verb = rest.index("install")
    if any(not g.startswith("-") for g in rest[:verb]):
        return "pip global option with a value before the verb (fail closed)"
    args = _expand(rest[verb + 1 :])

    flags: set[str] = set()
    req_files: list[str] = []
    editable: list[str] = []
    positional: list[str] = []
    only_binary: str | None = None
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in ("-r", "--requirement", "-e", "--editable", "--only-binary"):
            if i + 1 >= len(args):
                return f"pip option {tok} without a value (fail closed)"
            value = args[i + 1]
            if tok in ("-r", "--requirement"):
                req_files.append(value)
            elif tok in ("-e", "--editable"):
                editable.append(value)
            else:
                only_binary = value
            i += 2
        elif tok in _FLAGS_OK:
            flags.add(tok)
            i += 1
        else:
            positional.append(tok)
            i += 1

    if "--require-hashes" in flags:
        if only_binary != ":all:":
            return "hashed install without --only-binary=:all: (C1)"
        if not req_files or editable or positional:
            return "hashed install must be `-r <lock>` and nothing else"
        return None
    if "--no-deps" in flags:
        if "--no-build-isolation" not in flags:
            return "--no-deps source build without --no-build-isolation (C1)"
        if req_files:
            return "--no-deps source build must not take -r requirement files (C4)"
        targets = editable + positional
        if targets and all(p == "." or p.startswith(VCS_SPEC_PREFIX) for p in targets):
            return None
        return (
            "--no-deps is only allowed for `.` / `-e .` / the VCS requirement, "
            f"got {targets}"
        )
    return "unhashed pip install (needs --require-hashes ... -r <lock>, or an exempt --no-deps source build)"


def classify(statement: str) -> str | None:
    """None if allowed (or not an installer statement); else the reason it is not."""
    if PIP_ENV_ASSIGN.search(statement):
        return "PIP_REQUIREMENT / PIP_CONSTRAINT / PIP_EDITABLE / PIP_CONFIG_FILE injects requirements"
    loose = _loose_view(statement)
    sh = _shlex_view(statement)
    views = [v for v in (loose, sh) if v is not None]
    for view in views:
        reason = forbidden_installer(view)
        if reason:
            return reason
    if not any(_has_pip_install(v) for v in views):
        return None
    if sh is None:
        return "unparseable pip install statement (fail closed)"
    return _classify_pip_install(sh)


def is_pip_install(statement: str) -> bool:
    """True when a pip install is DETECTED on the statement (either tokenisation)."""
    views = [
        v for v in (_loose_view(statement), _shlex_view(statement)) if v is not None
    ]
    return any(_has_pip_install(v) for v in views)


def statements(text: str) -> list[str]:
    """Logical segments: continuations joined, comments/prose echo dropped,
    split on && || ; |. An ``echo``/``printf`` that carries a command
    substitution (``$(`` or a backtick) EXECUTES it and is kept."""
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    out: list[str] = []
    for line in joined.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # `RUN`, make `@`, yaml `run:`/`- ` prefixes are noise for verb matching
        for seg in re.split(r"&&|\|\||;|\|", line):
            body = seg.strip().lstrip("@-").strip()
            if body.startswith("#"):
                continue
            if body.startswith(("echo ", "printf ")) and not re.search(r"\$\(|`", body):
                continue
            out.append(body)
    return out


def _yaml_strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _yaml_strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _yaml_strings(v)]
    return []


def _py_units(text: str) -> list[str] | None:
    """String literals, and the string elements of every list/tuple literal
    joined as one command (``["pip", "install", ...]``, ``[sys.executable,
    "-m", "pip", ...]``), including lists that span several lines. ``None``
    when the file does not parse (the caller then falls back to the raw lines)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, (ast.List, ast.Tuple)):
            strs = [
                e.value
                for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
            if len(strs) >= 2:
                out.append(" ".join(strs))
    return out


def scan_units(rel: str, text: str) -> list[str]:
    """Every statement the scanner judges for one file. ``.py``: ONLY the parsed
    literals (lists joined as one command, backtick spans removed), falling back
    to the raw lines if the file does not parse. Everything else: the raw line
    scan, plus (YAML) the parsed scalars, where a folded ``run: >`` block folds
    onto ONE command."""
    if rel.endswith(".py"):
        # Python has no backtick substitution: a backtick span in a literal is a
        # markdown hint to a human ("Reinstall with `pip install x`"), not a command.
        py = _py_units(text)
        if py is not None:
            return [u for s in py for u in statements(re.sub(r"`+[^`]+`+", " ", s))]
    units = statements(text)
    if rel.endswith((".yml", ".yaml")):
        try:
            docs = list(yaml.safe_load_all(text))
        except yaml.YAMLError:
            docs = []
        for doc in docs:
            for s in _yaml_strings(doc):
                units += statements(s)
    return units


def install_violations(files: dict[str, str]) -> list[str]:
    bad: list[str] = []
    seen: set[tuple[str, str]] = set()
    for rel, text in sorted(files.items()):
        for stmt in scan_units(rel, text):
            reason = classify(stmt)
            if reason and (rel, stmt) not in seen:
                seen.add((rel, stmt))
                bad.append(f"{rel}: {reason}: {stmt[:140]}")
    return bad
