"""The last resort's frame chain (gate r2 A9): class and frames, never text.

Neuters: log ``str(exc)`` in the chain -> ``test_the_chain_carries_no_text``;
drop the ``MAX_FRAMES`` bound -> ``test_a_runaway_recursion_keeps_the_innermost_frames``.
"""

from __future__ import annotations

import re

from bff.chat_last_resort import MAX_FRAMES, frame_chain


def _recurse(depth: int) -> None:
    if depth:
        _recurse(depth - 1)
    raise RuntimeError("secret-bearing text")


def _chain_of(depth: int) -> str:
    try:
        _recurse(depth)
    except RuntimeError as exc:
        return frame_chain(exc)
    raise AssertionError("unreachable")


def test_the_chain_names_the_class_and_every_frame_outermost_first() -> None:
    chain = _chain_of(1)

    assert re.fullmatch(r"RuntimeError at (\S+:\d+)( > \S+:\d+)+", chain), chain
    frames = chain.removeprefix("RuntimeError at ").split(" > ")
    assert [frame.split(":")[0] for frame in frames] == [__file__.rsplit("/", 1)[1]] * 3


def test_the_chain_carries_no_text() -> None:
    assert "secret" not in _chain_of(1)


def test_a_runaway_recursion_keeps_the_innermost_frames() -> None:
    chain = _chain_of(MAX_FRAMES * 3)

    frames = chain.removeprefix("RuntimeError at ").split(" > ")
    assert frames[0] == "..."
    assert len(frames) == MAX_FRAMES + 1
