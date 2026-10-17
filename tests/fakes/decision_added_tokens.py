"""LITERAL added-token list of the pinned model, for the #459 tests.

Deliberately NOT derived from ``services.decision.token_tables``: tests that
parametrise from the module under test lose their own parameter when a neuter
removes an entry (an instrument reading the same source as what it checks).
One pin test compares this list with the vendored table.

Source: the pinned model's ``added_tokens_decoder`` (33 entries, ids
248044-248076; 21 ``special:true``, 12 ``special:false``). ``LEAKY`` is what
was MEASURED live (b10288): the added tokens that still come back as ONE id
with ``parse_special:false``. It is not derivable from the config's flags.
"""

from __future__ import annotations

ALL_ADDED: tuple[tuple[int, str], ...] = (
    (248044, "<|endoftext|>"),
    (248045, "<|im_start|>"),
    (248046, "<|im_end|>"),
    (248047, "<|object_ref_start|>"),
    (248048, "<|object_ref_end|>"),
    (248049, "<|box_start|>"),
    (248050, "<|box_end|>"),
    (248051, "<|quad_start|>"),
    (248052, "<|quad_end|>"),
    (248053, "<|vision_start|>"),
    (248054, "<|vision_end|>"),
    (248055, "<|vision_pad|>"),
    (248056, "<|image_pad|>"),
    (248057, "<|video_pad|>"),
    (248058, "<tool_call>"),
    (248059, "</tool_call>"),
    (248060, "<|fim_prefix|>"),
    (248061, "<|fim_middle|>"),
    (248062, "<|fim_suffix|>"),
    (248063, "<|fim_pad|>"),
    (248064, "<|repo_name|>"),
    (248065, "<|file_sep|>"),
    (248066, "<tool_response>"),
    (248067, "</tool_response>"),
    (248068, "<think>"),
    (248069, "</think>"),
    (248070, "<|audio_start|>"),
    (248071, "<|audio_end|>"),
    (248072, "<tts_pad>"),
    (248073, "<tts_text_bos>"),
    (248074, "<tts_text_eod>"),
    (248075, "<tts_text_bos_single>"),
    (248076, "<|audio_pad|>"),
)

ALL_STRINGS: tuple[str, ...] = tuple(s for _, s in ALL_ADDED)
ID_OF: dict[str, int] = {s: i for i, s in ALL_ADDED}

LEAKY: tuple[str, ...] = (
    "<tool_call>",
    "</tool_call>",
    "<tool_response>",
    "</tool_response>",
    "<think>",
    "</think>",
)
