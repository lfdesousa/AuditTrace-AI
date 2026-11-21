"""Every image part of one chat request: the value of ``interactions.input_media``."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from audittrace.media.image_part_record import (
    STATUS_RECORDED,
    STATUS_REFUSED,
    ImagePartRecord,
)

SCHEMA = 1


@dataclass(frozen=True)
class InputMediaRecord:
    items: tuple[ImagePartRecord, ...]

    @property
    def refused(self) -> tuple[ImagePartRecord, ...]:
        return tuple(i for i in self.items if i.status == STATUS_REFUSED)

    def to_json(self) -> str:
        """The stored form: the canonical JSON ``content_hash`` also uses."""
        body = {
            "schema": SCHEMA,
            "count": len(self.items),
            "items": [i.as_dict() for i in self.items],
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":"))

    def span_attributes(self) -> dict[str, Any]:
        """Count, size and digests for the request span: no payload."""
        done = [i for i in self.items if i.status == STATUS_RECORDED]
        return {
            "audittrace.input.image.count": len(self.items),
            "audittrace.input.image.refused": len(self.refused),
            "audittrace.input.image.bytes_total": sum(i.size or 0 for i in done),
            "audittrace.input.image.sha256": [str(i.sha256) for i in done],
        }
