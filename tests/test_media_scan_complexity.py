"""LIBRECHAT-IMAGE-SLICE round 4 -- the scans of caller text are linear, and equal to the former patterns.

A code-scanning finding on the pull request: the data-URL scrub was a regular expression that
backtracked quadratically on caller text (``base64,`` followed by ``data:+`` and ``;data:+``
repeated 16000 times, 112 KB, ran for about 20 s). It is now a scan anchored on the literal
``;base64,`` that validates backwards. The link guard's pattern is replaced by a string check.

No assertion here reads a clock (a wall-clock bound is a flake). Linearity is proved
STRUCTURALLY: the scan's primitive steps are counted through two seams (the per-character
test of the backward walk and the per-token head test), on adversarial inputs at two sizes:
the count stays below a constant times the input length and grows linearly with it. The
112 KB adversarial inputs are also ordinary inputs that must give the correct output.
Equality with the former patterns is checked on random strings, the valid shapes, and (link
guard) every Unicode code point.
"""

from __future__ import annotations

import random
import re
import sys
from collections.abc import Callable

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
    """Counts the primitive steps of the backward scan through its two seams."""

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
