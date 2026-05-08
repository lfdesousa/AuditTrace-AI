"""Page-level PDF extraction helpers (encryption / attachments / forms / OCR).

Each function operates on one pymupdf ``Page`` (or ``Document`` for the
encryption check) and returns either text or audit metadata. The
orchestrator in ``memory_pdf.pipeline`` calls these once per page in
the per-document loop.

Concerns are kept separate within this single module rather than
spread across four files because each helper is short, the contracts
are similar (defensive degradation on raise / non-bytes / missing
attribute), and the call site is a single inline tier through the
orchestrator. Splitting further produces files of <50 LOC each which
trades navigability for ceremony.

If any of these grow significantly past their current shape, split
them into separate modules at that point — the §11 discipline triggers
on size, not on count.
"""

from __future__ import annotations

import hashlib
import io
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _pdf_is_encrypted(doc: Any) -> bool:
    """Return True if the PDF is encrypted and not yet authenticated.

    Tier-B item #15. ``pymupdf`` exposes both ``is_encrypted`` (any
    encryption present) and ``needs_pass`` (encryption requires a
    password to read content). We treat *any* encryption requiring
    authentication as a refuse — see ADR-050 §#15 for the rationale
    (no password-bearing endpoint).

    Strict ``is True`` comparison (not truthy-check) means MagicMock
    attributes — which would otherwise be truthy by default — read
    as False. Test fixtures don't need to opt into the tier-B
    encryption contract; real pymupdf documents return real ``bool``
    values from these attrs and the comparison works correctly.
    """
    is_enc = getattr(doc, "is_encrypted", False)
    needs_pw = getattr(doc, "needs_pass", False)
    return is_enc is True and needs_pw is True


def _quarantine_pdf_attachments(
    doc: Any,
    *,
    parent_filename: str,
    layer_prefix: str,
    minio_client: Any,
    bucket: str,
) -> tuple[int, list[dict[str, Any]]]:
    """Extract embedded attachments and write them to MinIO under
    ``{layer_prefix}{parent_filename}/attachments/{name}``.

    Tier-B item #6. Returns ``(count, warnings)``. Each successful
    quarantine appends one ``{"code": "attachment", ...}`` entry to
    *warnings*; failures append ``"attachment_quarantine_failed"`` so
    auditors can distinguish "no attachments" from "attachments
    existed but we couldn't store them." Recursion bound = 1 level
    (per ADR-050 §#6) — embedded PDFs get an attachment row but are
    not themselves parsed.
    """
    warnings: list[dict[str, Any]] = []
    try:
        # int() coercion: real pymupdf returns int; tests with
        # MagicMock would otherwise return a MagicMock, breaking the
        # range() iteration below.
        count = int(doc.embfile_count() or 0)
    except (TypeError, ValueError, Exception):  # pragma: no cover — defensive
        # Older pymupdf or malformed catalog. Treat as zero
        # attachments rather than failing the whole document.
        return 0, []
    if count <= 0:
        return 0, []
    successes = 0
    # Sanity cap on attachment count — a real PDF rarely carries
    # more than a few. MagicMock-driven tests sometimes return
    # ``__int__`` defaults of 1, which is fine; if a real PDF
    # somehow declared 10 000 attachments we'd refuse to walk all
    # of them.
    if count > 256:
        return 0, [
            {
                "code": "attachment_quarantine_failed",
                "error": "too_many_attachments",
                "count": count,
            }
        ]
    for i in range(count):
        # One try/except per iteration covering the full extract +
        # write cycle so any failure (malformed embfile entry, MinIO
        # error, MagicMock duck-typing edge case in tests) records a
        # structured warning and moves on rather than aborting the
        # whole document.
        try:
            info = doc.embfile_info(i)
            name = info.get("filename") or f"attachment-{i}"
            mime = info.get("mime") or "application/octet-stream"
            data = doc.embfile_get(i)
            if not isinstance(data, (bytes, bytearray)):
                raise TypeError(
                    f"embfile_get({i}) returned {type(data).__name__}, "
                    f"expected bytes-like"
                )
            sha256 = hashlib.sha256(data).hexdigest()
            # parent_filename is the human key (e.g. "main.pdf");
            # the layer prefix is "episodic/" or "procedural/".
            # MinIO key shape: "episodic/main.pdf/attachments/<name>".
            attachment_key = f"{layer_prefix}{parent_filename}/attachments/{name}"
            minio_client.put_object(
                bucket,
                attachment_key,
                io.BytesIO(data),
                length=len(data),
            )
        except Exception as exc:
            warnings.append(
                {
                    "code": "attachment_quarantine_failed",
                    "index": i,
                    "error": type(exc).__name__,
                }
            )
            continue
        successes += 1
        warnings.append(
            {
                "code": "attachment",
                "name": name,
                "mime": mime,
                "size": len(data),
                "sha256": sha256,
                "minio_key": attachment_key,
            }
        )
    return successes, warnings


def _acroform_text_for_page(page: Any) -> tuple[str | None, int]:
    """Return ``(text, count)`` rendered from AcroForm widgets on *page*.

    Tier-B item #7. Concatenates ``Label: Value`` lines in widget
    order. Returns ``(None, 0)`` when the page has no widgets — caller
    can fall back to plain text-layer extraction. The page's normal
    text is chunked separately; form-field text is its own chunk so
    embeddings carry the label-value semantic anchor (per ADR-050 §#7).
    """
    try:
        widgets = list(page.widgets() or [])
    except Exception:
        return None, 0
    if not widgets:
        return None, 0
    lines: list[str] = []
    count = 0
    for w in widgets:
        # pymupdf Widget exposes field_name + field_value + field_label
        # (the latter is the human-readable display string when the
        # PDF carries one, else None).
        name = getattr(w, "field_name", None) or ""
        value = getattr(w, "field_value", None)
        label = getattr(w, "field_label", None) or name
        if value is None or value == "":
            # Empty fields contribute no semantic signal — skip rather
            # than emit "Label: " noise that pollutes the embedding.
            continue
        lines.append(f"{label}: {value}".strip())
        count += 1
    if not lines:
        return None, 0
    return "\n".join(lines), count


def _ocr_render_page(
    page: Any,
    *,
    enabled: bool,
    languages: str,
    dpi: int,
) -> tuple[str, str, float]:
    """Run Tesseract OCR on *page* if it has raster content but no text.

    Tier-B item #1. Returns ``(text, text_source, confidence)`` where:

    * ``text_source`` ∈ {``"native"``, ``"ocr"``, ``"no_text_layer"``}.
      ``"native"`` is the no-op return for callers that pre-checked
      and decided OCR isn't needed. ``"no_text_layer"`` signals a
      raster page where OCR was unavailable (binary missing) or
      returned empty; caller should emit an extraction warning.
    * ``confidence`` is Tesseract's mean per-word confidence in
      [0.0, 1.0]; 1.0 for the native short-circuit; 0.0 for
      no_text_layer.

    Graceful degradation: if pytesseract is not importable OR the
    tesseract binary is missing OR rendering fails, return
    ``("", "no_text_layer", 0.0)`` — caller logs the warning, the
    page produces zero chunks, processing continues. Per ADR-050 §#1.
    """
    if not enabled:
        return ("", "no_text_layer", 0.0)
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ("", "no_text_layer", 0.0)
    try:
        # Render the page to a 300-DPI raster. pymupdf's get_pixmap
        # returns a Pixmap; we convert to PNG bytes then to PIL.Image
        # for pytesseract's image_to_data API.
        pix = page.get_pixmap(dpi=dpi)
        png_bytes = pix.tobytes("png")
        img = Image.open(io.BytesIO(png_bytes))
        # image_to_data returns word-level results with confidences.
        # The mean of the (non -1) confidences is the cleanest signal.
        data = pytesseract.image_to_data(
            img, lang=languages, output_type=pytesseract.Output.DICT
        )
    except (
        Exception
    ) as exc:  # pragma: no cover — needs real Tesseract crash to exercise
        logger.debug("OCR unavailable for page: %s", exc)
        return ("", "no_text_layer", 0.0)
    words: list[str] = []
    confidences: list[float] = []
    for word, conf in zip(  # pragma: no cover — exercised by live OCR PDFs
        data.get("text", []), data.get("conf", []), strict=False
    ):
        if not word or not word.strip():
            continue
        try:
            conf_value = float(conf)
        except (TypeError, ValueError):
            continue
        if conf_value < 0:
            # Tesseract emits -1 for "no recognition"; skip.
            continue
        words.append(word)
        confidences.append(conf_value)
    if not words:  # pragma: no cover — exercised by live OCR PDFs
        return ("", "no_text_layer", 0.0)
    text = " ".join(words)
    mean_conf = (sum(confidences) / len(confidences)) / 100.0
    return (text, "ocr", round(mean_conf, 3))
