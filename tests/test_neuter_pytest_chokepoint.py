"""Guard: NOTHING under ``scripts/neuter/`` may invoke pytest except through
the chokepoint (``scripts/neuter/pytest_run.py::run_pytest``) -- review
round 2's structural fix, requirement 2.

An AST scan, not a text grep. The realistic pattern every phase used to
follow (and pytest_run.py itself still does) is ``cmd = [python, "-m",
"pytest", ...]`` built as a SEPARATE statement, then ``subprocess.run(cmd,
...)`` referencing it by name -- the ``"pytest"`` string literal is never a
direct argument of the ``Call`` node itself, so a scan that only inspects a
matched call's own arguments (the first design attempt) MISSES this exact
shape (proven by ``test_scanner_catches_the_realistic_two_statement_shape``
below, the regression test for that fix). The scan instead looks at each
FUNCTION (or the module itself) as a whole: if it contains BOTH a string
literal mentioning ``pytest`` AND a subprocess-invoking call
(``subprocess.run``/``.Popen``/``.check_call``/``.check_output``, or
``pytest.main``) in its OWN body -- never reaching into a NESTED function's
body, which is scanned as its own separate scope -- that scope is a
bypass, unless the file is ``pytest_run.py`` itself.

``hold-shared``'s ``subprocess.run(argv)`` (an arbitrary, caller-supplied
child command -- its whole purpose is to wrap ANY command, pytest-shaped or
not, under the heavy-cap lock) carries no ``"pytest"`` literal anywhere in
its own function body, so it is correctly never flagged; only a function
that actually HARDCODES a pytest invocation is.
"""

from __future__ import annotations

import ast
import tempfile
from pathlib import Path

SCAN_ROOT = Path(__file__).resolve().parent.parent / "scripts" / "neuter"
CHOKEPOINT_FILE = "pytest_run.py"

_FLAGGED_CALL_NAMES = frozenset({"run", "Popen", "check_call", "check_output", "main"})
_FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _call_name(node: ast.AST) -> str | None:
    """The flagged call name, or ``None`` -- ``"main"`` only counts when
    it's an ATTRIBUTE access (``pytest.main(...)``, ``something.main(...)``),
    never a bare ``main()`` call: this module's own ``if __name__ ==
    "__main__": main()`` at module scope is not a pytest invocation, and a
    bare-name match on ``"main"`` false-positived on exactly that."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name) and func.id != "main":
        return func.id
    return None


def _mentions_pytest(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "pytest" in node.value
    )


def _scope_name(node: ast.AST, filename: str, lineno: int) -> str:
    name = getattr(node, "name", None)
    return f"{filename}:{name}" if name else f"{filename}:module:{lineno}"


def _own_body_nodes(scope: ast.AST) -> list[ast.AST]:
    """Every descendant of ``scope`` EXCLUDING the bodies of any nested
    function/async-function defined inside it -- a manual, boundary-aware
    walk (``ast.walk`` has no such stop condition), so a literal in one
    function and a call in an unrelated nested helper can never be
    attributed to the SAME scope."""
    collected: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        collected.append(node)
        if isinstance(node, _FUNC_TYPES):
            continue  # the nested function is scanned as ITS OWN scope
        stack.extend(ast.iter_child_nodes(node))
    return collected


def find_chokepoint_bypasses(
    root: Path, *, exclude: str = CHOKEPOINT_FILE
) -> list[str]:
    """Every ``file:scope`` whose FUNCTION (or module-level code) body
    contains both a ``"pytest"``-mentioning string literal and a
    subprocess-invoking call, outside ``exclude``. Shared by the real guard
    test and its own self-neuter/positive-control tests, so every proof
    exercises the SAME code the guard runs, never a re-implementation that
    could silently drift from it.
    """
    offenders: list[str] = []
    for path in sorted(root.glob("*.py")):
        if path.name == exclude:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        scopes: list[ast.AST] = [tree]
        scopes.extend(n for n in ast.walk(tree) if isinstance(n, _FUNC_TYPES))
        for scope in scopes:
            own_nodes = _own_body_nodes(scope)
            has_pytest_literal = any(_mentions_pytest(n) for n in own_nodes)
            has_subprocess_call = any(
                _call_name(n) in _FLAGGED_CALL_NAMES for n in own_nodes
            )
            if has_pytest_literal and has_subprocess_call:
                offenders.append(
                    _scope_name(scope, path.name, getattr(scope, "lineno", 0))
                )
    return offenders


def test_no_pytest_invocation_outside_the_chokepoint():
    offenders = find_chokepoint_bypasses(SCAN_ROOT)
    assert offenders == [], (
        f"pytest invoked outside scripts/neuter/pytest_run.py::run_pytest: {offenders}"
    )


def test_scan_root_is_non_empty():
    """A positive control on the SCAN ITSELF (not just the product code):
    the glob must actually find files, or an empty scan would pass
    vacuously regardless of what scripts/neuter/ contains."""
    assert len(list(SCAN_ROOT.glob("*.py"))) >= 8


def test_the_chokepoint_file_itself_is_excluded_and_does_invoke_pytest():
    """Proves the exclusion is doing real work: ``pytest_run.py`` DOES
    contain pytest-shaped subprocess calls (it's the chokepoint), and a
    scan that did NOT exclude it would find them -- so the exclusion in
    :func:`find_chokepoint_bypasses` is provably necessary, not a dead
    parameter."""
    unfiltered = find_chokepoint_bypasses(SCAN_ROOT, exclude="")
    assert any(o.startswith(f"{CHOKEPOINT_FILE}:") for o in unfiltered)
    filtered = find_chokepoint_bypasses(SCAN_ROOT)
    assert not any(o.startswith(f"{CHOKEPOINT_FILE}:") for o in filtered)


def test_scanner_fires_on_a_synthetic_bypass(tmp_path):
    """Positive control on the SCANNER's own logic, independent of whatever
    scripts/neuter/ currently contains: a throwaway file with an obvious,
    single-statement bypass must be caught."""
    (tmp_path / "offender.py").write_text(
        "import subprocess\n"
        "def sneaky():\n"
        "    subprocess.run(['python', '-m', 'pytest', 'tests/'])\n"
    )
    (tmp_path / "pytest_run.py").write_text("# the real chokepoint, excluded\n")
    (tmp_path / "innocent.py").write_text(
        "import subprocess\ndef fine():\n    subprocess.run(['git', 'status'])\n"
    )
    offenders = find_chokepoint_bypasses(tmp_path)
    assert offenders == ["offender.py:sneaky"]


def test_scanner_catches_the_realistic_two_statement_shape(tmp_path):
    """The shape that actually matters: ``cmd = [...]`` built as its own
    statement, THEN ``subprocess.run(cmd, ...)`` referencing it by name --
    the exact pattern every real phase (and the chokepoint itself) uses.
    A first-draft scanner that only inspected a matched call's OWN
    arguments missed this entirely (it only sees the bare name ``cmd``,
    never the literal); this is the regression test for that fix."""
    (tmp_path / "offender.py").write_text(
        "import subprocess\n"
        "def two_statement():\n"
        "    cmd = ['python', '-m', 'pytest', 'tests/']\n"
        "    subprocess.run(cmd, cwd='.', capture_output=True)\n"
    )
    offenders = find_chokepoint_bypasses(tmp_path)
    assert offenders == ["offender.py:two_statement"]


def test_scanner_does_not_cross_function_boundaries():
    """A pytest-mentioning literal in ONE function and an unrelated
    subprocess call (git/docker) in ANOTHER must never combine into a
    false positive -- each function is its own scope."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "fine.py").write_text(
            "import subprocess\n"
            "def mentions_pytest_only():\n"
            "    return 'see pytest docs'\n"
            "def runs_git_only():\n"
            "    subprocess.run(['git', 'status'])\n"
        )
        assert find_chokepoint_bypasses(root) == []
