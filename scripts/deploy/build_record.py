"""SDLC-ADR-005 Layer-0 build-record schema (ADR-059 mechanical enforcement, WU-1).

The concrete object every later enforcement layer verifies: every builder
build-record / reviewer verdict MUST carry a structured YAML front-matter
block naming :data:`REQUIRED_FIELDS`. This module is the standalone
validator — no network egress, no dependency on :mod:`scripts.deploy.memory`
— so Layer 1 (dispatch gate), Layer 2 (independent reviewer), and Layer 3
(CI backstop) can each import :func:`validate_build_record` directly without
pulling in the memory-server HTTP seam. :mod:`scripts.deploy.memory` wires
this in as an *additive*, opt-in entry point (:func:`~scripts.deploy.memory.
log_build_record`) that never changes the existing, shared
:func:`~scripts.deploy.memory.log_deploy_record` used by the deploy/release/
curator agents, whose records do not carry this schema.

**Scope (WU-1 / Layer 0 only):** this module checks *presence* — every
required field exists in the front-matter and is non-empty. It does NOT
resolve ``spec_ref`` against ``sdlc/specs/`` or the memory server (that is
Layer 1's job), and it does NOT independently re-query the memory server to
confirm ``log_key`` actually indexed (that is Layer 2's job). Layer 0 is
deliberately the cheapest, most-local check: the object every later layer
verifies, not the verification itself.

**Required fields** (see the SDLC-ADR-005 spec's "Layer 0" section for the
authoritative meaning of each):

* ``spec_ref`` — path in ``sdlc/specs/`` and the memory-server key of the
  ratified spec this record builds from.
* ``spec_hash`` — content hash of the spec, so "ratified" is pinned.
* ``recall_evidence`` — the STEP-2 recall query + result count (the literal
  string ``"empty"`` is a valid, non-empty value — an empty recall never
  blocks the loop, but it MUST be stated, not omitted).
* ``log_key`` — the STEP-5 memory-server key the record itself was logged
  under.
* ``index_status`` — the STEP-5 index result (must show at least one
  indexed chunk; Layer 0 only checks it is present).
* ``branch`` — the feature branch the work landed on.
* ``commit`` — the commit SHA.
* ``gates`` — the local gate results (`make test` summary, per-file
  coverage, `make helm-lint`, the integration-gate result).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REQUIRED_FIELDS: tuple[str, ...] = (
    "spec_ref",
    "spec_hash",
    "recall_evidence",
    "log_key",
    "index_status",
    "branch",
    "commit",
    "gates",
)

_FRONT_MATTER_DELIMITER = "---"


class BuildRecordValidationError(ValueError):
    """Raised when a build-record's Layer-0 front-matter is missing/empty a
    required field.

    Carries :attr:`missing_fields` — the exact keys (in :data:`REQUIRED_FIELDS`
    order) that are absent or empty — so a caller (reviewer, CI) can report
    precisely what is wrong rather than a generic parse failure. Fail-closed:
    a record missing YAML front-matter entirely, or front-matter that fails
    to parse as a YAML mapping, is treated as missing EVERY required field —
    there is no partial-credit path.
    """

    def __init__(self, missing_fields: list[str]) -> None:
        joined = ", ".join(missing_fields)
        super().__init__(f"build-record missing/empty required field(s): {joined}")
        self.missing_fields = missing_fields


def _is_existing_file(candidate: str) -> bool:
    """True iff ``candidate`` names an existing file.

    Mirrors :func:`scripts.deploy.memory._is_existing_file` (duplicated, not
    imported, so this module stays free of any dependency on the network-
    carrying ``memory`` module — see the module docstring). A candidate too
    long to stat (``OSError``/ENAMETOOLONG on literal record text) is not a
    file; treat it as text rather than crashing.
    """
    try:
        return Path(candidate).is_file()
    except OSError:
        return False


def _read_text(path_or_text: str | Path) -> str:
    """Resolve ``path_or_text`` to its record text.

    A :class:`~pathlib.Path`, or a ``str`` naming an EXISTING file, is read
    from disk. Anything else is treated as literal record text.
    """
    if isinstance(path_or_text, Path):
        return path_or_text.read_text()
    if isinstance(path_or_text, str) and _is_existing_file(path_or_text):
        return Path(path_or_text).read_text()
    return str(path_or_text)


def _extract_front_matter(text: str) -> dict[str, Any] | None:
    """Return the parsed YAML front-matter mapping, or ``None`` if absent/malformed.

    A record MUST open with a ``---``-delimited YAML block (leading blank
    lines are tolerated). Returns ``None`` — never raises — for "no front
    matter" / "no closing delimiter" / "front-matter parses to something
    other than a mapping" / "YAML doesn't parse". The caller
    (:func:`validate_build_record`) turns any of those into the fail-closed
    :class:`BuildRecordValidationError` naming every required field as
    missing: a malformed record is indistinguishable from an empty one, both
    are "not a valid Layer-0 record".
    """
    stripped = text.lstrip("\n")
    opening = f"{_FRONT_MATTER_DELIMITER}\n"
    if not stripped.startswith(opening):
        return None
    rest = stripped[len(opening) :]
    closing_marker = f"\n{_FRONT_MATTER_DELIMITER}"
    end = rest.find(closing_marker)
    if end == -1:
        return None
    block = rest[:end]
    try:
        parsed = yaml.safe_load(block)
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_empty(value: Any) -> bool:
    """Fail-closed emptiness check shared across every required field.

    ``None`` and blank/whitespace-only strings are empty; empty containers
    (list/dict/tuple/set) are empty. Everything else — including falsy
    scalars like ``0`` or ``False`` — counts as present: a field's *presence*
    is Layer 0's job, its *value* (e.g. "index_status shows >=1 indexed
    chunk") is Layer 2's job.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


def validate_build_record(path_or_text: str | Path) -> dict[str, Any]:
    """Validate the Layer-0 structured front-matter of a build-record / verdict.

    Accepts a :class:`~pathlib.Path`, a ``str`` path to an existing file, or
    literal record text. Parses the leading YAML front-matter and confirms
    every field in :data:`REQUIRED_FIELDS` is present and non-empty (see
    :func:`_is_empty`). Fail-closed: raises :class:`BuildRecordValidationError`
    naming every missing/empty field — never returns a partial result.

    Returns the parsed front-matter mapping on success, so a caller can read
    individual fields (e.g. ``spec_ref``) without re-parsing the record.
    """
    text = _read_text(path_or_text)
    front_matter = _extract_front_matter(text)
    if front_matter is None:
        raise BuildRecordValidationError(list(REQUIRED_FIELDS))
    missing = [field for field in REQUIRED_FIELDS if _is_empty(front_matter.get(field))]
    if missing:
        raise BuildRecordValidationError(missing)
    return front_matter
