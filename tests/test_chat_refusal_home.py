"""LIBRECHAT-IMAGE-SLICE ADDENDUM-2 AC-2 addition (gate r2 S3) -- ONE home for the refusal.

ADDENDUM-1 specified the refusal helper twice, incompatibly: "one helper call each into
``media/``" and "both refusal writes stay in ``chat.py`` or ``_upstream_errors.py``". Ruling:

* ``audittrace.media`` only CLASSIFIES: ``classify_refusal(messages, record)`` is pure and
  returns a ``RequestRefusal`` value (reason, position, reply body, row detail) or ``None``;
* the ONE audit write of a refused request and its reply live in
  ``routes/_upstream_errors.py::refused_request_response``, a module the write-site guard
  reads (``tests/test_chat_persist_sites_carry_input_media.py``);
* ``chat.py`` calls each exactly once.

The properties below are identities over the source (stdlib ``ast``), not a list of places
where a write must not be.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "audittrace"
ROUTES = SRC / "routes"
GUARDED = ("chat.py", "_upstream_errors.py")
WRITERS = {"_persist_interaction", "_persist_or_enqueue"}
IMPURE_ROOTS = ("audittrace.routes", "audittrace.db", "sqlalchemy", "httpx", "fastapi")
DATA_URL = "data:image/png;base64,QUJD"


def _callee(node: ast.Call) -> str:
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")


def _calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _refusal_writes() -> list[tuple[str, str]]:
    """(module, enclosing function) of every call that writes a ``request_refused`` row."""
    found: list[tuple[str, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    value = keyword.value
                    named = (
                        isinstance(value, ast.Name)
                        and value.id == "FAILURE_REQUEST_REFUSED"
                    )
                    literal = (
                        isinstance(value, ast.Constant)
                        and value.value == "request_refused"
                    )
                    if keyword.arg == "failure_class" and (named or literal):
                        found.append((path.relative_to(SRC).as_posix(), func.name))
    return found


class TestMediaOnlyClassifies:
    def test_the_media_package_imports_no_route_database_or_http_code(self) -> None:
        imported: set[str] = set()
        for path in sorted((SRC / "media").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
                elif isinstance(node, ast.Import):
                    imported |= {alias.name for alias in node.names}
        assert len(imported) > 0, "control: imports were read"
        assert sorted(m for m in imported if m.startswith(IMPURE_ROOTS)) == []

    def test_the_media_package_calls_no_row_writer(self) -> None:
        calls = [
            (path.name, _callee(node))
            for path in sorted((SRC / "media").glob("*.py"))
            for node in _calls(path)
        ]
        assert len(calls) > 0, "control: calls were read"
        assert [c for c in calls if c[1] in WRITERS | {"InteractionRecord"}] == []

    def test_importing_either_side_first_works(self) -> None:
        """No import cycle: each order in a fresh interpreter."""
        for first, second in (
            ("audittrace.media", "audittrace.routes._upstream_errors"),
            ("audittrace.routes._upstream_errors", "audittrace.media"),
            ("audittrace.routes.chat", "audittrace.media"),
        ):
            done = subprocess.run(
                [sys.executable, "-c", f"import {first}; import {second}"],
                capture_output=True,
                text=True,
                check=False,
            )
            assert done.returncode == 0, (first, second, done.stderr[-400:])


class TestOneWriteInAGuardedModule:
    def test_exactly_one_call_writes_a_refused_row_and_the_guard_reads_it(self) -> None:
        assert _refusal_writes() == [
            ("routes/_upstream_errors.py", "refused_request_response")
        ]

    def test_that_call_is_a_row_writer_the_write_site_guard_counts(self) -> None:
        calls = [
            node
            for node in _calls(ROUTES / "_upstream_errors.py")
            if _callee(node) in WRITERS
            and any(
                k.arg == "failure_class"
                and isinstance(k.value, ast.Name)
                and k.value.id == "FAILURE_REQUEST_REFUSED"
                for k in node.keywords
            )
        ]
        assert len(calls) == 1
        assert any(k.arg == "input_media" for k in calls[0].keywords)

    def test_chat_calls_the_classifier_and_the_helper_once_each(self) -> None:
        names = [_callee(node) for node in _calls(ROUTES / "chat.py")]
        assert names.count("classify_refusal") == 1
        assert names.count("refused_request_response") == 1
        for gone in ("refusal_body", "refusal_detail", "media_link_body"):
            assert names.count(gone) == 0, gone

    def test_no_other_module_builds_the_refusal_reply(self) -> None:
        callers = sorted(
            path.relative_to(SRC).as_posix()
            for path in SRC.rglob("*.py")
            if any(_callee(n) == "refused_request_response" for n in _calls(path))
        )
        assert callers == ["routes/chat.py"]


def _image(url: str) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": url}}


CASES: list[tuple[str, list[Any], tuple[str, int, int] | None]] = [
    ("no media", [{"type": "text", "text": "hello"}], None),
    ("inline image", [{"type": "text", "text": "t"}, _image(DATA_URL)], None),
    (
        "linked image",
        [{"type": "text", "text": "t"}, _image("https://example.invalid/a.png")],
        ("image_url_not_supported", 0, 1),
    ),
    (
        "damaged image",
        [_image("data:image/png;base64,@@@")],
        ("image_data_invalid", 0, 0),
    ),
    (
        "linked video",
        [
            {"type": "text", "text": "t"},
            {"type": "text", "text": "u"},
            {"type": "input_video", "input_video": {"url": "http://example.invalid/"}},
        ],
        ("media_link_not_supported", 0, 2),
    ),
    (
        "an image refusal comes before a media link",
        [
            {"type": "input_audio", "input_audio": {"url": "http://example.invalid/"}},
            _image("https://example.invalid/a.png"),
        ],
        ("image_url_not_supported", 0, 1),
    ),
]


class TestTheClassifierIsPureAndTotal:
    @pytest.mark.parametrize(("name", "content", "expected"), CASES)
    def test_it_returns_a_value_and_changes_nothing(
        self, name: str, content: list[Any], expected: tuple[str, int, int] | None
    ) -> None:
        from audittrace.media import classify_refusal, digest_input_media

        messages = [{"role": "user", "content": content}]
        before = repr(messages)
        refusal = classify_refusal(messages, digest_input_media(messages))
        assert repr(messages) == before, name
        if expected is None:
            assert refusal is None, name
            return
        reason, message_index, part_index = expected
        assert refusal is not None, name
        assert (refusal.reason, refusal.message_index, refusal.part_index) == expected
        assert refusal.detail == f"{reason}: message {message_index} part {part_index}"
        error = refusal.body()["error"]
        assert (error["code"], error["type"], error["param"], error["status"]) == (
            reason,
            "invalid_request_error",
            "messages",
            400,
        )

    def test_an_unknown_reason_cannot_be_answered(self) -> None:
        from audittrace.media.request_refusal import RequestRefusal

        with pytest.raises(KeyError):
            RequestRefusal("not_a_reason", 0, 0).body()
