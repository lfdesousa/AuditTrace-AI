"""Who is the mesh peer on the other end of this connection?

Functions only. Inside the mesh every workload's sidecar terminates mutual TLS
and tells the application who connected, in the ``X-Forwarded-Client-Cert``
header: one element per hop, the element of the IMMEDIATE peer appended last
by this workload's own sidecar (Envoy ``APPEND_FORWARD``, Istio's setting for
inbound sidecar listeners). Only that last element is the sidecar's own
statement; everything before it travelled with the request. A forwarded
element could leave a double quote open and so swallow the comma that separates
it from the sidecar's own element; a header whose quote state is not closed at
the end is therefore refused outright (no identity), never partly read.

This is trustworthy only where the sidecar terminates STRICT mutual TLS: over
plaintext the sidecar appends nothing and a caller's own header would stand.
The chart sets the consuming switch only together with STRICT mode.
"""

from __future__ import annotations

HEADER = "x-forwarded-client-cert"


def _split(text: str, separator: str) -> list[str]:
    """Split on ``separator`` outside double quotes (a Subject may hold both).

    Raises ``ValueError`` when a quote is still open at the end: an element a
    caller forwarded with an open quote would otherwise swallow the comma, and
    with it the element this workload's own sidecar appended.
    """
    parts: list[str] = []
    current: list[str] = []
    quoted = escaped = False
    for char in text:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == separator and not quoted:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    if quoted:
        raise ValueError("unbalanced quote")
    parts.append("".join(current))
    return parts


def peer_spiffe_id(header: str | None) -> str | None:
    """The single URI of the LAST element, or ``None`` when there is not
    exactly one (no header, no URI, several URIs) or the header leaves a quote
    open (an earlier element cannot swallow the sidecar's own)."""
    if not header:
        return None
    try:
        last = _split(header, ",")[-1]
        pairs = _split(last, ";")
    except ValueError:
        # An open quote: the header cannot be read, so it names nobody.
        return None
    uris = [
        pair.split("=", 1)[1].strip().strip('"')
        for pair in pairs
        if pair.strip().lower().startswith("uri=")
    ]
    return uris[0] if len(uris) == 1 and uris[0] else None
