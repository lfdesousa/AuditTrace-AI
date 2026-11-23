"""LIBRECHAT-IMAGE-SLICE round 4 -- the scans of caller text are linear, and equal to the former patterns.

A code-scanning finding on the pull request: the data-URL scrub was a regular expression that
backtracked quadratically on caller text (``base64,`` followed by ``data:+`` and ``;data:+``
repeated 16000 times, 112 KB, ran for about 20 s). It is now a scan anchored on the literal
``;base64,`` that validates backwards. The link guard's pattern is replaced by a string check.

No assertion here reads a clock (a wall-clock bound is a flake). Linearity is argued
STRUCTURALLY, with three counters, on adversarial inputs at two sizes (the count stays below a
constant times the input length and grows linearly with it). What each one proves, and what
none of them can see, is stated on the counter (see ``_Steps``, ``_line_events``,
``_instruction_events``). Taken together they are a regression net for the quadratic shape that
code scanning found, NOT a proof of linearity: work done inside one C-level call (``str.count``,
``str.find``, ``re`` and the like) costs one event whatever the input, and is closed only by the
other guard in this package (``test_media_regex_allowlist.py``: no regular expression on the
scan, and no import of anything else), by review, and by CodeQL on the pull request. The 112 KB
adversarial inputs are also ordinary inputs that must give the correct output. Equality with the
former patterns is checked on random strings, the valid shapes, and (link guard) every Unicode
code point.
"""

from __future__ import annotations

import contextlib
import importlib.util
import random
import re
import sys
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from types import CodeType

import pytest

from audittrace.media import link_guard, payload_scrub
from audittrace.media.payload_scrub import MEDIA_MARK, scrub_data_url_payloads

#: the former patterns, kept here only as the reference the new code must equal
_OLD_DATA_URL = re.compile(
    r"(data:[A-Za-z0-9.+/-]+(?:;[^;,\"\\\s]+)*;base64,)[A-Za-z0-9+/=]+"
)
_OLD_LINK = re.compile(r"\s*(?:https?|file)://", re.IGNORECASE)


def _old_scrub(value: str) -> str:
    if "base64," not in value:
        return value
    return _OLD_DATA_URL.sub(r"\1" + MEDIA_MARK, value)


class _Steps:
    """Counts the primitive steps of the backward scan through its two seams.

    PROVES: each call of the per-character test and of the per-token head test is counted
    (a token head costs 1 + its length), so a scan that walks back over the same characters
    again for every marker shows as a count that grows faster than the input.
    MISSES: any work that does not go through those two functions (a loop added beside them,
    a second pass, a one-line comprehension) and any work inside one C-level call. The line
    and instruction counters below cover the Python-level part of that; nothing here covers
    the C-level part. Its control test asserts the seams are live, so a scan that no longer
    reaches them (a regular expression) cannot pass this guard vacuously.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        real_char = payload_scrub._is_token_char
        real_data = payload_scrub._data_start

        def char(c: str) -> bool:
            self.count += 1
            return real_char(c)

        def data(token: str) -> int:
            self.count += 1 + len(token)
            return real_data(token)

        monkeypatch.setattr(payload_scrub, "_is_token_char", char)
        monkeypatch.setattr(payload_scrub, "_data_start", data)

    def of(self, text: str) -> int:
        self.count = 0
        scrub_data_url_payloads(text)
        return self.count


#: adversarial shapes, each a function of the size n. The first two are the code-scanning
#: finding's; the others make many markers, long heads, empty tokens, or valid matches.
SHAPES: dict[str, Callable[[int], str]] = {
    "finding-1": lambda n: "base64," + "data:+" + ";data:+" * n + ";base64,QUJD",
    "finding-2": lambda n: "base64," + "data:+;" * n + ";base64,QUJD",
    "one-long-head": lambda n: "data:+;" * n + ";base64,QUJD",
    "empty-tokens": lambda n: ";" * (4 * n) + ";base64,QUJD",
    "many-markers-short-head": lambda n: ("abcdefgh" + ";base64,") * n,
    "many-markers-payload": lambda n: ("x;base64,AAAA") * n,
    "markers-only": lambda n: ";base64," * n,
    "valid-matches": lambda n: "data:image/png;base64,QUJD " * n,
}
SIZES = (2000, 8000)


_TOOL = 3  # sys.monitoring tool id (0 debugger, 1 coverage, 2 profiler, 5 optimizer are taken)


@contextlib.contextmanager
def _line_events(filename: str) -> Iterator[list[int]]:
    """Count the Python line events executed in ``filename`` while the block runs.

    PROVES: every time the interpreter starts a source line of that file counts one event, so a
    Python-level loop whose body is on its own lines costs (iterations x body lines), wherever
    the work hides (in the two seams or outside them). No clock: the count is a property of the
    code and the input.
    MISSES (measured on CPython 3.12, ``test_the_line_counter_is_blind_to_one_line_loops``):
    a loop or comprehension that sits on ONE source line (``sum(1 for _ in t for _ in t)``, a
    one-line nested ``for``) fires a single event whatever its trip count, so a one-line
    quadratic is invisible here; and C-level work (``str.find``, ``str.count``, ``rfind``,
    ``split``) is one event per call whatever the input length. The instruction counter below
    closes the first gap; nothing here closes the second.
    """
    events = sys.monitoring.events
    count = [0]

    def on_line(code: CodeType, line: int) -> None:
        if code.co_filename == filename:
            count[0] += 1

    sys.monitoring.use_tool_id(_TOOL, "media-scan-complexity")
    try:
        sys.monitoring.register_callback(_TOOL, events.LINE, on_line)
        sys.monitoring.set_events(_TOOL, events.LINE)
        yield count
    finally:
        sys.monitoring.set_events(_TOOL, 0)
        sys.monitoring.register_callback(_TOOL, events.LINE, None)
        sys.monitoring.free_tool_id(_TOOL)


def _code_objects(code: CodeType) -> Iterator[CodeType]:
    """``code`` and every code object nested in it (inner functions, comprehensions)."""
    yield code
    for const in code.co_consts:
        if isinstance(const, CodeType):
            yield from _code_objects(const)


def _module_code(module: types.ModuleType) -> list[CodeType]:
    """The code objects of the functions defined in ``module`` (not of what it imports)."""
    roots = [
        value.__code__
        for value in vars(module).values()
        if isinstance(value, types.FunctionType) and value.__module__ == module.__name__
    ]
    return [code for root in roots for code in _code_objects(root)]


@contextlib.contextmanager
def _instruction_events(codes: list[CodeType]) -> Iterator[list[int]]:
    """Count the bytecode instructions executed in ``codes`` while the block runs.

    PROVES: each executed instruction of the given code objects counts one event, so a
    quadratic Python-level loop is seen even when it sits on one source line (the case the
    line counter is blind to). Local events are set per code object, so the objects must be
    listed (``_module_code`` collects the functions of a module and what is nested in them).
    MISSES: work inside one C-level call (``str.count``, ``str.find``, ``re``): that is a
    handful of instructions whatever the input length. A function added to the module AFTER the
    code objects were collected, or code reached only through another module, is not counted.
    """
    events = sys.monitoring.events
    count = [0]

    def on_instruction(code: CodeType, offset: int) -> None:
        count[0] += 1

    sys.monitoring.use_tool_id(_TOOL, "media-scan-complexity")
    try:
        sys.monitoring.register_callback(_TOOL, events.INSTRUCTION, on_instruction)
        for code in codes:
            sys.monitoring.set_local_events(_TOOL, code, events.INSTRUCTION)
        yield count
    finally:
        for code in codes:
            sys.monitoring.set_local_events(_TOOL, code, 0)
        sys.monitoring.register_callback(_TOOL, events.INSTRUCTION, None)
        sys.monitoring.free_tool_id(_TOOL)


def _instructions(text: str) -> int:
    codes = _module_code(payload_scrub)
    with _instruction_events(codes) as count:
        scrub_data_url_payloads(text)
    return count[0]


def _lines(text: str) -> int:
    with _line_events(payload_scrub.__file__) as count:
        scrub_data_url_payloads(text)
    return count[0]


class TestLineEventLinearity:
    """Python-level work of the scan on its own lines, not only what goes through the two seams.

    Bounds: at most 12 line events per input character (plus 200) and x4.4 for x4 the input.
    Blind to one-line loops and to C-level work (see ``_line_events``); the next class and the
    planted-code tests at the end of the file are about exactly that.
    """

    @pytest.mark.parametrize("shape", sorted(SHAPES))
    def test_line_events_stay_below_a_constant_per_character_and_grow_linearly(
        self, shape: str
    ) -> None:
        small, large = (SHAPES[shape](n) for n in SIZES)
        a, b = _lines(small), _lines(large)
        assert a <= 12 * len(small) + 200 and b <= 12 * len(large) + 200, (
            a,
            len(small),
            b,
            len(large),
        )
        assert b <= 4.4 * max(a, 1), (shape, a, b)

    def test_control_the_counter_sees_the_scan(self) -> None:
        assert _lines(SHAPES["markers-only"](SIZES[0])) >= SIZES[0]


#: instruction events per character (the real scan measures about 39 at the worst) and growth
#: for x4 the input; the same predicate judges the real scan and the planted one-liners.
_PER_CHAR = 60
_GROWTH = 4.4


def _linear(a: int, b: int, small: int, large: int) -> bool:
    """True when the counts ``a`` (small input) and ``b`` (large) look linear in the length."""
    return (
        a <= _PER_CHAR * small + 400
        and b <= _PER_CHAR * large + 400
        and b <= _GROWTH * max(a, 1)
    )


class TestInstructionLinearity:
    """Every bytecode instruction of the scan module's functions, one-line loops included.

    PROVES: the Python-level work of ``payload_scrub`` (every function defined there and what
    is nested in it) grows linearly on the adversarial shapes, even where a loop or comprehension
    shares one source line. MISSES: work inside one C-level call; see ``_instruction_events``.
    """

    @pytest.mark.parametrize("shape", sorted(SHAPES))
    def test_instructions_stay_below_a_constant_per_character_and_grow_linearly(
        self, shape: str
    ) -> None:
        small, large = (SHAPES[shape](n) for n in SIZES)
        a, b = _instructions(small), _instructions(large)
        assert _linear(a, b, len(small), len(large)), (shape, a, b)

    def test_control_the_counter_sees_the_scan_and_its_nested_code(self) -> None:
        codes = _module_code(payload_scrub)
        names = {code.co_name for code in codes}
        assert {"scrub_data_url_payloads", "_opening", "_data_start"} <= names
        assert _instructions(SHAPES["markers-only"](SIZES[0])) >= SIZES[0]


def _one_line_quadratic(text: str) -> int:
    return sum(1 for _ in text for _ in text)


def _one_line_c_level_quadratic(text: str) -> int:
    return sum(text.count("a") for _ in text)


def _measure(fn: Callable[[str], int], counter: str) -> tuple[int, int, int, int]:
    small, large = "a" * 500, "a" * 2000
    out = []
    for text in (small, large):
        if counter == "line":
            with _line_events(__file__) as count:
                fn(text)
        else:
            with _instruction_events(list(_code_objects(fn.__code__))) as count:
                fn(text)
        out.append(count[0])
    return out[0], out[1], len(small), len(large)


class TestWhatTheCountersCannotSee:
    """The limits the docstrings state, pinned so that they stay true (G1)."""

    def test_the_line_counter_is_blind_to_one_line_loops(self) -> None:
        a, b, small, large = _measure(_one_line_quadratic, "line")
        assert _linear(a, b, small, large), (
            "a one-line quadratic passes the line counter"
        )
        assert b == a, "one event however many iterations: the count is flat"

    def test_the_instruction_counter_catches_the_one_line_quadratic(self) -> None:
        a, b, small, large = _measure(_one_line_quadratic, "instruction")
        assert not _linear(a, b, small, large), (a, b)
        assert b >= 12 * a, "x4 the input gives about x16 the instructions"

    def test_neither_counter_sees_a_c_level_quadratic(self) -> None:
        for counter in ("line", "instruction"):
            a, b, small, large = _measure(_one_line_c_level_quadratic, counter)
            assert _linear(a, b, small, large), (counter, a, b)

    def test_a_planted_one_liner_in_a_copy_of_the_scan_passes_lines_fails_instructions(
        self, tmp_path: Path
    ) -> None:
        """The neuter: the real scan plus ONE quadratic line, loaded from a temp copy."""
        source = Path(payload_scrub.__file__).read_text(encoding="utf-8")
        head = "def scrub_data_url_payloads(value: str) -> str:"
        anchor = '    if "base64," not in value:\n        return value\n'
        assert source.count(head) == 1 and source.count(anchor) == 1
        planted = source.replace(
            anchor, anchor + "    _ = sum(1 for _a in value for _b in value)\n"
        )
        path = tmp_path / "payload_scrub_planted.py"
        path.write_text(planted, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("payload_scrub_planted", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        text_small, text_large = (SHAPES["many-markers-payload"](n) for n in (200, 800))
        counts = {}
        for kind in ("line", "instruction"):
            row = []
            for text in (text_small, text_large):
                if kind == "line":
                    with _line_events(str(path)) as count:
                        module.scrub_data_url_payloads(text)
                else:
                    with _instruction_events(_module_code(module)) as count:
                        module.scrub_data_url_payloads(text)
                row.append(count[0])
            counts[kind] = row
        sizes = (len(text_small), len(text_large))
        assert _linear(*counts["line"], *sizes), ("line", counts)
        assert not _linear(*counts["instruction"], *sizes), ("instruction", counts)


class TestStructuralLinearity:
    @pytest.mark.parametrize("shape", sorted(SHAPES))
    def test_steps_stay_below_a_constant_per_character_and_grow_linearly(
        self, monkeypatch: pytest.MonkeyPatch, shape: str
    ) -> None:
        steps = _Steps(monkeypatch)
        small, large = (SHAPES[shape](n) for n in SIZES)
        a, b = steps.of(small), steps.of(large)
        assert len(large) > 4 * len(small) * 0.9
        assert a <= 3 * len(small) and b <= 3 * len(large), (
            a,
            len(small),
            b,
            len(large),
        )
        assert b <= 4.4 * max(a, 1), (shape, a, b)

    def test_control_the_seams_are_live_on_the_long_head(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A scan that never reaches the seams (a regular expression) must not pass."""
        steps = _Steps(monkeypatch)
        text = SHAPES["one-long-head"](SIZES[0])
        assert steps.of(text) >= len(text) // 2


class TestTheAdversarialInputsAreCorrectOutputs:
    def test_finding_shape_1_at_112kb_is_scrubbed(self) -> None:
        text = SHAPES["finding-1"](16000)
        assert len(text) > 100_000
        assert scrub_data_url_payloads(text) == text[: -len("QUJD")] + MEDIA_MARK

    def test_finding_shape_2_at_112kb_is_left_alone(self) -> None:
        """``data:+;data:+;...;;base64,``: an empty parameter, so not a data URL (as before)."""
        text = SHAPES["finding-2"](16000)
        assert len(text) > 100_000
        assert scrub_data_url_payloads(text) == text
        small = SHAPES["finding-2"](40)
        assert _old_scrub(small) == small, "control: the former pattern agrees"

    def test_many_valid_urls_are_all_scrubbed(self) -> None:
        out = scrub_data_url_payloads(SHAPES["valid-matches"](5000))
        assert out == ("data:image/png;base64," + MEDIA_MARK + " ") * 5000

    def test_markers_without_a_head_change_nothing(self) -> None:
        for shape in (
            "markers-only",
            "many-markers-short-head",
            "many-markers-payload",
        ):
            text = SHAPES[shape](3000)
            assert scrub_data_url_payloads(text) == text


VALID_SHAPES = [
    ("data:image/png;base64,QUJD", "data:image/png;base64," + MEDIA_MARK),
    (
        "data:image/png;charset=utf-8;base64,QUJD",
        "data:image/png;charset=utf-8;base64," + MEDIA_MARK,
    ),
    ("data:a;b;c;d;base64,QUJD", "data:a;b;c;d;base64," + MEDIA_MARK),
    (
        "x data:image/svg+xml;base64,QUJD= y",
        "x data:image/svg+xml;base64," + MEDIA_MARK + " y",
    ),
    ('"data:image/png;base64,QUJD"', '"data:image/png;base64,' + MEDIA_MARK + '"'),
    ("a,data:image/png;base64,QUJD,b", "a,data:image/png;base64," + MEDIA_MARK + ",b"),
    (
        "see\tdata:image/png;base64,QUJD\n",
        "see\tdata:image/png;base64," + MEDIA_MARK + "\n",
    ),
    ("xxdata:image/png;base64,QUJD", "xxdata:image/png;base64," + MEDIA_MARK),
    ("data:data:image/png;base64,QUJD", "data:data:image/png;base64," + MEDIA_MARK),
    (
        "data:image/png;base64,QUJD;base64,REVG",
        "data:image/png;base64," + MEDIA_MARK + ";base64,REVG",
    ),
    # not data URLs: unchanged
    ("DATA:image/png;base64,QUJD", "DATA:image/png;base64,QUJD"),
    ("data:;base64,QUJD", "data:;base64,QUJD"),
    ("data:image/png;;base64,QUJD", "data:image/png;;base64,QUJD"),
    ("data:image/png;base64,", "data:image/png;base64,"),
    ("x;base64,QUJD", "x;base64,QUJD"),
    ("data:ima ge/png;base64,QUJD", "data:ima ge/png;base64,QUJD"),
]


class TestEqualToTheFormerPattern:
    @pytest.mark.parametrize(("text", "expected"), VALID_SHAPES)
    def test_valid_and_invalid_shapes(self, text: str, expected: str) -> None:
        assert scrub_data_url_payloads(text) == expected
        assert _old_scrub(text) == expected, "control: the former pattern agrees"

    def test_random_strings_equal_the_former_pattern(self) -> None:
        pieces = [
            "data:", ";base64,", "image/png", ";charset=x", "+", ";", ",", " ", '"',
            "\\", "A", "QUJD", "=", "x", ":", "\u00a0", "\x1c", "-", ".", "/", "base64",
            "data", ";;", "\n", "Z9", "\u00e9",
        ]  # fmt: skip
        letters = 'dat:;b64,+ A="x'
        rnd = random.Random(20261121)
        for i in range(40_000):
            if i % 2:
                text = "".join(rnd.choice(pieces) for _ in range(rnd.randint(1, 14)))
            else:
                text = "".join(rnd.choice(letters) for _ in range(rnd.randint(1, 40)))
            assert scrub_data_url_payloads(text) == _old_scrub(text), repr(text)


class TestScrubIsNotTheIdentityAndNotTooBroad:
    def test_a_data_url_payload_is_dropped(self) -> None:
        assert scrub_data_url_payloads("a data:image/png;base64,QUJD b") != (
            "a data:image/png;base64,QUJD b"
        )

    def test_text_that_only_mentions_base64_is_kept(self) -> None:
        text = "the base64, encoding; see x;base64,QUJD and data:text/plain,hello"
        assert scrub_data_url_payloads(text) == text


class TestLinkGuardWithoutARegex:
    WHITESPACE = [
        "\t", "\n", "\x0b", "\x0c", "\r", "\x1c", "\x1d", "\x1e", "\x1f", " ", "\x85",
        "\u00a0", "\u1680", "\u2000", "\u200a", "\u2028", "\u2029", "\u202f", "\u205f",
        "\u3000",
    ]  # fmt: skip
    NOT_WHITESPACE = ["\ufeff", "\u200b", "\u180e", "\u2060", "x", "\u00ad"]

    @pytest.mark.parametrize("space", WHITESPACE)
    def test_leading_unicode_whitespace_is_skipped_as_before(self, space: str) -> None:
        text = space * 3 + "http://127.0.0.1/"
        assert link_guard._is_link(text) is True
        assert _OLD_LINK.match(text) is not None

    @pytest.mark.parametrize("char", NOT_WHITESPACE)
    def test_other_leading_characters_hide_the_link_as_before(self, char: str) -> None:
        text = char + "http://127.0.0.1/"
        assert link_guard._is_link(text) is False
        assert _OLD_LINK.match(text) is None

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("HTTPS://x", True),
            ("FiLe:///etc/passwd", True),
            ("ht\u0074ps://x", True),
            ("http\u017f://x", True),  # long s equals s under the old ignore-case
            ("f\u0131le://x", True),  # dotless i equals i under the old ignore-case
            ("f\u0130le://x", True),
            ("\ufb01le://x", False),  # the fi ligature is NOT f + i for the old pattern
            ("http:/x", False),
            ("httpx://x", False),
            ("ftp://x", False),
            ("", False),
            ("  ", False),
            ("file:/", False),
        ],
    )
    def test_scheme_matching_equals_the_former_pattern(
        self, text: str, expected: bool
    ) -> None:
        assert link_guard._is_link(text) is expected
        assert (_OLD_LINK.match(text) is not None) is expected

    def test_every_code_point_in_every_scheme_position_equals_the_former_pattern(
        self,
    ) -> None:
        wrong: list[str] = []
        for cp in range(sys.maxunicode + 1):
            char = chr(cp)
            for text in (
                char + "http://x",
                char + "ttp://x",
                "ht" + char + "ps://x",
                "fi" + char + "e://x",
            ):
                if (_OLD_LINK.match(text) is not None) != link_guard._is_link(text):
                    wrong.append(repr(text))
        assert wrong == []

    def test_a_long_whitespace_run_is_a_correct_answer(self) -> None:
        assert link_guard._is_link(" " * 200_000 + "file://x") is True
        assert link_guard._is_link(" " * 200_000 + "x") is False
