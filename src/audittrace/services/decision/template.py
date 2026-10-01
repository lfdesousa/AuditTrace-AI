"""tev1 choice-prompt template (#459 WU-459-1).

Renders WITHOUT ``transformers`` (no new heavy dependency). The shape is the
tev1 chat template with thinking disabled, as measured from the model's
``apply_chat_template(enable_thinking=False, add_generation_prompt=True)``::

    <|im_start|>user\\n{content}<|im_end|>\\n<|im_start|>assistant\\n<think>\\n\\n</think>\\n\\n

A vendored golden fixture pins byte equality (T4).

The client never sends this STRING to the runtime. It sends token ids built
by :func:`splice_prompt_ids`: user-influenced text is tokenised in ONE call
with ``parse_special:false`` and the template's special-token ids are placed
around it, so user text can never cross the chat-template boundary.
Rendering and splicing share the same constants below, so they cannot drift.

Reconstruction note: ``input_sha256`` is computed over TOKEN IDS, so a third
party needs the pinned model's TOKENIZER (the same GGUF digest) in addition
to this template to reproduce it. ``rendered_sha256`` is debug/golden only
and is not recorded on the audit row.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

TEMPLATE_ID = "tev1-choice-v1"

# sha256 of the model's upstream ``chat_template.jinja`` (measured; recorded on
# the row as provenance only; it is NOT the hash of this module's template).
UPSTREAM_CHAT_TEMPLATE_SHA256 = (
    "d78de6bee4c952ca3145eb161921560a6ede7b59a34e7e4be815f3c5386b4364"
)

# Special tokens (single control ids on the pinned tokenizer).
IM_START = "<|im_start|>"
IM_END = "<|im_end|>"
THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

# Ordinary-text pieces of the template (tokenised with parse_special:false).
USER_ROLE = "user\n"
ASSISTANT_ROLE = "assistant\n"
NEWLINE = "\n"
THINK_BODY = "\n\n"
AFTER_THINK = "\n\n"

# User-content literals.
STATE_HEAD = "State:\n"
QUESTION_HEAD = "\n\nQuestion: "
OPTIONS_HEAD = "\nOptions:\n"
OPTION_SEP = "\n"
INSTRUCTION = "\nAnswer with the option letter only."

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWX"  # 24 options max
MIN_OPTIONS = 2
MAX_OPTIONS = len(LETTERS)

# Every special token the template places itself (resolved with
# parse_special:true; each must be exactly one id).
TEMPLATE_SPECIAL_TOKENS = (IM_START, IM_END, THINK_OPEN, THINK_CLOSE)

# Every ordinary-text piece the template places itself.
TEMPLATE_TEXT_PIECES = (USER_ROLE, NEWLINE, ASSISTANT_ROLE, THINK_BODY)

# The fixed list whose ids are DENIED in user content (SF-1). On the pinned
# tokenizer ``parse_special:false`` splits the role tokens but still returns
# <think>, </think>, <tool_call>, </tool_call> as single control ids, so the
# denylist is checked on the tokenised user content, not assumed.
CONTROL_STRINGS = (
    "<|im_start|>",
    "<|im_end|>",
    "<|endoftext|>",
    "<think>",
    "</think>",
    "<tool_call>",
    "</tool_call>",
)

# The sha256 of OUR template definition (format pieces + special-token names),
# NOT of the upstream jinja. Changing any piece changes this id; the version
# id ``TEMPLATE_ID`` must then be bumped (a test pins the pair).
TEMPLATE_SOURCE = json.dumps(
    {
        "special": list(TEMPLATE_SPECIAL_TOKENS),
        "text": [USER_ROLE, NEWLINE, ASSISTANT_ROLE, THINK_BODY, AFTER_THINK],
        "content": [
            STATE_HEAD,
            QUESTION_HEAD,
            OPTIONS_HEAD,
            OPTION_SEP,
            INSTRUCTION,
            "{letter}. {option}",
        ],
        "letters": LETTERS,
    },
    sort_keys=True,
    ensure_ascii=True,
    separators=(",", ":"),
)
TEMPLATE_SHA256 = hashlib.sha256(TEMPLATE_SOURCE.encode("utf-8")).hexdigest()


def _check_options(options: Sequence[str]) -> None:
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        raise ValueError(
            f"need {MIN_OPTIONS}..{MAX_OPTIONS} options, got {len(options)}"
        )


def build_user_content(state: str, question: str, options: Sequence[str]) -> str:
    """The user-turn content exactly as Phase 0 built it."""
    _check_options(options)
    lines = OPTION_SEP.join(f"{LETTERS[i]}. {opt}" for i, opt in enumerate(options))
    return (
        f"{STATE_HEAD}{state}{QUESTION_HEAD}{question}{OPTIONS_HEAD}{lines}"
        f"{INSTRUCTION}"
    )


def wrap_chat_template(content: str) -> str:
    """Wrap user ``content`` in the tev1 chat template (thinking disabled)."""
    return (
        f"{IM_START}{USER_ROLE}{content}{IM_END}{NEWLINE}"
        f"{IM_START}{ASSISTANT_ROLE}{THINK_OPEN}{THINK_BODY}{THINK_CLOSE}{AFTER_THINK}"
    )


def render_choice_prompt(state: str, question: str, options: Sequence[str]) -> str:
    """User content wrapped in the tev1 chat template (thinking disabled).

    Debug/golden use only: the client sends token ids, not this string.
    """
    return wrap_chat_template(build_user_content(state, question, options))


def splice_prompt_ids(
    special_ids: Mapping[str, int],
    piece_ids: Mapping[str, Sequence[int]],
    content_ids: Sequence[int],
) -> list[int]:
    """Place the template's ids around the user-content ids.

    ``content_ids`` MUST come from ONE ``parse_special:false`` tokenisation of
    the whole user content (tokenising it piece by piece changes the ids).
    """
    ids: list[int] = [special_ids[IM_START], *piece_ids[USER_ROLE], *content_ids]
    ids += [special_ids[IM_END], *piece_ids[NEWLINE]]
    ids += [special_ids[IM_START], *piece_ids[ASSISTANT_ROLE]]
    ids += [special_ids[THINK_OPEN], *piece_ids[THINK_BODY]]
    ids += [special_ids[THINK_CLOSE], *piece_ids[AFTER_THINK]]
    return ids


def input_sha256(token_ids: Sequence[int]) -> str:
    """sha256 of the canonical JSON of the token-id list (what the model saw)."""
    blob = json.dumps(list(token_ids), separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def rendered_sha256(rendered: str) -> str:
    """sha256 of the rendered string (debug/golden only; not on the row)."""
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
