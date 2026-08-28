"""Unit tests for the SDLC-ADR-005 Layer-0 build-record schema (WU-1).

Pure, network-free: :mod:`scripts.deploy.build_record` never touches the
memory server. Every guard is exercised twice per the ADR-049 discipline —
once proving the RED behaviour (missing/empty field raises) and once proving
GREEN (a complete record validates) — and each parametrized "missing field"
case names exactly the one field it removed, so a guard collapsing to "any
field missing raises the same generic error" would still be caught by the
``exc.value.missing_fields == [field]`` assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.deploy.build_record import (
    REQUIRED_FIELDS,
    BuildRecordValidationError,
    _extract_front_matter,
    _is_empty,
    _is_existing_file,
    validate_build_record,
)

_LONG_FIRST_SEGMENT_TEXT = ("word " * 60).strip() + "\nsecond line of the record"

COMPLETE_FIELDS = {
    "spec_ref": "sdlc/specs/2026-08-28-SPEC-adr059-mechanical-enforcement.md "
    "| 0b0cdd4d-04c3-428f-ab9d-37b47429c381/episodic/"
    "2026-08-28-SPEC-adr059-mechanical-enforcement.md",
    "spec_hash": "sha256:abc123",
    "recall_evidence": "query='adr059 layer0' -> 25 items",
    "log_key": "0b0cdd4d-04c3-428f-ab9d-37b47429c381/episodic/build-record.md",
    "index_status": "indexed: 3 chunks",
    "branch": "feat/sdlc-adr059-build-record-schema",
    "commit": "0123456789abcdef0123456789abcdef01234567",
    "gates": "make test: 640 passed, 0 skipped, 91.2% coverage; helm-lint: n/a",
}


def _front_matter_block(fields: dict[str, str]) -> str:
    lines = [f"{key}: {value!r}" for key, value in fields.items()]
    return "---\n" + "\n".join(lines) + "\n---\n\n# Build record\n\nbody text.\n"


def _complete_record_text() -> str:
    return _front_matter_block(COMPLETE_FIELDS)


# ── happy path ───────────────────────────────────────────────────────────────


def test_complete_record_validates_and_returns_front_matter():
    parsed = validate_build_record(_complete_record_text())
    for field, value in COMPLETE_FIELDS.items():
        assert parsed[field] == value


def test_validate_reads_from_an_existing_file(tmp_path: Path):
    record = tmp_path / "build-record.md"
    record.write_text(_complete_record_text())
    parsed = validate_build_record(record)
    assert parsed["branch"] == COMPLETE_FIELDS["branch"]


def test_validate_reads_from_a_path_string(tmp_path: Path):
    record = tmp_path / "build-record.md"
    record.write_text(_complete_record_text())
    parsed = validate_build_record(str(record))
    assert parsed["commit"] == COMPLETE_FIELDS["commit"]


def test_validate_tolerates_leading_blank_lines_before_front_matter():
    text = "\n\n" + _complete_record_text()
    parsed = validate_build_record(text)
    assert parsed["spec_hash"] == COMPLETE_FIELDS["spec_hash"]


# ── each required field, missing or empty, raises (parametrized, RED->GREEN) ──


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_missing_field_raises_naming_exactly_that_field(field: str):
    """Neuter proof: drop ONE required key from an otherwise-complete record.

    If the guard were removed (e.g. ``validate_build_record`` stopped
    checking a field), this test goes RED because no exception is raised at
    all. Restoring the check makes it GREEN again — the guard is
    non-vacuous.
    """
    fields = {k: v for k, v in COMPLETE_FIELDS.items() if k != field}
    with pytest.raises(BuildRecordValidationError) as exc:
        validate_build_record(_front_matter_block(fields))
    assert exc.value.missing_fields == [field]
    assert field in str(exc.value)


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_empty_string_field_raises_naming_exactly_that_field(field: str):
    """An explicitly present-but-blank field is exactly as invalid as an
    absent one — a builder cannot satisfy the schema by writing
    ``spec_ref: ""`` and calling it done."""
    fields = {**COMPLETE_FIELDS, field: "   "}
    with pytest.raises(BuildRecordValidationError) as exc:
        validate_build_record(_front_matter_block(fields))
    assert exc.value.missing_fields == [field]


def test_multiple_missing_fields_are_all_named():
    fields = {
        k: v
        for k, v in COMPLETE_FIELDS.items()
        if k not in ("spec_ref", "commit", "gates")
    }
    with pytest.raises(BuildRecordValidationError) as exc:
        validate_build_record(_front_matter_block(fields))
    assert exc.value.missing_fields == ["spec_ref", "commit", "gates"]


# ── malformed / absent front-matter → fail-closed as "everything missing" ────


def test_no_front_matter_at_all_raises_naming_every_field():
    with pytest.raises(BuildRecordValidationError) as exc:
        validate_build_record("# Just a plain markdown record\n\nno front matter.\n")
    assert exc.value.missing_fields == list(REQUIRED_FIELDS)


def test_unclosed_front_matter_raises_naming_every_field():
    text = "---\nspec_ref: x\n\n# body with no closing delimiter\n"
    with pytest.raises(BuildRecordValidationError) as exc:
        validate_build_record(text)
    assert exc.value.missing_fields == list(REQUIRED_FIELDS)


def test_front_matter_that_is_not_a_mapping_raises_naming_every_field():
    text = "---\n- just\n- a\n- list\n---\n\nbody\n"
    with pytest.raises(BuildRecordValidationError) as exc:
        validate_build_record(text)
    assert exc.value.missing_fields == list(REQUIRED_FIELDS)


def test_invalid_yaml_front_matter_raises_naming_every_field():
    text = "---\nspec_ref: [unterminated\n---\n\nbody\n"
    with pytest.raises(BuildRecordValidationError) as exc:
        validate_build_record(text)
    assert exc.value.missing_fields == list(REQUIRED_FIELDS)


def test_extract_front_matter_returns_none_for_no_delimiter():
    assert _extract_front_matter("no delimiter here") is None


def test_is_existing_file_survives_oserror_on_a_too_long_candidate():
    """A single path segment over the filesystem's 255-byte limit makes
    ``Path.is_file()`` raise ``OSError`` (ENAMETOOLONG) rather than
    returning ``False`` — ``_is_existing_file`` must swallow that and report
    "not a file" so literal record text is never mistaken for a crash."""
    assert len(_LONG_FIRST_SEGMENT_TEXT.split("/", 1)[0]) > 255
    assert _is_existing_file(_LONG_FIRST_SEGMENT_TEXT) is False


def test_validate_build_record_survives_long_literal_text_no_slash():
    """End-to-end: literal record text long enough to blow the filesystem's
    path-segment limit is treated as text, not a crash — it still fails
    validation for lacking front-matter, never an unhandled ``OSError``."""
    with pytest.raises(BuildRecordValidationError) as exc:
        validate_build_record(_LONG_FIRST_SEGMENT_TEXT)
    assert exc.value.missing_fields == list(REQUIRED_FIELDS)


# ── _is_empty helper — the shared emptiness contract ─────────────────────────


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "\n\t", [], {}, (), set()],
)
def test_is_empty_true_for_blank_values(value):
    assert _is_empty(value) is True


@pytest.mark.parametrize(
    "value",
    ["x", "0", 0, False, ["item"], {"k": "v"}, 3.14],
)
def test_is_empty_false_for_present_falsy_or_nonempty_values(value):
    """A falsy scalar (``0`` / ``False``) still counts as PRESENT — Layer 0
    checks presence, not truthiness; a genuinely wrong value is Layer 2's
    job."""
    assert _is_empty(value) is False
