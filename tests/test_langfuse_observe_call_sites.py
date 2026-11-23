"""#474 review A4 -- the request payload reaches a Langfuse-observed function POSITIONALLY, never by keyword.

``@_lf_observe(name="sovereign-chat-request")`` (Langfuse ``observe``) records the arguments of the
function it decorates as the observation's input. The SDK's media manager then walks that input
looking for ``data:...;base64,`` strings and UPLOADS what it finds to Langfuse media storage.
Its walk descends lists and dicts but not the ``args`` tuple the decorator builds for positional
arguments, so a request whose messages carry an inline image is not uploaded today only because
both call sites pass ``payload`` positionally. Calling with ``payload=payload`` (a refactor that
looks harmless) would put the whole request, images included, in the walked ``kwargs`` dict.

Structural guard, read with ``ast`` over ``src/audittrace``:

* the observed functions are exactly the ones this file lists (a new ``@observe``-decorated
  function must be added here on purpose, with its call sites checked);
* every reference to one of them is a direct call or the callee of ``asyncio.to_thread(...)``
  (the tools-mode path); anything else (``partial``, ``run_in_executor``, a stored reference) is
  reported, because its arguments cannot be read;
* at each of those, the parameter named ``payload`` is supplied by position, there is no keyword
  named ``payload`` and no ``*args`` / ``**kwargs`` splat (whose contents cannot be read).

WHAT IT MISSES: a call through ``getattr`` or an alias of the function (``f = _build...``), and
a payload carried under another parameter name than ``payload``. The SDK's own walk is the
backstop; this file is the guard on what the repository passes to it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "audittrace"
CHAT = SRC / "routes" / "chat.py"

#: (file, function): the observed functions whose ``payload`` parameter must stay positional.
OBSERVED = {
    ("routes/chat.py", "_build_memory_context_with_trace"),
    ("routes/chat.py", "_prepare_tools_mode_trace"),
}
Function = ast.FunctionDef | ast.AsyncFunctionDef


def _is_observe(decorator: ast.expr) -> bool:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Name):
        return target.id in {"_lf_observe", "observe"}
    return isinstance(target, ast.Attribute) and target.attr == "observe"


def _observed(tree: ast.Module) -> dict[str, Function]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(_is_observe(d) for d in node.decorator_list)
    }


def _name_of(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _violations(
    path_name: str, source: str, observed: dict[str, Function]
) -> list[str]:
    """Every reference to an observed function that does not pass ``payload`` by position."""
    tree = ast.parse(source)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Name, ast.Attribute)):
            continue
        name = _name_of(node)
        if name not in observed or isinstance(parents.get(node), ast.Attribute):
            continue
        function = observed[name]
        where = f"{path_name}:{node.lineno} {name}"
        parent = parents.get(node)
        if isinstance(parent, ast.Call) and parent.func is node:
            positional, call = parent.args, parent
        elif (
            isinstance(parent, ast.Call)
            and _name_of(parent.func) == "to_thread"
            and parent.args
            and parent.args[0] is node
        ):
            positional, call = parent.args[1:], parent
        else:
            problems.append(f"{where}: used other than called or to_thread'd")
            continue
        params = [a.arg for a in function.args.args if a.arg not in {"self", "cls"}]
        if "payload" not in params:
            problems.append(f"{where}: the function has no parameter named payload")
            continue
        if any(isinstance(a, ast.Starred) for a in positional):
            problems.append(f"{where}: *args splat")
        if any(k.arg is None for k in call.keywords):
            problems.append(f"{where}: **kwargs splat")
        if any(k.arg == "payload" for k in call.keywords):
            problems.append(f"{where}: payload passed by keyword")
        elif params.index("payload") >= len(positional):
            problems.append(f"{where}: payload not passed by position")
    return problems


def _scan_src() -> tuple[dict[tuple[str, str], Function], list[str], int]:
    """(observed functions by (file, name), all violations, references seen)."""
    observed: dict[tuple[str, str], Function] = {}
    sources: dict[str, str] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        sources[rel] = path.read_text(encoding="utf-8")
        for name, fn in _observed(ast.parse(sources[rel])).items():
            observed[(rel, name)] = fn
    by_name = {name: fn for (_, name), fn in observed.items()}
    problems: list[str] = []
    for rel, source in sources.items():
        problems += _violations(rel, source, by_name)
    seen = sum(
        1
        for source in sources.values()
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Name) and node.id in by_name
    )
    return observed, problems, seen


class TestPayloadIsPositional:
    def test_the_observed_functions_are_the_listed_ones(self) -> None:
        observed, _, _ = _scan_src()
        assert set(observed) == OBSERVED

    def test_every_call_site_passes_payload_by_position(self) -> None:
        _, problems, seen = _scan_src()
        # control: the rule saw both call sites (one direct call, one ``asyncio.to_thread``
        # reference); a count of zero would make the next assertion vacuous
        assert seen == 2, "control: 1 direct call + 1 to_thread reference"
        assert problems == []


def _chat() -> tuple[str, dict[str, Function]]:
    source = CHAT.read_text(encoding="utf-8")
    return source, _observed(ast.parse(source))


class TestThePlantedCallIsReported:
    """Neuters, kept in the suite: the real source with one call changed must be reported."""

    def test_the_real_chat_module_starts_clean(self) -> None:
        source, observed = _chat()
        assert _violations("routes/chat.py", source, observed) == []

    @pytest.mark.parametrize(
        "callee",
        ["_build_memory_context_with_trace", "_prepare_tools_mode_trace"],
    )
    def test_a_keyword_payload_in_the_real_call_is_reported(self, callee: str) -> None:
        source, observed = _chat()
        tree = ast.parse(source)
        moved = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            offset = 0
            if _name_of(node.func) == "to_thread" and node.args:
                offset = 1
            if offset == 0 and _name_of(node.func) != callee:
                continue
            if offset == 1 and _name_of(node.args[0]) != callee:
                continue
            params = [a.arg for a in observed[callee].args.args]
            index = params.index("payload") + offset
            node.keywords.append(ast.keyword(arg="payload", value=node.args.pop(index)))
            moved += 1
        assert moved == 1, "control: exactly the one real call was changed"
        problems = _violations("routes/chat.py", ast.unparse(tree), observed)
        assert any("payload passed by keyword" in p for p in problems), problems

    def test_payload_by_keyword_in_a_direct_call_is_reported(self) -> None:
        source = (
            "@_lf_observe(name='x')\n"
            "def f(payload, query):\n    return 1\n\n"
            "def g(p):\n    return f(payload=p, query='q')\n"
        )
        observed = _observed(ast.parse(source))
        assert set(observed) == {"f"}
        problems = _violations("m.py", source, observed)
        assert any("payload passed by keyword" in p for p in problems), problems

    @pytest.mark.parametrize(
        ("call", "expect"),
        [
            ("f(p, 'q')", None),
            ("f(*args)", "*args splat"),
            ("f(**kw)", "**kwargs splat"),
            ("f(query='q', payload=p)", "payload passed by keyword"),
            ("f(query='q')", "payload not passed by position"),
            ("loop.run_in_executor(None, f, p)", "used other than called"),
            ("asyncio.to_thread(f, p, 'q')", None),
            ("asyncio.to_thread(f, query='q', payload=p)", "payload passed by keyword"),
        ],
    )
    def test_each_shape_of_call_is_judged(self, call: str, expect: str | None) -> None:
        source = (
            "@observe()\n"
            "async def f(payload, query):\n    return 1\n\n"
            f"def g(p, args, kw, loop):\n    return {call}\n"
        )
        problems = _violations("m.py", source, _observed(ast.parse(source)))
        if expect is None:
            assert problems == []
        else:
            assert any(expect in p for p in problems), problems
