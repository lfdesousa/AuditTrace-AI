"""Hash-pinned artefact record of the decision model (#459 WU-459-2, Q9/B3).

The record is the ONLY place in ``src/`` that names a model: its weights
digests, its tokenizer's added tokens, its chat-template pieces, its licence
status and the evaluation it passed. Everything else in the decision package
reads the record; a new model is a new record, not a code change (operator Q9,
gate B3). The file is a JSON document under ``artefacts/`` (schema v1, this
module), shipped as package data, and pinned by sha256 from the configuration
(``decision_artefact_record_sha256``): a record that was edited after it was
pinned is refused at startup.

Guards (each has a test that turns RED when the guard is removed): the sha256
comparison (``sha256_mismatch``); the schema walk (``schema``, carrying the
offending key name and never the file contents); the registered-family check
(``family_unknown``); and :meth:`ArtefactRecord.table_for`, which hands out the
added-token table ONLY for a digest the record lists (fail closed: ``None``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from audittrace.services.decision.token_tables import AddedToken, AddedTokenTable

# The chat-template engines this package implements. A record whose
# ``template.family`` is not registered here cannot be rendered and is refused.
TEMPLATE_FAMILIES: frozenset[str] = frozenset({"chatml-think-off-v1"})

# The closed vocabulary of refusal codes (``RecordError.code``).
RECORD_CODES: frozenset[str] = frozenset({"missing", "sha256_mismatch", "schema", "family_unknown"})  # fmt: skip

_HEX64 = frozenset("0123456789abcdef")
_RECORD_VERSION = 1
_MODEL_KEYS = ("id", "hf_repo", "hf_commit", "family")
_WEIGHTS_KEYS = ("digest", "file_basename", "alias", "quantisation", "backend_measured")
_SPECIAL_TOKEN_KEYS = ("im_start", "im_end", "think_open", "think_close")
_PIECE_KEYS = ("user_role", "assistant_role", "newline", "think_body", "after_think")
_LITERAL_KEYS = (
    "state_head",
    "question_head",
    "options_head",
    "option_sep",
    "instruction",
)
_LICENCE_KEYS = (
    "status",
    "source_card_commit",
    "checked_on",
    "base_model",
    "base_licence",
    "risk_accepted_by",
    "accepted_on",
    "disclaimer",
)


class RecordError(ValueError):
    """A record could not be loaded; ``code`` is one of :data:`RECORD_CODES`.

    The message carries the code and a short detail (a key name, a path, a
    family name), never the file contents.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in RECORD_CODES:
            raise ValueError(f"unknown record code {code!r}")
        super().__init__(f"artefact record {code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Weights:
    """One weights file the record vouches for (``weights[]``)."""

    digest: str
    file_basename: str
    alias: str
    quantisation: str
    backend_measured: str


@dataclass(frozen=True)
class TemplateSpec:
    """The chat-template block: identity plus every piece the engine places."""

    template_id: str
    family: str
    upstream_chat_template_sha256: str
    special_tokens: Mapping[str, str]
    pieces: Mapping[str, str]
    user_content_literals: Mapping[str, str]


@dataclass(frozen=True)
class Licence:
    """The licence block (BC-1: the disclaimer travels with the model)."""

    status: str
    source_card_commit: str
    checked_on: str
    base_model: str
    base_licence: str
    risk_accepted_by: str
    accepted_on: str
    disclaimer: str


@dataclass(frozen=True)
class ArtefactRecord:
    """A loaded, sha256-verified, schema-checked record."""

    path: Path
    sha256: str
    record_version: int
    model: Mapping[str, str]
    weights: tuple[Weights, ...]
    tokenizer_config_sha256: str
    tokenizer_source: str
    added_tokens: tuple[AddedToken, ...]
    added_token_ids: tuple[int, ...]
    template: TemplateSpec
    licence: Licence
    evaluation: Mapping[str, object]
    _table: AddedTokenTable = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        tokens = MappingProxyType(
            dict(zip(self.added_token_ids, self.added_tokens, strict=True))
        )
        object.__setattr__(
            self,
            "_table",
            AddedTokenTable(
                source=self.tokenizer_source,
                tokenizer_config_sha256=self.tokenizer_config_sha256,
                tokens=tokens,
            ),
        )

    @property
    def digests(self) -> frozenset[str]:
        """Every weights digest the record vouches for."""
        return frozenset(w.digest for w in self.weights)

    def table_for(self, digest: str) -> AddedTokenTable | None:
        """The added-token table for ``digest``, or ``None`` (fail closed)."""
        return self._table if any(w.digest == digest for w in self.weights) else None


def resolve_record_path(value: str) -> Path:
    """An absolute path as given; a bare name under this package's ``artefacts/``."""
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    return Path(__file__).resolve().parent / "artefacts" / value


def _mapping(parent: Any, key: str) -> dict[str, Any]:
    value = parent.get(key) if isinstance(parent, dict) else None
    if not isinstance(value, dict):
        raise RecordError("schema", key)
    return value


def _string(parent: dict[str, Any], key: str, where: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value:
        raise RecordError("schema", f"{where}.{key}")
    return value


def _string_mapping(
    parent: dict[str, Any], key: str, keys: tuple[str, ...], where: str
) -> Mapping[str, str]:
    block = _mapping(parent, key)
    return MappingProxyType({k: _string(block, k, f"{where}.{key}") for k in keys})


def _weights(data: dict[str, Any]) -> tuple[Weights, ...]:
    raw = data.get("weights")
    if not isinstance(raw, list) or not raw:
        raise RecordError("schema", "weights")
    out: list[Weights] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise RecordError("schema", f"weights[{i}]")
        values = {k: _string(entry, k, f"weights[{i}]") for k in _WEIGHTS_KEYS}
        digest = values["digest"]
        if len(digest) != 64 or not set(digest) <= _HEX64:
            raise RecordError("schema", f"weights[{i}].digest")
        out.append(Weights(**values))
    return tuple(out)


def _added_tokens(
    tokenizer: dict[str, Any],
) -> tuple[tuple[int, ...], tuple[AddedToken, ...]]:
    raw = tokenizer.get("added_tokens")
    if not isinstance(raw, list) or not raw:
        raise RecordError("schema", "tokenizer.added_tokens")
    ids: list[int] = []
    tokens: list[AddedToken] = []
    for i, entry in enumerate(raw):
        where = f"tokenizer.added_tokens[{i}]"
        if not isinstance(entry, dict):
            raise RecordError("schema", where)
        token_id = entry.get("id")
        content = entry.get("content")
        special = entry.get("special")
        if isinstance(token_id, bool) or not isinstance(token_id, int):
            raise RecordError("schema", f"{where}.id")
        if not isinstance(content, str) or not content:
            raise RecordError("schema", f"{where}.content")
        if not isinstance(special, bool):
            raise RecordError("schema", f"{where}.special")
        if ids and token_id <= ids[-1]:
            raise RecordError("schema", f"{where}.id not strictly increasing")
        ids.append(token_id)
        tokens.append(AddedToken(content, special=special))
    return tuple(ids), tuple(tokens)


def load_record(path: Path, expected_sha256: str) -> ArtefactRecord:
    """Read, sha256-pin and schema-check the record at ``path`` (fail closed).

    Raises :class:`RecordError` with ``code`` ``missing`` (no such file),
    ``sha256_mismatch`` (the bytes are not the pinned ones), ``schema`` (a key
    is absent or of the wrong shape; the detail names the key) or
    ``family_unknown`` (``template.family`` is not in :data:`TEMPLATE_FAMILIES`).
    """
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
        raise RecordError("missing", str(path)) from exc
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise RecordError(
            "sha256_mismatch",
            f"expected {expected_sha256[:12]}..., file is {actual_sha256[:12]}...",
        )
    try:
        data: Any = json.loads(raw)
    except ValueError as exc:
        raise RecordError("schema", "json") from exc
    if not isinstance(data, dict):
        raise RecordError("schema", "document")
    if data.get("record_version") != _RECORD_VERSION:
        raise RecordError("schema", "record_version")
    model = _string_mapping(data, "model", _MODEL_KEYS, "")
    weights = _weights(data)
    tokenizer = _mapping(data, "tokenizer")
    tokenizer_config_sha256 = _string(tokenizer, "config_sha256", "tokenizer")
    tokenizer_source = _string(tokenizer, "source", "tokenizer")
    added_token_ids, added_tokens = _added_tokens(tokenizer)
    template_block = _mapping(data, "template")
    template = TemplateSpec(
        template_id=_string(template_block, "template_id", "template"),
        family=_string(template_block, "family", "template"),
        upstream_chat_template_sha256=_string(
            template_block, "upstream_chat_template_sha256", "template"
        ),
        special_tokens=_string_mapping(
            template_block, "special_tokens", _SPECIAL_TOKEN_KEYS, "template"
        ),
        pieces=_string_mapping(template_block, "pieces", _PIECE_KEYS, "template"),
        user_content_literals=_string_mapping(
            template_block, "user_content_literals", _LITERAL_KEYS, "template"
        ),
    )
    licence_block = _mapping(data, "licence")
    licence = Licence(
        **{k: _string(licence_block, k, "licence") for k in _LICENCE_KEYS}
    )
    evaluation = MappingProxyType(dict(_mapping(data, "evaluation")))
    if template.family not in TEMPLATE_FAMILIES:
        raise RecordError("family_unknown", template.family)
    return ArtefactRecord(
        path=path,
        sha256=actual_sha256,
        record_version=_RECORD_VERSION,
        model=model,
        weights=weights,
        tokenizer_config_sha256=tokenizer_config_sha256,
        tokenizer_source=tokenizer_source,
        added_tokens=added_tokens,
        added_token_ids=added_token_ids,
        template=template,
        licence=licence,
        evaluation=evaluation,
    )
