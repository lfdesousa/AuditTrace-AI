"""The refusal a whole-document read raises instead of returning partial text."""

from __future__ import annotations


class SemanticDocumentIncompleteError(Exception):
    """The chunks of a folded document do not add up to the document its fold
    wrote: one is missing, one comes from another fold, or the reassembled text
    does not hash to the fold's stamp. Routes map it to 409."""
