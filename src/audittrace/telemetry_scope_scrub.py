"""Tempo-path scope scrubber (WP-13, ADDENDUM-2 B1).

Our ``langfuse-sdk`` tracer carries ``public_key`` as an
*instrumentation-scope* attribute: that attribute is the Langfuse SDK's own
routing contract (``LangfuseSpanProcessor`` exports a ``langfuse-sdk`` span only
when ``instrumentation_scope.attributes["public_key"]`` equals its own key), so
it must stay on the tracer. The side effect is that every OTLP batch to Tempo
repeats the value, and ``ScrubbingSpanProcessor`` scrubs span/event/link
attributes, not scope attributes.

``ScopeScrubbingSpanExporter`` closes that gap at export time on the Tempo path
only: it forwards COPIES of the spans whose scope no longer carries the listed
attribute keys. It never mutates the span it was handed or the (shared) scope
object, so the Langfuse processor, which holds the original span, still sees the
attribute.

Falsifiability: ``tests/test_langfuse_key_scope_scrub.py`` runs the real
``init_telemetry`` and asserts (AC-13.T) that no value shaped like a Langfuse key
reaches the Tempo chain, and (AC-13.L) that a real ``LangfuseSpanProcessor``
still receives the attribute. Removing the wrap turns AC-13.T red; dropping the
attribute from the tracer turns AC-13.L red.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Sequence
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

logger = logging.getLogger(__name__)

#: Scope-attribute keys removed from spans on the Tempo path.
SCRUBBED_SCOPE_ATTRIBUTE_KEYS: tuple[str, ...] = ("public_key",)


class ScopeScrubbingSpanExporter(SpanExporter):
    """Wrap ``inner`` and forward spans whose scope lacks the scrubbed keys."""

    def __init__(
        self,
        inner: SpanExporter,
        keys: Sequence[str] = SCRUBBED_SCOPE_ATTRIBUTE_KEYS,
    ) -> None:
        self.inner = inner
        self._keys = frozenset(keys)

    def _scrubbed(self, span: ReadableSpan) -> ReadableSpan:
        scope = span.instrumentation_scope
        attrs = getattr(scope, "attributes", None)
        if scope is None or not attrs or self._keys.isdisjoint(attrs):
            return span
        kept = {k: v for k, v in attrs.items() if k not in self._keys}
        clone = copy.copy(span)  # shallow: attributes/events stay shared, read-only
        clone._instrumentation_scope = InstrumentationScope(
            scope.name,
            scope.version,
            scope.schema_url,
            kept or None,
        )
        return clone

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self.inner.export([self._scrubbed(s) for s in spans])

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        flush: Any = getattr(self.inner, "force_flush", None)
        if not callable(flush):
            return True
        done = flush(timeout_millis)
        return True if done is None else bool(done)  # SDK base returns None = ok
