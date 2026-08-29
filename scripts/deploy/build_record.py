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

**Placeholder rejection (2026-08-29, kills the "PENDING ships forever"
defect):** ``log_key`` and ``index_status`` describe the record's OWN
upload, so a builder writing the record BEFORE calling
:func:`~scripts.deploy.memory.log_build_record` cannot know their real
values yet and historically wrote a literal placeholder such as
``"PENDING — filled in by log_build_record..."``. Presence-only checking
lets that placeholder pass Layer 0 *vacuously* — the record looks complete
but attests nothing about whether it actually indexed. :data:`
PLACEHOLDER_CHECKED_FIELDS` names the two self-referential fields for which
:func:`validate_build_record` now also rejects a PENDING-shaped value (see
``_is_placeholder``), and :func:`patch_front_matter_fields` is the
companion mechanism :func:`~scripts.deploy.memory.log_build_record` uses to
overwrite them with REAL post-upload values before the record is persisted
— so a caller cannot leave placeholders in the server-persisted copy even
if they never patch anything themselves.

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

# The two self-referential fields (2026-08-29): only known AFTER
# ``log_build_record`` uploads/indexes the record itself, so they are the
# ones a builder is tempted to placeholder. ``validate_build_record`` rejects
# a PENDING-shaped value for exactly these two by default; a caller may pass
# them (and only them) in ``placeholder_exempt_fields`` for the pre-upload
# checkpoint that runs BEFORE the real values are known.
PLACEHOLDER_CHECKED_FIELDS: frozenset[str] = frozenset({"log_key", "index_status"})

_FRONT_MATTER_DELIMITER = "---"
_PLACEHOLDER_MARKER = "pending"


class BuildRecordValidationError(ValueError):
    """Raised when a build-record's Layer-0 front-matter is missing/empty a
    required field, OR carries a PENDING-shaped placeholder in one of
    :data:`PLACEHOLDER_CHECKED_FIELDS`.

    Carries :attr:`missing_fields` — the exact keys (in :data:`REQUIRED_FIELDS`
    order) that are absent, empty, or (for the two self-referential fields)
    still a placeholder — so a caller (reviewer, CI) can report precisely
    what is wrong rather than a generic parse failure. Fail-closed: a record
    missing YAML front-matter entirely, or front-matter that fails to parse
    as a YAML mapping, is treated as missing EVERY required field — there is
    no partial-credit path.
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


def _split_front_matter(text: str) -> tuple[str, str] | None:
    """Split *text* into ``(raw_yaml_block, body_after_closing_delimiter)``.

    A record MUST open with a ``---``-delimited YAML block (leading blank
    lines are tolerated). Returns ``None`` — never raises — for "no front
    matter" / "no closing delimiter". Shared by :func:`_extract_front_matter`
    (parse-only) and :func:`patch_front_matter_fields` (parse-and-rewrite) so
    the delimiter-scanning logic lives in exactly one place.
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
    return rest[:end], rest[end + len(closing_marker) :]


def _extract_front_matter(text: str) -> dict[str, Any] | None:
    """Return the parsed YAML front-matter mapping, or ``None`` if absent/malformed.

    Returns ``None`` — never raises — for "no front matter" / "no closing
    delimiter" / "front-matter parses to something other than a mapping" /
    "YAML doesn't parse". The caller (:func:`validate_build_record`) turns
    any of those into the fail-closed :class:`BuildRecordValidationError`
    naming every required field as missing: a malformed record is
    indistinguishable from an empty one, both are "not a valid Layer-0
    record".
    """
    split = _split_front_matter(text)
    if split is None:
        return None
    block, _body = split
    try:
        parsed = yaml.safe_load(block)
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_placeholder(value: Any) -> bool:
    """True iff *value* is a PENDING-shaped placeholder string.

    Case-insensitive substring match on :data:`_PLACEHOLDER_MARKER`
    (``"pending"``) — catches both the known literal prefix ``"PENDING —
    filled in by log_build_record..."`` and any case variant. Only strings
    can be placeholders; a non-string value (e.g. an already-real nested
    mapping) is never flagged here.
    """
    return isinstance(value, str) and _PLACEHOLDER_MARKER in value.lower()


def patch_front_matter_fields(path_or_text: str | Path, updates: dict[str, Any]) -> str:
    """Return the record's text with the given front-matter fields overwritten.

    Used by :func:`scripts.deploy.memory.log_build_record` (D1, 2026-08-29)
    to replace ``log_key``/``index_status`` with their REAL post-upload
    values before the record is re-persisted — the mechanical fix for the
    "PENDING placeholder ships forever" defect (presence-only checking let it
    pass Layer 0 vacuously). Every OTHER field, and the record's body text
    after the closing delimiter, is preserved unchanged; field order from the
    original document is kept (``sort_keys=False``).

    Fail-closed like :func:`validate_build_record`: raises
    :class:`BuildRecordValidationError` naming every required field if
    *path_or_text* has no parseable YAML front-matter — a record that can't
    be parsed can't be safely patched.
    """
    text = _read_text(path_or_text)
    split = _split_front_matter(text)
    if split is None:
        raise BuildRecordValidationError(list(REQUIRED_FIELDS))
    block, body = split
    try:
        front_matter = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        raise BuildRecordValidationError(list(REQUIRED_FIELDS)) from exc
    if not isinstance(front_matter, dict):
        raise BuildRecordValidationError(list(REQUIRED_FIELDS))
    front_matter.update(updates)
    patched_block = yaml.safe_dump(
        front_matter, sort_keys=False, default_flow_style=False
    ).rstrip("\n")
    return (
        f"{_FRONT_MATTER_DELIMITER}\n{patched_block}\n{_FRONT_MATTER_DELIMITER}{body}"
    )


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


def validate_build_record(
    path_or_text: str | Path,
    *,
    placeholder_exempt_fields: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Validate the Layer-0 structured front-matter of a build-record / verdict.

    Accepts a :class:`~pathlib.Path`, a ``str`` path to an existing file, or
    literal record text. Parses the leading YAML front-matter and confirms
    every field in :data:`REQUIRED_FIELDS` is present and non-empty (see
    :func:`_is_empty`) AND that neither field in :data:`PLACEHOLDER_CHECKED_FIELDS`
    holds a PENDING-shaped placeholder (see :func:`_is_placeholder`) — presence
    alone is no longer sufficient for those two self-referential fields
    (2026-08-29 D2). Fail-closed: raises :class:`BuildRecordValidationError`
    naming every missing/empty/placeholder field — never returns a partial
    result.

    ``placeholder_exempt_fields`` is an ADDITIVE, opt-in escape hatch for the
    ONE legitimate pre-upload checkpoint that runs before the real values are
    knowable: :func:`~scripts.deploy.memory.log_build_record` passes
    :data:`PLACEHOLDER_CHECKED_FIELDS` here for its first, pre-network call so
    a caller's still-placeholder ``log_key``/``index_status`` doesn't block
    the very call that is about to auto-populate them (D1) — every other
    caller (the independent reviewer, CI, a hand-edited record) gets the
    default, fully-strict check. Fields outside :data:`PLACEHOLDER_CHECKED_FIELDS`
    are never exempt regardless of what is passed here — this only ever
    loosens the placeholder-shape check, never the presence check.

    Returns the parsed front-matter mapping on success, so a caller can read
    individual fields (e.g. ``spec_ref``) without re-parsing the record.
    """
    text = _read_text(path_or_text)
    front_matter = _extract_front_matter(text)
    if front_matter is None:
        raise BuildRecordValidationError(list(REQUIRED_FIELDS))
    problems: list[str] = []
    for field in REQUIRED_FIELDS:
        value = front_matter.get(field)
        if _is_empty(value):
            problems.append(field)
        elif (
            field in PLACEHOLDER_CHECKED_FIELDS
            and field not in placeholder_exempt_fields
            and _is_placeholder(value)
        ):
            problems.append(field)
    if problems:
        raise BuildRecordValidationError(problems)
    return front_matter
