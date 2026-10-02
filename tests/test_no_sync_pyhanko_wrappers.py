"""Spec #460 AC9 — no pyhanko / certvalidator SYNC wrapper anywhere in ``src/``.

pyhanko's synchronous entry points are thin wrappers that run
``asyncio.run(<coroutine>)``. Called from async code (the whole app) they
raise ``asyncio.run() cannot be called from a running event loop``, which is
how #366 made every signed PDF read ``check_failed``.

Resolved versions the literal list below was enumerated on: pyhanko 0.37.0,
pyhanko-certvalidator 0.32.1.

Matching is on QUALIFIED names resolved through each module's import table
(never a bare ``.sign(``, which would false-positive on unrelated APIs), plus
a short list of method names that are unambiguous in this code base. A
module-level indirect wrapper (``pyhanko.sign.signers.sign_pdf``) is banned
too.

Neuter: reintroduce ``validate_pdf_signature`` in a SYNC helper under
``src/`` -> ``test_src_has_no_banned_sync_wrapper`` goes RED
(``test_a_sync_helper_calling_the_wrapper_is_flagged`` proves the scanner
sees that exact shape).
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"

# Qualified module-level functions that call ``asyncio.run`` (directly, or
# transitively for ``sign_pdf``), as enumerated by ``discover_sync_wrappers``.
BANNED_QUALIFIED: frozenset[str] = frozenset(
    {
        "pyhanko.sign.validation.validate_pdf_signature",
        "pyhanko.sign.validation.validate_pdf_timestamp",
        "pyhanko.sign.validation.add_validation_info",
        "pyhanko.sign.signers.sign_pdf",
        "pyhanko.sign.signers.functions.sign_pdf",
        "pyhanko_certvalidator.validate.validate_path",
    }
)

# Sync METHODS that call ``asyncio.run``. Unambiguous names; an instance call
# cannot be resolved through an import table, so the attribute name is banned.
BANNED_METHOD_NAMES: frozenset[str] = frozenset(
    {
        "sign_pdf",  # PdfSigner.sign_pdf (use async_sign_pdf)
        "finish_signing",  # PdfTBSDocument.finish_signing
        "timestamp_pdf",  # PdfTimeStamper.timestamp_pdf
        "update_archival_timestamp_chain",  # PdfTimeStamper
        "build_paths",  # PathBuilder.build_paths
    }
)

# ``module:qualname`` of every sync callable found by the source scan on the
# resolved versions. A pyhanko upgrade that adds one turns the cross-check RED.
KNOWN_SYNC_WRAPPERS: frozenset[str] = frozenset(
    {
        "pyhanko.sign.signers.pdf_signer:PdfSigner.sign_pdf",
        "pyhanko.sign.signers.pdf_signer:PdfTBSDocument.finish_signing",
        "pyhanko.sign.signers.pdf_signer:PdfTimeStamper.timestamp_pdf",
        "pyhanko.sign.signers.pdf_signer:PdfTimeStamper.update_archival_timestamp_chain",
        "pyhanko.sign.validation:add_validation_info",
        "pyhanko.sign.validation:validate_pdf_signature",
        "pyhanko.sign.validation:validate_pdf_timestamp",
        "pyhanko_certvalidator.registry:PathBuilder.build_paths",
        "pyhanko_certvalidator.validate:validate_path",
        # transitive: PdfSigner.sign_pdf behind a module-level function
        "pyhanko.sign.signers.functions:sign_pdf",
    }
)


# ───────────────────────────── the scanner ─────────────────────────────


def _import_table(tree: ast.AST) -> dict[str, str]:
    """Local name -> dotted qualified name, for every import in the module
    (function-local imports included: this code base imports lazily)."""
    table: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    table[alias.asname] = alias.name
                else:
                    root = alias.name.split(".")[0]
                    table[root] = root
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                table[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return table


def _qualified(node: ast.AST, table: dict[str, str]) -> str | None:
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name) or cur.id not in table:
        return None
    return ".".join([table[cur.id], *reversed(parts)])


def find_violations(source: str, filename: str = "<src>") -> list[str]:
    tree = ast.parse(source)
    table = _import_table(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                if f"{node.module}.{alias.name}" in BANNED_QUALIFIED:
                    found.add(f"{filename}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.Name | ast.Attribute) and isinstance(
            node.ctx, ast.Load
        ):
            qualified = _qualified(node, table)
            if qualified in BANNED_QUALIFIED:
                found.add(f"{filename}:{node.lineno} {qualified}")
            if isinstance(node, ast.Attribute) and node.attr in BANNED_METHOD_NAMES:
                found.add(f"{filename}:{node.lineno} .{node.attr}")
    return sorted(found)


def discover_sync_wrappers() -> set[str]:
    """Scan the INSTALLED pyhanko + pyhanko_certvalidator sources for non-async
    functions that call ``asyncio.run`` (direct), plus their public sync
    callers inside the same packages (transitive, by name)."""
    import importlib

    funcs: dict[str, tuple[set[str], bool]] = {}
    for pkg in ("pyhanko", "pyhanko_certvalidator"):
        root = pathlib.Path(next(iter(importlib.import_module(pkg).__path__)))
        for path in root.rglob("*.py"):
            rel = path.relative_to(root).with_suffix("")
            module = ".".join([pkg, *rel.parts]).removesuffix(".__init__")
            tree = ast.parse(path.read_text())

            def visit(node: ast.AST, prefix: str, module: str = module) -> None:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.ClassDef):
                        visit(child, f"{prefix}{child.name}.")
                    elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                        calls: set[str] = set()
                        for sub in ast.walk(child):
                            if isinstance(sub, ast.Call):
                                fn = sub.func
                                if isinstance(fn, ast.Attribute):
                                    owner = fn.value
                                    calls.add(
                                        f"asyncio.{fn.attr}"
                                        if isinstance(owner, ast.Name)
                                        and owner.id == "asyncio"
                                        else fn.attr
                                    )
                                elif isinstance(fn, ast.Name):
                                    calls.add(fn.id)
                        funcs[f"{module}:{prefix}{child.name}"] = (
                            calls,
                            isinstance(child, ast.AsyncFunctionDef),
                        )
                        visit(child, f"{prefix}{child.name}.")

            visit(tree, "")
    direct = {
        q
        for q, (calls, is_async) in funcs.items()
        if not is_async and ({"asyncio.run", "asyncio.run_until_complete"} & calls)
    }
    found = set(direct)
    changed = True
    while changed:
        changed = False
        names = {q.split(":")[1].split(".")[-1] for q in found}
        for q, (calls, is_async) in funcs.items():
            short = q.split(":")[1].split(".")[-1]
            if q in found or is_async or short.startswith("_"):
                continue
            if calls & names:
                found.add(q)
                changed = True
    # The transitive pass over-approximates by bare name; keep only the
    # module-level public functions it adds (methods that merely share a name
    # with a wrapper are noise), which is the SF4 ``sign_pdf`` case.
    return direct | {q for q in found - direct if "." not in q.split(":")[1]}


# ───────────────────────────────── tests ─────────────────────────────────


def test_src_has_no_banned_sync_wrapper() -> None:
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        violations += find_violations(path.read_text(), str(path.relative_to(SRC)))
    assert violations == []


def test_a_sync_helper_calling_the_wrapper_is_flagged() -> None:
    """The neuter, as a self-test: the exact shape #366 had."""
    source = (
        "def helper(emb, vc):\n"
        "    from pyhanko.sign.validation import validate_pdf_signature\n"
        "    return validate_pdf_signature(emb, vc)\n"
    )
    assert find_violations(source)


@pytest.mark.parametrize(
    "source",
    [
        "import pyhanko.sign.validation as v\ndef f(e):\n    return v.validate_pdf_signature(e)\n",
        "from pyhanko.sign import validation\ndef f(e):\n    return validation.validate_pdf_timestamp(e)\n",
        "from pyhanko.sign.signers import sign_pdf\ndef f(w):\n    return sign_pdf(w)\n",
        "import pyhanko.sign.signers\ndef f(w):\n    return pyhanko.sign.signers.sign_pdf(w)\n",
        "from pyhanko_certvalidator.validate import validate_path\n",
        "async def f(loop, e):\n    from pyhanko.sign.validation import validate_pdf_signature as v\n    return await loop.run_in_executor(None, v, e)\n",
        "def f(signer, w):\n    return signer.sign_pdf(w)\n",
        "def f(builder, c):\n    return builder.build_paths(c)\n",
    ],
    ids=[
        "alias-module",
        "from-package-module",
        "from-signers-indirect",
        "dotted-indirect",
        "import-only",
        "passed-as-reference",
        "method-sign_pdf",
        "method-build_paths",
    ],
)
def test_the_scanner_resolves_aliases_and_indirect_wrappers(source: str) -> None:
    assert find_violations(source)


@pytest.mark.parametrize(
    "source",
    [
        "def f(key, data):\n    return key.sign(data)\n",
        "from pyhanko.sign.validation import async_validate_pdf_signature\n"
        "async def f(e):\n    return await async_validate_pdf_signature(e)\n",
        "from pyhanko.sign.signers import PdfSigner\n"
        "async def f(s, w):\n    return await PdfSigner(s).async_sign_pdf(w)\n",
    ],
    ids=["bare-sign-is-not-banned", "async-validate-ok", "async-sign-ok"],
)
def test_the_scanner_does_not_flag_async_apis_or_a_bare_sign(source: str) -> None:
    assert find_violations(source) == []


def test_literal_list_matches_a_scan_of_the_installed_pyhanko() -> None:
    """A pyhanko upgrade that adds (or removes) a sync wrapper turns this
    RED, so the ban list cannot go stale silently."""
    assert discover_sync_wrappers() == set(KNOWN_SYNC_WRAPPERS)


def test_every_discovered_wrapper_is_covered_by_the_ban() -> None:
    for entry in discover_sync_wrappers():
        module, _, qual = entry.partition(":")
        if "." in qual:  # a method: banned by attribute name
            assert qual.split(".")[-1] in BANNED_METHOD_NAMES, entry
        else:
            assert f"{module}.{qual}" in BANNED_QUALIFIED, entry
