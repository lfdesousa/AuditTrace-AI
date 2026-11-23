"""LIBRECHAT-IMAGE-SLICE round 5 (review E1) -- every regular expression in ``audittrace.media`` is listed.

The complexity guard of ``test_media_scan_complexity.py`` only sees the work the scan does
through its two seams. A regular expression added beside the scan (a pre-filter, a second
pass) would be invisible to it, and a regular expression on caller text is exactly the defect
code scanning found (quadratic backtracking). So, structurally:

* every use of ``re`` in ``src/audittrace/media/`` (the import, ``re.<call>``, and every call of
  a compiled pattern's method) must be on an explicit allow-list below, each entry with a
  one-line complexity note; an unlisted use, or a listed one that no longer exists, fails.
  ``re`` is followed through ``import re as r``, ``from re import compile [as c]`` and
  aliases of either, at module level or inside a function (``_re = re``, ``make =
  re.compile``, ``q = _PATTERN``, and a bound method of a pattern, ``s = _PATTERN.search``,
  also a reference to ``_PATTERN.search`` that is never called);
  the callees the package may use at all are the stated set ``ALLOWED_CALLEES``;
* the two scan modules (``link_guard``, ``payload_scrub``) import nothing from ``audittrace``
  outside ``audittrace.media``, so a pattern or helper cannot be pulled in from elsewhere to do
  the scan's work out of sight of this file;
* the data-URL scan (``scrub_data_url_payloads`` and its three helpers) and the link check
  (``_is_link``) use no regular expression at all, neither ``re`` nor any compiled pattern.

Read from the source with ``ast``: no clock, no execution of the scanned code. WHAT IT
MISSES (all of them ways to reach ``re`` or a pattern without a name this analysis follows;
none is used in the package today):

* ``getattr(re, "findall")`` or ``getattr(_PATTERN, "search")`` (a literal name too, not only
  a computed one) and ``vars(re)["findall"]``;
* ``sys.modules["re"]``, ``import re._compiler``, ``import sre_compile`` / ``sre_parse``
  (the same engine reached under another module name; the import rule restricts only what
  the scan modules take from ``audittrace``);
* a compiled pattern put in a container (``PATTERNS = [_P]``) or passed to a helper whose
  parameter is then used (``def run(p, v): return p.search(v)``): the use is a name this file
  does not bind to the pattern;
* a sibling media module that is not a scan module importing from outside ``audittrace.media``
  (the foreign-import rule covers the two scan modules, as ruled);
* a pattern built by a third-party module the scan modules do not import (the standard library
  and ``typing`` imports are not restricted), and the cost of a pattern that IS on the list
  (each entry's note is the claim; the complexity guard measures it).

``importlib.import_module("re")`` and ``from re import *`` ARE caught. CodeQL on the pull
request and review are the backstop. The planted-code tests at the end pin that the checks do
fire (neuters).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

MEDIA = Path(__file__).resolve().parents[1] / "src" / "audittrace" / "media"

#: (file, use) -> the complexity note. A use is "import re", "re.<name>" (a call on the module),
#: "re.compile:<NAME>" (a module-level pattern) or "<NAME>.<method>" (a call on that pattern).
ALLOWED: dict[tuple[str, str], str] = {
    ("digest.py", "import re"): "needed for the header pattern below",
    (
        "digest.py",
        "re.compile:_HEADER",
    ): "classes exclude ';' so parameters are delimited by the literal: linear",
    (
        "digest.py",
        "_HEADER.fullmatch",
    ): "applied to the URL head before its first comma; one give-back per character: linear",
    ("payload_scrub.py", "import re"): "needed for the run pattern below",
    (
        "payload_scrub.py",
        "re.compile:_BASE64_RUN_RE",
    ): "one character class, no nesting: O(128 n) worst case, a constant factor",
    (
        "payload_scrub.py",
        "_BASE64_RUN_RE.sub",
    ): "scans each run from its offsets, at most 128 steps each: linear",
}

#: (file, function): these use no regular expression at all.
REGEX_FREE: list[tuple[str, str]] = [
    ("payload_scrub.py", "scrub_data_url_payloads"),
    ("payload_scrub.py", "_opening"),
    ("payload_scrub.py", "_data_start"),
    ("payload_scrub.py", "_is_token_char"),
    ("link_guard.py", "_is_link"),
]


#: the callees of ``re`` this package may use at all (the stated set); an entry of ALLOWED
#: naming any other callee is itself a failure.
ALLOWED_CALLEES = frozenset({"compile", "fullmatch", "sub"})

#: the scan modules: the ones the export-time chokepoint imports (``logging_config`` imports
#: ``payload_scrub``; ``redaction`` and ``refusal`` import ``link_guard``). Per CODE-INDEX at
#: this commit, the only importers of ``re`` in ``audittrace.media`` are ``digest`` and
#: ``payload_scrub``; these two may import nothing from ``audittrace`` outside the package.
SCAN_MODULES = ("link_guard.py", "payload_scrub.py")


def _re_names(tree: ast.Module) -> tuple[set[str], dict[str, str]]:
    """The local names bound to the module ``re``, and to its functions, however imported.

    ``import re``, ``import re as r``, ``from re import compile [as c]``, and module-level
    aliases of either (``_re = re``, ``make = re.compile``), to a fixed point.
    """
    modules: set[str] = set()
    functions: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {(a.asname or a.name) for a in node.names if a.name == "re"}
        elif (
            isinstance(node, ast.ImportFrom) and node.module == "re" and not node.level
        ):
            functions |= {(a.asname or a.name): a.name for a in node.names}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            targets, value = _assignment(node)
            if isinstance(value, ast.Name) and value.id in modules:
                new = {t for t in targets if t not in modules}
                modules |= new
                changed |= bool(new)
            elif isinstance(value, ast.Name) and value.id in functions:
                for t in targets:
                    if t not in functions:
                        functions[t] = functions[value.id]
                        changed = True
            elif (
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id in modules
            ):
                for t in targets:
                    if t not in functions:
                        functions[t] = value.attr
                        changed = True
    return modules, functions


def _assignment(node: ast.AST) -> tuple[list[str], ast.expr | None]:
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)], node.value
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id], node.value
    return [], None


def _re_callee(
    node: ast.expr, modules: set[str], functions: dict[str, str]
) -> str | None:
    """The ``re`` function a call target names (through any alias), or None."""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in modules
    ):
        return node.attr
    if isinstance(node, ast.Name) and node.id in functions:
        return functions[node.id]
    return None


def _module_uses(tree: ast.Module) -> tuple[set[str], set[str]]:
    """The set of uses of ``re`` in a module and the names of its compiled patterns.

    A use is "import re" (``import re`` under any alias, or ``from re import ...``, which is
    also listed as "from re import <name>"), "re.<name>" (a reference to a function of the
    module, called or not, under any alias), "re.compile:<NAME>" (a module-level pattern),
    "<NAME>.<method>" (a call on that pattern) and "dynamic import" (``__import__`` and
    ``importlib.import_module``, which could name ``re`` without an import statement).
    """
    uses: set[str] = set()
    modules, functions = _re_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name == "re" for a in node.names):
            uses.add("import re")
        elif (
            isinstance(node, ast.ImportFrom) and node.module == "re" and not node.level
        ):
            uses.add("import re")
            uses |= {f"from re import {a.name}" for a in node.names}
        elif isinstance(node, ast.Call) and _dynamic_import(node):
            uses.add("dynamic import")
    patterns: dict[str, ast.Call] = {}
    for node in tree.body:
        targets, value = _assignment(node)
        if (
            isinstance(value, ast.Call)
            and _re_callee(value.func, modules, functions) == "compile"
        ):
            for name in targets:
                patterns[name] = value
                uses.add(f"re.compile:{name}")
    # a pattern may be re-bound (``q = _HEADER``) or have a method bound to a name
    # (``s = _HEADER.search``): both names are the pattern, to a fixed point
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            targets, value = _assignment(node)
            holder = value
            if isinstance(value, ast.Attribute):
                holder = value.value
            if isinstance(holder, ast.Name) and holder.id in patterns:
                for name in targets:
                    if name not in patterns:
                        patterns[name] = patterns[holder.id]
                        changed = True
    compiled_calls = {id(call) for call in patterns.values()}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and id(node) not in compiled_calls:
            called = _re_callee(node.func, modules, functions)
            if called is not None:
                uses.add(f"re.{called}")
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in patterns:
                # a method of a compiled pattern, called or only referenced (bound to a
                # name, passed as a callback): the attribute is the use
                uses.add(f"{node.value.id}.{node.attr}")
            elif not _is_call_target(tree, node):
                called = _re_callee(node, modules, functions)
                if called is not None:
                    uses.add(f"re.{called}")
    return uses, set(patterns)


def _is_call_target(tree: ast.Module, attribute: ast.Attribute) -> bool:
    return any(
        isinstance(node, ast.Call) and node.func is attribute for node in ast.walk(tree)
    )


def _dynamic_import(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in {"__import__", "import_module"}
    return isinstance(func, ast.Attribute) and func.attr in {
        "import_module",
        "__import__",
    }


def _foreign_imports(tree: ast.Module) -> list[str]:
    """Imports of ``audittrace`` outside ``audittrace.media`` (relative ones resolved)."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [
                f"import {a.name}"
                for a in node.names
                if a.name.split(".")[0] == "audittrace"
                and not (a.name + ".").startswith("audittrace.media.")
            ]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 1:
                continue  # a sibling inside the package (the scan modules live in media/)
            if node.level > 1:
                found.append(f"from {'.' * node.level}{module} import ...")
            elif module.split(".")[0] == "audittrace" and not (
                (module + ".").startswith("audittrace.media.")
            ):
                found.append(f"from {module} import ...")
    return found


def _parse(name: str) -> ast.Module:
    return ast.parse((MEDIA / name).read_text(encoding="utf-8"))


def _found_in(name: str, source: str) -> set[tuple[str, str]]:
    uses, _ = _module_uses(ast.parse(source))
    return {(name, use) for use in uses}


def _found() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(MEDIA.glob("*.py")):
        found |= _found_in(path.name, path.read_text(encoding="utf-8"))
    return found


class TestEveryRegexIsListed:
    def test_the_media_package_was_read(self) -> None:
        assert len(list(MEDIA.glob("*.py"))) >= 10, "control: the modules were found"
        assert _found(), "control: the known patterns were seen"

    def test_no_use_of_re_outside_the_allow_list(self) -> None:
        assert sorted(_found() - ALLOWED.keys()) == []

    def test_no_stale_entry_in_the_allow_list(self) -> None:
        assert sorted(ALLOWED.keys() - _found()) == []

    def test_every_entry_has_a_one_line_complexity_note(self) -> None:
        assert all(note and "\n" not in note for note in ALLOWED.values())

    def test_the_listed_callees_are_the_stated_set(self) -> None:
        callees = {
            use.split(".", 1)[1].split(":")[0]
            for _, use in ALLOWED
            if use.startswith("re.")
        } | {
            use.split(".", 1)[1] for _, use in ALLOWED if "." in use and ":" not in use
        }
        assert callees == ALLOWED_CALLEES


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _regex_names_in(tree: ast.Module, function: str) -> set[str]:
    """The names in ``function`` that are ``re`` or a compiled pattern (or a bound method)."""
    _, patterns = _module_uses(tree)
    names = {
        node.id
        for node in ast.walk(_function(tree, function))
        if isinstance(node, ast.Name)
    }
    return names & ({"re"} | patterns)


class TestTheScansUseNoRegex:
    @pytest.mark.parametrize(("file", "function"), REGEX_FREE)
    def test_function_has_no_regular_expression(self, file: str, function: str) -> None:
        assert _regex_names_in(_parse(file), function) == set()

    @pytest.mark.parametrize(
        "body",
        [
            "return _P.search(v)",
            "s = _P.search\n    return s(v)",
            "return re.sub('a', 'b', v)",
            "q = _P\n    return q.sub('a', v)",
        ],
        ids=["method", "bound-method-local", "re-call", "pattern-alias"],
    )
    def test_a_planted_regex_in_a_regex_free_function_is_reported(
        self, body: str
    ) -> None:
        clean = "import re\n_P = re.compile('a')\n\ndef f(v):\n    return v\n"
        planted = clean.replace("    return v", "    " + body)
        assert _regex_names_in(ast.parse(clean), "f") == set(), "control"
        assert _regex_names_in(ast.parse(planted), "f") != set(), body

    def test_a_bound_method_alias_declared_at_module_level_is_reported(self) -> None:
        """Review round: ``_S = _PATTERN.search`` then ``_S(v)`` in a regex-free function."""
        source = (
            "import re\n_P = re.compile('a')\n_S = _P.search\n\n"
            "def f(v):\n    return _S(v)\n"
        )
        assert _regex_names_in(ast.parse(source), "f") == {"_S"}

    def test_the_link_guard_module_imports_no_re(self) -> None:
        tree = _parse("link_guard.py")
        assert _module_uses(tree)[0] == set()


def _foreign(name: str, source: str) -> list[str]:
    return _foreign_imports(ast.parse(source)) if name in SCAN_MODULES else []


class TestScanModulesImportNothingFromElsewhere:
    @pytest.mark.parametrize("name", SCAN_MODULES)
    def test_only_audittrace_media_is_imported(self, name: str) -> None:
        assert (MEDIA / name).is_file(), "control: the scan module exists"
        assert _foreign_imports(_parse(name)) == []

    def test_the_scan_modules_do_import_from_the_package(self) -> None:
        """Control: the rule sees imports (the package-internal ones it allows)."""
        text = (MEDIA / "link_guard.py").read_text(encoding="utf-8")
        assert "from audittrace.media.json_walk import" in text
        assert "audittrace.media.json_walk" in {
            n.module
            for n in ast.walk(_parse("link_guard.py"))
            if isinstance(n, ast.ImportFrom)
        }


#: planted code, appended to a copy of the real module source (the shipped file is never edited).
#: (id, module, snippet, what the checks must report)
PLANTED_RE: list[tuple[str, str, str]] = [
    ("alias-compile", "payload_scrub.py", "import re as r\n_X = r.compile('a+')\n"),
    (
        "alias-call",
        "digest.py",
        "import re as r\n\ndef _f(v):\n    return r.sub('a', 'b', v)\n",
    ),
    (
        "from-compile",
        "payload_scrub.py",
        "from re import compile\n_X = compile('a+')\n",
    ),
    (
        "from-alias",
        "digest.py",
        "from re import match as m\n\ndef _f(v):\n    return m('a', v)\n",
    ),
    (
        "module-alias",
        "digest.py",
        "_r = re\n\ndef _f(v):\n    return _r.sub('a', 'b', v)\n",
    ),
    ("function-alias", "digest.py", "_c = re.compile\n_Y = _c('a+')\n"),
    ("literal-re", "digest.py", "def _f(v):\n    return re.sub('a', 'b', v)\n"),
    ("link-guard-import", "link_guard.py", "import re\n"),
    ("dynamic-import", "digest.py", "_m = __import__('re')\n"),
    (
        "pattern-alias-method",
        "digest.py",
        "_Q = _HEADER\n\ndef _f(v):\n    return _Q.search(v)\n",
    ),
    (
        "bound-method-alias",
        "payload_scrub.py",
        "_S = _BASE64_RUN_RE.search\n\ndef _f(v):\n    return _S(v)\n",
    ),
    (
        "bound-method-alias-local",
        "digest.py",
        "def _f(v):\n    s = _HEADER.match\n    return s(v)\n",
    ),
    (
        "method-reference-only",
        "payload_scrub.py",
        "_L = list(map(_BASE64_RUN_RE.search, []))\n",
    ),
]


class TestThePlantedCodeIsCaught:
    """Neuters, kept in the suite: each snippet, appended to a copy, must be reported."""

    @pytest.mark.parametrize(
        ("case", "name", "snippet"), PLANTED_RE, ids=[c[0] for c in PLANTED_RE]
    )
    def test_a_planted_use_of_re_is_outside_the_allow_list(
        self, case: str, name: str, snippet: str
    ) -> None:
        source = (MEDIA / name).read_text(encoding="utf-8")
        assert _found_in(name, source) <= ALLOWED.keys(), (
            "control: the copy starts clean"
        )
        extra = _found_in(name, source + "\n" + snippet) - ALLOWED.keys()
        assert extra != set(), case

    @pytest.mark.parametrize("name", SCAN_MODULES)
    @pytest.mark.parametrize(
        "line",
        [
            "from audittrace.x import y\n",
            "import audittrace.config\n",
            "from audittrace import config\n",
            "from ..logging_config import scrub_text\n",
            "from audittrace.mediax import z\n",
        ],
    )
    def test_a_planted_import_from_elsewhere_is_reported(
        self, name: str, line: str
    ) -> None:
        source = (MEDIA / name).read_text(encoding="utf-8")
        assert _foreign(name, source) == [], "control: the copy starts clean"
        assert _foreign(name, source + "\n" + line) != [], line

    def test_an_import_inside_the_package_is_not_reported(self) -> None:
        source = (MEDIA / "link_guard.py").read_text(encoding="utf-8")
        for line in (
            "from audittrace.media.digest import X\n",
            "from . import digest\n",
        ):
            assert _foreign("link_guard.py", source + "\n" + line) == [], line

    def test_a_callee_outside_the_stated_set_is_reported_by_name(self) -> None:
        source = (MEDIA / "digest.py").read_text(encoding="utf-8")
        extra = _found_in("digest.py", source + "\nimport re as r\n_Z = r.findall\n")
        assert ("digest.py", "re.findall") in extra - ALLOWED.keys()
        assert "findall" not in ALLOWED_CALLEES
