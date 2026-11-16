"""Raised inside the body stream when a memory request passes its ceiling."""

from __future__ import annotations


class MemoryRequestTooLargeError(Exception):
    """The request body is larger than any acceptable document could need."""
