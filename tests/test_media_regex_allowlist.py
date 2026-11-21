"""LIBRECHAT-IMAGE-SLICE round 5 (review E1) -- every regular expression in ``audittrace.media`` is listed.

The complexity guard of ``test_media_scan_complexity.py`` only sees the work the scan does
through its two seams. A regular expression added beside the scan (a pre-filter, a second
pass) would be invisible to it, and a regular expression on caller text is exactly the defect
code scanning found (quadratic backtracking). So, structurally:

* every use of ``re`` in ``src/audittrace/media/`` (the import, ``re.<call>``, and every call of
  a compiled pattern's method) must be on an explicit allow-list below, each entry with a
  one-line complexity note; an unlisted use, or a listed one that no longer exists, fails;
* the data-URL scan (``scrub_data_url_payloads`` and its three helpers) and the link check
  (``_is_link``) use no regular expression at all, neither ``re`` nor any compiled pattern.

Read from the source with ``ast``: no clock, no execution of the scanned code.
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


def _is_re_call(node: ast.Call) -> str | None:
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "re"
    ):
        return func.attr
    return None


def _module_uses(tree: ast.Module) -> tuple[set[str], set[str]]:
    """The set of uses of ``re`` in a module and the names of its compiled patterns."""
    uses: set[str] = set()
    patterns: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name == "re" for a in node.names):
            uses.add("import re")
        elif isinstance(node, ast.ImportFrom) and node.module == "re":
            uses.add("import re")
        elif (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _is_re_call(node.value) == "compile"
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    patterns.add(target.id)
                    uses.add(f"re.compile:{target.id}")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = _is_re_call(node)
        if called is not None:
            if called != "compile" or not _is_assigned_compile(tree, node):
                uses.add(f"re.{called}")
        elif (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in patterns
        ):
            uses.add(f"{node.func.value.id}.{node.func.attr}")
    return uses, patterns


def _is_assigned_compile(tree: ast.Module, call: ast.Call) -> bool:
    return any(
        isinstance(node, ast.Assign) and node.value is call for node in ast.walk(tree)
    )


def _parse(name: str) -> ast.Module:
    return ast.parse((MEDIA / name).read_text(encoding="utf-8"))


def _found() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(MEDIA.glob("*.py")):
        uses, _ = _module_uses(ast.parse(path.read_text(encoding="utf-8")))
        found |= {(path.name, use) for use in uses}
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


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


class TestTheScansUseNoRegex:
    @pytest.mark.parametrize(("file", "function"), REGEX_FREE)
    def test_function_has_no_regular_expression(self, file: str, function: str) -> None:
        tree = _parse(file)
        _, patterns = _module_uses(tree)
        names = {
            node.id
            for node in ast.walk(_function(tree, function))
            if isinstance(node, ast.Name)
        }
        assert names & ({"re"} | patterns) == set()

    def test_the_link_guard_module_imports_no_re(self) -> None:
        tree = _parse("link_guard.py")
        assert _module_uses(tree)[0] == set()
