"""Counter for PDF signature classification outcomes (spec #460 §2.5).

``audittrace_pdf_signature_checks_total{status}`` increments once per
classified document, labelled by the closed signature taxonomy. A spike in
``check_error`` ("our call into the validator failed") is the operator-visible
signal that a classifier regression like #366 (every signed PDF reading
``check_failed``) has returned. ``metrics.get_meter`` is a no-op until a real
``MeterProvider`` is configured, so this is inert when
``AUDITTRACE_OTLP_ENDPOINT`` is unset.

NO PII in labels: ``{status}`` only, never a filename, user or document hash.
"""

from __future__ import annotations

from opentelemetry import metrics

_meter = metrics.get_meter("audittrace.pdf_signature")

_pdf_signature_checks_total = _meter.create_counter(
    name="audittrace_pdf_signature_checks_total",
    description=(
        "PDF signature classifications by outcome. Labels: status (the "
        "closed signature taxonomy, 10 codes). No PII."
    ),
)


def emit_signature_check(*, status: str) -> None:
    """Increment ``audittrace_pdf_signature_checks_total{status}`` by 1."""
    _pdf_signature_checks_total.add(1, {"status": status})
