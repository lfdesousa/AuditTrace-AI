"""Closed question registry (#459 WU-459-1).

The registry is AUTHORITATIVE: ``decide(state, question_id)`` takes no
free-text question or options. The question and option TEXT live here
(versioned by ``question_id``); an audit row records only ``question_id``
plus the two hashes below, which act as a registry-drift detector. A
reconstruction rebuilds the text from ``(question_id, template_id)`` plus the
state taken from the interaction.

``memory_layer_v1`` options deliberately MIRROR the recall tools the Qwen
loop can call (``recall_decisions``, ``recall_skills``,
``recall_recent_sessions``, ``recall_semantic``) so shadow-mode agreement is
measurable one to one. There is no separate "session" option.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class Question:
    """One registry entry: fixed question text and fixed option texts."""

    question_id: str
    text: str
    options: tuple[str, ...]

    @property
    def question_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def options_sha256(self) -> str:
        # Canonical JSON of the ordered option list (order is the letter map).
        blob = json.dumps(list(self.options), ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


QUESTIONS: Mapping[str, Question] = MappingProxyType(
    {
        "memory_layer_v1": Question(
            question_id="memory_layer_v1",
            text="Which memory layer is most relevant to answer this?",
            options=(
                "none",
                "episodic (decision records)",
                "procedural (skills)",
                "conversational (session history)",
                "semantic (documents)",
            ),
        )
    }
)


def get_question(question_id: object) -> Question | None:
    """Return the registry entry, or ``None`` for anything not registered."""
    if not isinstance(question_id, str):
        return None
    return QUESTIONS.get(question_id)
