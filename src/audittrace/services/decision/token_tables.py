"""Vendored added-token tables of the pinned decision models (#459 WU-459-1).

Why this exists: ``/tokenize`` with ``parse_special:false`` still returns some
added tokens as single ids (measured live: ``<tool_call>``, ``</tool_call>``,
``<tool_response>``, ``</tool_response>``, ``<think>``, ``</think>``), and the
tokenizer config's own ``special`` flag does NOT predict which ones. A guard
built from a hand-picked list missed ``<tool_response>``. The denylist is
therefore EVERY added token of the pinned model, special or not.

The table is keyed by the sha256 of the GGUF file the deployment is pinned to
(``decision_model_digest``). A model with no table here has no denylist, and
therefore may not take decisions: the config validator (and the client
constructor) refuse it, fail closed.

Source (the private local path is deliberately not recorded):
``HF togethercomputer/Tev1-0.8B-experimental @ 6bb2dff14b38fea90ddb14d870166ccaf77374e9``, file ``tokenizer_config.json`` (key
``added_tokens_decoder``), sha256 ``4ed764fafae09ed7390f85ab1ddc28baa34a4805b4119048ba8d922abaaebcaf``.
Regenerate by re-reading that file; a test pins this table.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class AddedToken:
    """One entry of ``added_tokens_decoder`` (the id is the table key)."""

    content: str
    special: bool


@dataclass(frozen=True)
class AddedTokenTable:
    """The added tokens of one pinned model and where they came from."""

    source: str
    tokenizer_config_sha256: str
    tokens: Mapping[int, AddedToken]

    @property
    def ids(self) -> frozenset[int]:
        return frozenset(self.tokens)

    @property
    def contents(self) -> tuple[str, ...]:
        return tuple(token.content for token in self.tokens.values())


_TEV1_08B_F16_TOKENS: Mapping[int, AddedToken] = MappingProxyType(
    {
        248044: AddedToken("<|endoftext|>", special=True),
        248045: AddedToken("<|im_start|>", special=True),
        248046: AddedToken("<|im_end|>", special=True),
        248047: AddedToken("<|object_ref_start|>", special=True),
        248048: AddedToken("<|object_ref_end|>", special=True),
        248049: AddedToken("<|box_start|>", special=True),
        248050: AddedToken("<|box_end|>", special=True),
        248051: AddedToken("<|quad_start|>", special=True),
        248052: AddedToken("<|quad_end|>", special=True),
        248053: AddedToken("<|vision_start|>", special=True),
        248054: AddedToken("<|vision_end|>", special=True),
        248055: AddedToken("<|vision_pad|>", special=True),
        248056: AddedToken("<|image_pad|>", special=True),
        248057: AddedToken("<|video_pad|>", special=True),
        248058: AddedToken("<tool_call>", special=False),
        248059: AddedToken("</tool_call>", special=False),
        248060: AddedToken("<|fim_prefix|>", special=False),
        248061: AddedToken("<|fim_middle|>", special=False),
        248062: AddedToken("<|fim_suffix|>", special=False),
        248063: AddedToken("<|fim_pad|>", special=False),
        248064: AddedToken("<|repo_name|>", special=False),
        248065: AddedToken("<|file_sep|>", special=False),
        248066: AddedToken("<tool_response>", special=False),
        248067: AddedToken("</tool_response>", special=False),
        248068: AddedToken("<think>", special=False),
        248069: AddedToken("</think>", special=False),
        248070: AddedToken("<|audio_start|>", special=True),
        248071: AddedToken("<|audio_end|>", special=True),
        248072: AddedToken("<tts_pad>", special=True),
        248073: AddedToken("<tts_text_bos>", special=True),
        248074: AddedToken("<tts_text_eod>", special=True),
        248075: AddedToken("<tts_text_bos_single>", special=True),
        248076: AddedToken("<|audio_pad|>", special=True),
    }
)

ADDED_TOKEN_TABLES: Mapping[str, AddedTokenTable] = MappingProxyType(
    {
        # f16 GGUF of tev1-0.8B (Phase-0 recorded sha256).
        "4f8a3d7fc2c8eda2601751ace44690ba1080e508842df88644cedcc08af82cdf": AddedTokenTable(
            source="HF togethercomputer/Tev1-0.8B-experimental @ 6bb2dff14b38fea90ddb14d870166ccaf77374e9",
            tokenizer_config_sha256="4ed764fafae09ed7390f85ab1ddc28baa34a4805b4119048ba8d922abaaebcaf",
            tokens=_TEV1_08B_F16_TOKENS,
        ),
    }
)


def table_for(model_digest: str) -> AddedTokenTable | None:
    """The vendored table for ``model_digest``, or ``None`` (fail closed)."""
    return ADDED_TOKEN_TABLES.get(model_digest)
