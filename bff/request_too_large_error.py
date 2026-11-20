"""Raised inside the body stream when a BFF request passes its ceiling."""

from __future__ import annotations


class RequestTooLargeError(Exception):
    """The request body is larger than the BFF accepts on this path."""
