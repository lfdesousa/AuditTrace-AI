"""ADR-048 PR-B2 — application-layer denylist on MinIO quarantine/* GET.

These tests cover the ``QuarantineDenyingMinioClient`` proxy that
wraps the bare ``minio.Minio`` instance. The proxy refuses
``get_object`` for any key starting with ``quarantine/`` and
delegates every other call.

PR-B7 will land MinIO IAM-side enforcement (the ``audittrace_app``
role gets ``Effect: Deny`` on ``quarantine/*``); after that lands,
MinIO returns 403 directly. This wrapper stays as defense-in-depth.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from audittrace.services.quarantine_guard import (
    _QUARANTINE_GUARD_ERROR_CODES,
    QuarantineDenyingMinioClient,
    QuarantinedObjectAccessError,
)


class TestClosedSetErrorCodes:
    """Pin the closed-set error codes so SOC parsers don't break on
    silent additions. ADR-048 PR-B2 ships exactly one code; future
    refusals (e.g., ``quarantine_age_exceeded``) are a closed-set
    extension that requires an ADR amendment."""

    def test_codes_match_adr_048_closed_set(self) -> None:
        expected = {"quarantine_read_denied"}
        assert _QUARANTINE_GUARD_ERROR_CODES == expected


class _FakeMinioClient:
    """Minimal stand-in for ``minio.Minio`` — just enough surface for
    the proxy tests. Not a mock library — that pattern doesn't carry
    its weight here. This is a hand-rolled fake that records calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def get_object(self, bucket: str, key: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("get_object", (bucket, key, *args), kwargs))
        return SimpleNamespace(read=lambda: b"FAKE-CONTENT")

    def put_object(self, bucket: str, key: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("put_object", (bucket, key, *args), kwargs))
        return SimpleNamespace(etag="abc")

    def remove_object(self, bucket: str, key: str) -> None:
        self.calls.append(("remove_object", (bucket, key), {}))

    def list_objects(
        self, bucket: str, prefix: str = "", recursive: bool = False
    ) -> list:
        self.calls.append(("list_objects", (bucket, prefix), {"recursive": recursive}))
        return []


class TestQuarantineDenyingMinioClient:
    """The proxy refuses ``quarantine/*`` GET; delegates everything else."""

    def test_get_quarantine_object_raises_access_error(self) -> None:
        inner = _FakeMinioClient()
        client = QuarantineDenyingMinioClient(inner)
        with pytest.raises(QuarantinedObjectAccessError) as excinfo:
            client.get_object("memory-shared", "quarantine/u1/abc/file.pdf")
        assert excinfo.value.code == "quarantine_read_denied"
        assert "quarantine/u1/abc/file.pdf" in str(excinfo.value)
        assert excinfo.value.key == "quarantine/u1/abc/file.pdf"
        # Inner client never called.
        assert inner.calls == []

    def test_get_episodic_object_delegates(self) -> None:
        inner = _FakeMinioClient()
        client = QuarantineDenyingMinioClient(inner)
        result = client.get_object("memory-shared", "episodic/papers/abc/file.pdf")
        assert result.read() == b"FAKE-CONTENT"
        assert len(inner.calls) == 1
        assert inner.calls[0][0] == "get_object"

    def test_put_object_on_quarantine_is_allowed(self) -> None:
        # PUT on quarantine/* is the legitimate upload path; only GET
        # is forbidden. The proxy must not interfere.
        inner = _FakeMinioClient()
        client = QuarantineDenyingMinioClient(inner)
        client.put_object("memory-shared", "quarantine/u1/abc/file.pdf", b"bytes", 5)
        assert len(inner.calls) == 1
        assert inner.calls[0][0] == "put_object"

    def test_remove_object_delegates(self) -> None:
        inner = _FakeMinioClient()
        client = QuarantineDenyingMinioClient(inner)
        client.remove_object("memory-shared", "quarantine/u1/abc/file.pdf")
        assert inner.calls == [
            ("remove_object", ("memory-shared", "quarantine/u1/abc/file.pdf"), {})
        ]

    def test_list_objects_delegates(self) -> None:
        inner = _FakeMinioClient()
        client = QuarantineDenyingMinioClient(inner)
        result = client.list_objects(
            "memory-shared", prefix="episodic/", recursive=True
        )
        assert result == []
        assert inner.calls == [
            ("list_objects", ("memory-shared", "episodic/"), {"recursive": True})
        ]

    def test_arbitrary_attribute_delegated(self) -> None:
        inner = _FakeMinioClient()
        inner.bucket_exists = lambda b: True  # type: ignore[attr-defined]
        client = QuarantineDenyingMinioClient(inner)
        # Method that exists on inner but not on proxy — falls through __getattr__.
        assert client.bucket_exists("memory-shared") is True


class TestPrefixValidation:
    """Constructor validates the prefix shape."""

    def test_prefix_without_trailing_slash_rejected(self) -> None:
        with pytest.raises(ValueError, match="must end with '/'"):
            QuarantineDenyingMinioClient(
                _FakeMinioClient(), quarantine_prefix="quarantine"
            )

    def test_custom_prefix_honoured(self) -> None:
        inner = _FakeMinioClient()
        client = QuarantineDenyingMinioClient(inner, quarantine_prefix="staging/")
        assert client.quarantine_prefix == "staging/"
        # quarantine/ no longer triggers — staging/ does
        client.get_object("memory-shared", "quarantine/u1/file.pdf")  # delegates
        with pytest.raises(QuarantinedObjectAccessError):
            client.get_object("memory-shared", "staging/u1/file.pdf")


class TestErrorMessageShape:
    """The exception message + .code attribute let SOC parsers pivot
    on the error class without parsing free-text."""

    def test_error_class_is_permission_error_subclass(self) -> None:
        # Subclass relationship — generic permission-error catchers
        # also catch this.
        assert issubclass(QuarantinedObjectAccessError, PermissionError)

    def test_error_carries_key_attribute(self) -> None:
        err = QuarantinedObjectAccessError(key="quarantine/abc")
        assert err.key == "quarantine/abc"
        assert err.code == "quarantine_read_denied"
