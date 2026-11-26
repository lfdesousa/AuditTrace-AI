"""The chain's console message-id predicate (CONSOLE-AUDIT-EVIDENCE, ADDENDUM-A1
section 3.2), in one place for the BFF.

The same predicate runs at four sites across the chain (the memory server's
header read and path parameter, this BFF's chat-refusal record and evidence
route, the fork's route). It is applied with ``fullmatch`` (``re.match`` would
accept ``"abc\\n"``), refuses ``"."`` and ``".."``, and has no brace, so an
unresolved ``{{LIBRECHAT_BODY_MESSAGEID}}`` placeholder never matches.
"""

from __future__ import annotations

import re

_MESSAGE_ID = re.compile(r"(?!\.{1,2}$)[A-Za-z0-9._:-]{1,255}")


def is_valid_message_id(value: str) -> bool:
    """True when ``value`` is a well-formed console message id."""
    return _MESSAGE_ID.fullmatch(value) is not None
