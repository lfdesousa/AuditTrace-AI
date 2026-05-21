"""In-memory FakeObjectStorageProvider for AT-AI tests.

Replaces the hand-rolled `_FakeMinio` doubles that lived inside each
test module. Implements the `S3ObjectStorageProvider` ABC against a
nested dict (bucket → key → bytes), so AT-AI's existing test bodies
can switch from `_FakeMinio()` injection to `FakeObjectStorageProvider()`
injection with no behaviour change.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import BinaryIO

from audittrace_object_storage import (
    ObjectMetadata,
    ObjectNotFoundError,
    ObjectReader,
    S3ObjectStorageProvider,
)


class FakeObjectStorageProvider(S3ObjectStorageProvider):
    """Process-local fake — buckets and objects live in nested dicts."""

    def __init__(self, objects: dict[str, dict[str, bytes]] | None = None) -> None:
        # objects[bucket][key] -> bytes
        self._objects: dict[str, dict[str, bytes]] = objects or {}

    # ---- helpers for tests ----

    def add(self, bucket: str, key: str, payload: bytes) -> None:
        self._objects.setdefault(bucket, {})[key] = payload

    def has(self, bucket: str, key: str) -> bool:
        return key in self._objects.get(bucket, {})

    def keys(self, bucket: str) -> list[str]:
        return sorted(self._objects.get(bucket, {}).keys())

    # ---- ABC implementation ----

    def list_objects(self, bucket: str, prefix: str = "") -> Iterator[ObjectMetadata]:
        for key, payload in self._objects.get(bucket, {}).items():
            if key.startswith(prefix):
                yield ObjectMetadata(
                    object_name=key,
                    size=len(payload),
                    etag=f"fake-etag-{key}",
                    last_modified=datetime.now(UTC),
                )

    def get_object(self, bucket: str, key: str) -> ObjectReader:
        bucket_objs = self._objects.get(bucket, {})
        if key not in bucket_objs:
            raise ObjectNotFoundError(f"{bucket}/{key}")
        stream = io.BytesIO(bucket_objs[key])
        return ObjectReader(stream=stream)

    def put_object(
        self,
        bucket: str,
        key: str,
        data: BinaryIO,
        length: int,
        content_type: str | None = None,  # noqa: ARG002
    ) -> None:
        payload = data.read(length) if length else data.read()
        self._objects.setdefault(bucket, {})[key] = payload

    def stat_object(self, bucket: str, key: str) -> ObjectMetadata:
        bucket_objs = self._objects.get(bucket, {})
        if key not in bucket_objs:
            raise ObjectNotFoundError(f"{bucket}/{key}")
        return ObjectMetadata(
            object_name=key,
            size=len(bucket_objs[key]),
            etag=f"fake-etag-{key}",
            last_modified=datetime.now(UTC),
        )

    def remove_object(self, bucket: str, key: str) -> None:
        # Idempotent: missing object is a no-op (matches S3 semantics).
        self._objects.get(bucket, {}).pop(key, None)

    def copy_object(
        self,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
    ) -> None:
        src_objs = self._objects.get(src_bucket, {})
        if src_key not in src_objs:
            raise ObjectNotFoundError(f"{src_bucket}/{src_key}")
        self._objects.setdefault(dst_bucket, {})[dst_key] = src_objs[src_key]

    def health_check(self) -> bool:
        return True
