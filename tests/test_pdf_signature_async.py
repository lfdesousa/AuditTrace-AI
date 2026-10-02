"""Spec #460 / GH #366 — PDF signature classification in the ASYNC pipeline.

Resolved versions these mechanism claims were measured on: pyhanko 0.37.0,
pyhanko-certvalidator 0.32.1, signxml 5.1.0.

Root cause (#366): ``_pdf_signature_status`` called pyhanko's SYNC
``validate_pdf_signature``, whose body is ``asyncio.run(...)``. That raises
inside a running loop, so every signed PDF indexed through the async
``_index_pdf_objects`` read ``check_failed``. The fix awaits the native
``async_validate_pdf_signature``.

Every guard below names its individual neuter. Fixtures (CA, leaf, signed
PDFs) are generated in test setup by ``tests/pdf_signature_fixtures.py``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider

from audittrace.config import get_settings
from audittrace.routes.memory_pdf import signature as sig
from audittrace.routes.memory_pdf.signature import _pdf_signature_status
from tests import pdf_signature_fixtures as fx

ASYNC_VALIDATE = "pyhanko.sign.validation.async_validate_pdf_signature"


# ─────────────────────────────── fixtures ───────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_validation_context():
    """A6: the validation context is a process singleton; reset it between
    cases so one case's trust roots never leak into the next."""
    sig._invalidate_validation_context()
    yield
    sig._invalidate_validation_context()


@pytest.fixture
def pki() -> fx.TestPki:
    return fx.make_test_pki()


@pytest.fixture
def trust_file(tmp_path: Any, pki: fx.TestPki) -> str:
    path = tmp_path / "trust.pem"
    path.write_bytes(pki.ca_pem)
    return str(path)


@pytest.fixture
def trust_env(monkeypatch: pytest.MonkeyPatch, trust_file: str):
    """Point the pipeline's settings at the generated trust bundle."""
    monkeypatch.setenv("AUDITTRACE_PDF_SIGNATURE_TRUST_STORE", trust_file)
    get_settings.cache_clear()
    yield trust_file
    get_settings.cache_clear()


@pytest.fixture
async def signed(pki: fx.TestPki) -> bytes:
    return await fx.sign_pdf_bytes(fx.blank_pdf(), pki)


async def _status(raw: bytes, trust_file: str, **kw: Any) -> tuple[str, int]:
    return await _pdf_signature_status(
        raw, enabled=True, trust_store_path=trust_file, **kw
    )


def _wrong_ca_trust_file(tmp_path: Any) -> str:
    """A trust bundle holding a DIFFERENT CA: the signature validates
    mathematically but its chain is untrusted (and the ADR-054 retry runs)."""
    other = fx.make_test_pki(ca_name="Other CA")
    path = tmp_path / "other.pem"
    path.write_bytes(other.ca_pem)
    return str(path)


# ───────────────────── AC1: through the real HTTP route ─────────────────────


def _index_signed_via_route(
    client: TestClient, raw: bytes
) -> tuple[Any, AsyncMock, MagicMock]:
    """POST /memory/index?file=...&details=true through the ASGI app with a
    scoped JWT (``audittrace:admin``), real pymupdf + real pyhanko."""
    mock_minio = MagicMock()
    response_obj = MagicMock()
    response_obj.read.return_value = raw
    response_obj.__enter__.return_value = response_obj
    mock_minio.get_object.return_value = response_obj
    mock_collection = AsyncMock()
    mock_chroma = MagicMock()
    mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)
    mock_chroma.delete_collection = AsyncMock()
    mock_chroma.list_collections = AsyncMock(return_value=[])
    manifest = AsyncMock()
    manifest.get = AsyncMock(return_value=None)
    with (
        patch("audittrace.auth.get_settings") as mock_settings,
        patch("audittrace.auth._get_jwks_keys") as mock_jwks,
        patch("audittrace.auth._decode_jwt_with_allowed_issuers") as mock_decode,
        patch("audittrace.routes.memory._get_minio_client", return_value=mock_minio),
        patch("audittrace.routes.memory.get_chromadb", return_value=mock_chroma),
        patch(
            "audittrace.routes.memory.get_memory_manifest_service",
            return_value=manifest,
        ),
        patch(
            "audittrace.routes.memory.embed_via_nomic",
            AsyncMock(side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts]),
        ),
    ):
        mock_settings.return_value = MagicMock(auth_enabled=True, auth_required=True)
        mock_jwks.return_value = ["fake-key"]
        mock_decode.return_value = {"sub": "test-user", "scope": "audittrace:admin"}
        response = client.post(
            "/memory/index",
            params={
                "collections": "ai_research_papers",
                "file": "episodic/papers/signed.pdf",
                "details": "true",
            },
            headers={"Authorization": "Bearer fake-token"},
        )
    return response, mock_collection, manifest


class TestAc1RealRoute:
    """AC1. Neuter: revert ``_pdf_signature_status`` to the sync
    ``validate_pdf_signature`` -> ``check_failed`` inside the route's running
    loop -> RED (executed in the build evidence)."""

    async def test_signed_by_trusted_ca_is_signed_valid_through_the_route(
        self, client: TestClient, trust_env: str, pki: fx.TestPki
    ) -> None:
        raw = await fx.sign_pdf_bytes(fx.blank_pdf(), pki)
        response, _col, manifest = await asyncio.to_thread(
            _index_signed_via_route, client, raw
        )
        assert response.status_code == 200, response.text
        doc = response.json()["documents"][0]
        assert doc["signature_status"] == "signed_valid"
        # The manifest row (the audit artefact) carries the same value.
        flushed = manifest.upsert_pdf_metadata.await_args.kwargs
        assert flushed["signature_status"] == "signed_valid"

    async def test_bundle_without_the_ca_is_signed_untrusted(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
        pki: fx.TestPki,
    ) -> None:
        monkeypatch.setenv(
            "AUDITTRACE_PDF_SIGNATURE_TRUST_STORE", _wrong_ca_trust_file(tmp_path)
        )
        get_settings.cache_clear()
        try:
            raw = await fx.sign_pdf_bytes(fx.blank_pdf(), pki)
            response, _col, _manifest = await asyncio.to_thread(
                _index_signed_via_route, client, raw
            )
        finally:
            get_settings.cache_clear()
        assert response.status_code == 200, response.text
        assert response.json()["documents"][0]["signature_status"] == (
            "signed_untrusted"
        )


# ───────────────────────── AC2: the worker path ─────────────────────────


class TestAc2WorkerPath:
    """AC2. Same neuter as AC1 (the worker drives the same
    ``_index_pdf_objects``)."""

    async def test_default_indexer_classifies_signed_valid(
        self, trust_env: str, pki: fx.TestPki
    ) -> None:
        from audittrace.services.index_worker import (
            IndexRequestEnvelope,
            default_indexer,
        )

        raw = await fx.sign_pdf_bytes(fx.blank_pdf(), pki)
        minio = MagicMock()
        response_obj = MagicMock()
        response_obj.read.return_value = raw
        response_obj.__enter__.return_value = response_obj
        minio.get_object.return_value = response_obj
        chroma_client = MagicMock()
        chroma_client.get_or_create_collection = AsyncMock(return_value=AsyncMock())
        manifest = AsyncMock()
        manifest.get = AsyncMock(return_value=None)
        env = IndexRequestEnvelope(
            scan_id="scan-1",
            key="episodic/papers/scan-1/signed.pdf",
            collection="ai_research_papers",
            user_id="alice",
            trace_id="t",
        )
        settings = SimpleNamespace(
            aws_bucket="", object_storage_backend="minio", minio_shared_bucket="b"
        )
        with (
            patch("audittrace.dependencies.get_chromadb", return_value=chroma_client),
            patch(
                "audittrace.dependencies.get_memory_manifest_service",
                return_value=manifest,
            ),
            patch("audittrace.routes.memory._get_minio_client", return_value=minio),
            patch(
                "audittrace.routes.memory.embed_via_nomic",
                AsyncMock(side_effect=lambda t, **_: [[0.1, 0.2, 0.3] for _ in t]),
            ),
        ):
            ok = await default_indexer(env, settings)  # type: ignore[arg-type]
        assert ok is True
        flushed = manifest.upsert_pdf_metadata.await_args.kwargs
        assert flushed["signature_status"] == "signed_valid"


# ───────────────────────── AC3: the ADR-054 retry ─────────────────────────


class TestAc3RetryPath:
    """AC3. A leaf whose notAfter is now+a few seconds, signed now, then the
    wall clock passes notAfter: trusted=False now, trusted=True as-of signing.
    freezegun is not installed and is not added (A6). Neuter: drop the
    ``await`` on the retry call -> the status is a coroutine, ``.trusted``
    raises ``AttributeError`` -> ``check_error`` -> RED."""

    async def test_expired_leaf_is_signed_expired_via_awaited_retry(self) -> None:
        now = dt.datetime.now(dt.UTC)
        pki = fx.make_test_pki(
            leaf_not_before=now - dt.timedelta(hours=1),
            leaf_not_after=now + dt.timedelta(seconds=4),
        )
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            trust = Path(td) / "t.pem"
            trust.write_bytes(pki.ca_pem)
            raw = await fx.sign_pdf_bytes(fx.blank_pdf(), pki)
            await asyncio.sleep(5)  # the leaf has now expired
            status, count = await _status(raw, str(trust))
        assert (status, count) == ("signed_expired", 1)


# ───────────────────── AC4: consecutive calls (a MEASUREMENT) ─────────────────────


class TestAc4ConsecutiveCalls:
    """Recorded measurement, not a guard (S7): the singleton context has
    ``allow_fetching=False`` so it holds no loop-bound fetchers."""

    async def test_three_consecutive_validations_in_one_loop(
        self, signed: bytes, trust_file: str
    ) -> None:
        results = [await _status(signed, trust_file) for _ in range(3)]
        assert results == [("signed_valid", 1)] * 3

    def test_two_separate_loops_share_the_singleton(
        self, pki: fx.TestPki, trust_file: str
    ) -> None:
        raw = asyncio.run(fx.sign_pdf_bytes(fx.blank_pdf(), pki))
        first = asyncio.run(_status(raw, trust_file))
        second = asyncio.run(_status(raw, trust_file))
        assert first == second == ("signed_valid", 1)
        assert sig._get_validation_context(trust_file).fetching_allowed is False


# ───────────────────────── AC5: the classification grid ─────────────────────────


def _corrupt(name: str, signed: bytes) -> bytes:
    return {
        "G1": fx.corrupt_contents_head,
        "G2": fx.zero_contents,
        "G3": lambda b: fx.corrupt_contents_at(b, 200),
        "G4": lambda b: fx.corrupt_contents_at(b, 3000),
        "G5": fx.wrong_byte_range,
        "G7": fx.truncated_xref,
    }[name](signed)


class TestAc5Grid:
    """B-2 grid. Expected values are literals, MEASURED on pyhanko 0.37.0
    (see the build evidence ``M1-M2-grid-0.37.0.txt``).

    * Neuter A: collapse ``DOCUMENT_FAILURE_TYPES`` to ``Exception`` -> G8,
      G9, G10 (and the retry/status-shape cases) go RED.
    * Neuter B: drop ``ValueError`` from the tuple -> G1 to G4 go RED."""

    @pytest.mark.parametrize(
        ("cell", "expected"),
        [
            ("G1", "check_failed"),  # /Contents head garbage: ValueError, parse step
            ("G2", "check_failed"),  # /Contents all zeros: ValueError, parse step
            ("G3", "check_failed"),  # garbage @200: ValueError, validate step
            ("G4", "check_failed"),  # garbage @3000: ValueError, validate step
            ("G5", "signed_tampered"),  # wrong ByteRange: no exception, intact=False
            ("G7", "check_failed"),  # truncated xref: PdfReadError, parse step
        ],
    )
    async def test_document_corruption_cells(
        self, cell: str, expected: str, signed: bytes, trust_file: str
    ) -> None:
        status, _ = await _status(_corrupt(cell, signed), trust_file)
        assert status == expected

    async def test_g6_empty_signature_field_is_none(self, trust_file: str) -> None:
        raw = fx.with_empty_signature_field(fx.blank_pdf())
        assert await _status(raw, trust_file) == ("none", 0)

    async def test_g8_validate_runtime_error_is_check_error(
        self, signed: bytes, trust_file: str
    ) -> None:
        with patch(ASYNC_VALIDATE, autospec=True, side_effect=RuntimeError("boom")):
            assert await _status(signed, trust_file) == ("check_error", 0)

    async def test_g9_retry_runtime_error_is_check_error(
        self, signed: bytes, tmp_path: Any
    ) -> None:
        """The primary validate is the REAL call (untrusted under the wrong
        CA), the retry raises ``RuntimeError``. Neuter: revert S5 (any retry
        exception -> ``signed_untrusted``) -> RED."""
        from pyhanko.sign.validation import async_validate_pdf_signature as real

        calls: list[int] = []

        async def first_real_then_boom(*a: Any, **k: Any) -> Any:
            calls.append(1)
            if len(calls) == 1:
                return await real(*a, **k)
            raise RuntimeError("retry boom")

        with patch(ASYNC_VALIDATE, side_effect=first_real_then_boom):
            status = await _status(signed, _wrong_ca_trust_file(tmp_path))
        assert len(calls) == 2  # the retry really ran
        assert status == ("check_error", 0)

    async def test_g10_validate_plain_key_error_is_check_error(
        self, signed: bytes, trust_file: str
    ) -> None:
        with patch(ASYNC_VALIDATE, autospec=True, side_effect=KeyError("k")):
            assert await _status(signed, trust_file) == ("check_error", 0)

    async def test_retry_document_failure_stays_signed_untrusted(
        self, signed: bytes, tmp_path: Any
    ) -> None:
        from pyhanko.sign.validation import async_validate_pdf_signature as real
        from pyhanko_certvalidator.errors import PathError

        calls: list[int] = []

        async def first_real_then_path_error(*a: Any, **k: Any) -> Any:
            calls.append(1)
            if len(calls) == 1:
                return await real(*a, **k)
            raise PathError("no path at signing time")

        with patch(ASYNC_VALIDATE, side_effect=first_real_then_path_error):
            status = await _status(signed, _wrong_ca_trust_file(tmp_path))
        assert status == ("signed_untrusted", 1)

    @pytest.mark.parametrize("missing", ["intact", "valid", "trusted"])
    async def test_status_missing_an_attribute_is_check_error(
        self, missing: str, signed: bytes, trust_file: str
    ) -> None:
        """S6. Neuter: ``getattr(status, name, True)`` defaults -> a status
        without ``trusted`` classifies ``signed_valid`` -> RED."""
        attrs = {"intact": True, "valid": True, "trusted": True}
        del attrs[missing]
        fake = SimpleNamespace(**attrs)
        with patch(ASYNC_VALIDATE, autospec=True, return_value=fake):
            assert await _status(signed, trust_file) == ("check_error", 0)

    async def test_retry_status_missing_trusted_is_check_error(
        self, signed: bytes, tmp_path: Any
    ) -> None:
        """S6 applies to the ADR-054 retry status too. Neuter: read the retry
        status with ``getattr(retry_status, "trusted", False)`` -> the missing
        attribute is silently ``signed_untrusted`` -> RED."""
        from pyhanko.sign.validation import async_validate_pdf_signature as real

        calls: list[int] = []

        async def first_real_then_shapeless(*a: Any, **k: Any) -> Any:
            calls.append(1)
            if len(calls) == 1:
                return await real(*a, **k)
            return SimpleNamespace(intact=True, valid=True)

        with patch(ASYNC_VALIDATE, side_effect=first_real_then_shapeless):
            status = await _status(signed, _wrong_ca_trust_file(tmp_path))
        assert len(calls) == 2
        assert status == ("check_error", 0)

    async def test_validation_context_failure_is_check_error_for_any_type(
        self, signed: bytes, trust_file: str
    ) -> None:
        """OUR trust-context setup failing is never the document's fault,
        even when the exception type (``ValueError``) is in the tuple."""
        with patch.object(sig, "_get_validation_context", side_effect=ValueError("x")):
            assert await _status(signed, trust_file) == ("check_error", 0)

    async def test_trust_roots_in_pem_without_cert_blocks_still_classifies(
        self, signed: bytes, tmp_path: Any
    ) -> None:
        empty = tmp_path / "empty.pem"
        empty.write_bytes(b"not a pem")
        status, _ = await _status(signed, str(empty))
        # No trust roots parsed -> falls back to system roots; the CA is not
        # among them, and there are no cached roots for the retry.
        assert status == "signed_untrusted"


class TestImportSmoke:
    """T-new (B-1): every name in the tuple resolves, and the nonexistent
    ``WeakHashAlgorithmError`` is NOT in it (an import failure inside the
    classifier's import ``try`` would turn every document into
    ``check_unavailable``)."""

    def test_every_listed_type_resolves_to_its_real_module_path(self) -> None:
        import importlib

        from audittrace.routes.memory_pdf.signature_errors import (
            DOCUMENT_FAILURE_TYPES,
        )

        expected = {
            "builtins.ValueError",
            "pyhanko.pdf_utils.misc.PdfError",
            "pyhanko.sign.general.NonexistentAttributeError",
            "pyhanko_certvalidator.errors.ValidationError",
            "pyhanko_certvalidator.errors.PathError",
            "pyhanko_certvalidator.errors.CRLValidationError",
            "pyhanko_certvalidator.errors.OCSPValidationError",
            "cryptography.exceptions.InvalidSignature",
        }
        assert {f"{t.__module__}.{t.__qualname__}" for t in DOCUMENT_FAILURE_TYPES} == (
            expected
        )
        for dotted in expected:
            module, _, name = dotted.rpartition(".")
            assert getattr(importlib.import_module(module), name) in (
                DOCUMENT_FAILURE_TYPES
            )

    def test_the_three_certvalidator_invalid_signature_subclasses_are_covered(
        self,
    ) -> None:
        from pyhanko_certvalidator import errors

        from audittrace.routes.memory_pdf.signature_errors import (
            DOCUMENT_FAILURE_TYPES,
        )

        for name in (
            "PSSParameterMismatch",
            "DSAParametersUnavailable",
            "AlgorithmNotSupported",
        ):
            assert issubclass(getattr(errors, name), DOCUMENT_FAILURE_TYPES)

    def test_weak_hash_algorithm_error_does_not_exist_and_is_not_imported(
        self,
    ) -> None:
        import importlib
        import pathlib

        import pyhanko

        root = pathlib.Path(next(iter(pyhanko.__path__)))
        assert not any(
            "class WeakHashAlgorithmError" in f.read_text() for f in root.rglob("*.py")
        )
        src = importlib.import_module(
            "audittrace.routes.memory_pdf.signature_errors"
        ).__file__
        assert src is not None
        assert "WeakHashAlgorithmError" not in pathlib.Path(src).read_text().replace(
            "``WeakHashAlgorithmError``", ""
        )


# ─────────────── AC5: log line, metric, join key (AC13 mechanics) ───────────────


class TestAc5ObservabilityOfCheckError:
    """Neuter: log ``check_error`` at WARNING / skip the counter / drop the
    ``file`` + ``document_sha256`` extras -> the matching assertion goes RED."""

    async def test_check_error_logs_error_with_join_fields_and_counts(
        self,
        signed: bytes,
        trust_file: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        provider = TracerProvider()
        tracer = provider.get_tracer("test")
        counter = MagicMock()
        with (
            patch(ASYNC_VALIDATE, autospec=True, side_effect=RuntimeError("boom")),
            patch(
                "audittrace.services.pdf_signature_telemetry._pdf_signature_checks_total",
                counter,
            ),
            tracer.start_as_current_span("t") as span,
            caplog.at_level(logging.DEBUG),
        ):
            status = await _status(
                signed,
                trust_file,
                key="episodic/papers/x.pdf",
                document_sha256="ab" * 32,
            )
            trace_hex = format(span.get_span_context().trace_id, "032x")
        assert status == ("check_error", 0)
        records = [r for r in caplog.records if r.levelno == logging.ERROR]
        rec = next(
            r
            for r in records
            if getattr(r, "reason", "") == ("signature_check_internal_error")
        )
        assert rec.file == "episodic/papers/x.pdf"  # type: ignore[attr-defined]
        assert rec.document_sha256 == "ab" * 32  # type: ignore[attr-defined]
        assert rec.trace_id == trace_hex  # type: ignore[attr-defined]
        counter.add.assert_called_once_with(1, {"status": "check_error"})

    async def test_check_failed_logs_warning_not_error_and_counts(
        self,
        signed: bytes,
        trust_file: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        counter = MagicMock()
        with (
            patch(
                "audittrace.services.pdf_signature_telemetry._pdf_signature_checks_total",
                counter,
            ),
            caplog.at_level(logging.DEBUG),
        ):
            status = await _status(fx.truncated_xref(signed), trust_file)
        assert status == ("check_failed", 0)
        warned = [
            r
            for r in caplog.records
            if getattr(r, "reason", "") == "signature_check_exception"
        ]
        assert warned and all(r.levelno == logging.WARNING for r in warned)
        assert not [
            r
            for r in caplog.records
            if r.levelno >= logging.ERROR
            and (getattr(r, "reason", "") == "signature_check_internal_error")
        ]
        counter.add.assert_called_once_with(1, {"status": "check_failed"})

    async def test_every_outcome_is_counted_including_skipped_and_unavailable(
        self, signed: bytes, trust_file: str
    ) -> None:
        counter = MagicMock()
        with patch(
            "audittrace.services.pdf_signature_telemetry._pdf_signature_checks_total",
            counter,
        ):
            await _pdf_signature_status(b"x", enabled=False, trust_store_path="")
            await _status(signed, trust_file)
            with patch.dict("sys.modules", {"pyhanko.pdf_utils.reader": None}):
                await _status(signed, trust_file)
        labels = [c.args[1]["status"] for c in counter.add.call_args_list]
        assert labels == ["check_skipped", "signed_valid", "check_unavailable"]

    async def test_trace_id_is_none_without_an_active_span(self) -> None:
        assert sig._current_trace_id() is None


# ─────────────────────── AC10: the taxonomy enumerations ───────────────────────

CANONICAL_CODES = frozenset(
    {
        "check_skipped",
        "check_unavailable",
        "check_failed",
        "check_error",
        "none",
        "signed_valid",
        "signed_invalid",
        "signed_untrusted",
        "signed_expired",
        "signed_tampered",
    }
)


class TestAc10TaxonomyEnumerations:
    """Every machine-checkable enumeration of the signature codes equals the
    canonical 10. Neuter: omit ``check_error`` from any one of them -> RED."""

    def test_the_code_tuple(self) -> None:
        assert sig._SIGNATURE_STATUS_CODES == CANONICAL_CODES

    def test_the_webui_chip_switch_handles_check_error(self) -> None:
        from pathlib import Path

        html = (
            Path(__file__).resolve().parent.parent / "webui" / "index.html"
        ).read_text()
        for code in CANONICAL_CODES:
            assert f"'{code}'" in html, code

    def test_the_readme_taxonomy_line_lists_every_code(self) -> None:
        from pathlib import Path

        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
        line = next(
            ln for ln in readme.splitlines() if "Audit-Grade PDF Ingestion" in ln
        )
        assert "10-class" in line
        for code in CANONICAL_CODES:
            assert f"`{code}`" in line, code

    def test_the_grafana_panel_selects_check_error_from_the_counter(self) -> None:
        import json
        from pathlib import Path

        dash = json.loads(
            (
                Path(__file__).resolve().parent.parent
                / "charts/audittrace/files/grafana-dashboards"
                / "audittrace-scan-pipeline.json"
            ).read_text()
        )
        exprs = [
            t["expr"]
            for p in dash["panels"]
            for t in p.get("targets", [])
            if "audittrace_pdf_signature_checks_total" in t.get("expr", "")
        ]
        assert any('status="check_error"' in e for e in exprs)
        assert any("by (status)" in e for e in exprs)

    def test_the_status_doc_and_adr_addenda_say_ten(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "docs"
        assert "10-class" in (root / "architecture/pdf-ingestion-status.md").read_text()
        assert (
            "check_error"
            in (root / "ADR-052-pades-trust-store-and-taxonomy.md").read_text()
        )
        assert (
            "check_error"
            in (root / "ADR-054-pades-as-of-signing-time-validation.md").read_text()
        )


# ───────────────────────── gates: pin bounds + CI step ─────────────────────────


class TestDependencyPinsAndCi:
    """Neuter: widen a pin / delete the CI step -> RED."""

    def test_pyhanko_pins_are_bounded_in_pyproject_and_requirements(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        for name in ("pyproject.toml", "requirements.txt"):
            text = (root / name).read_text()
            assert "pyhanko[etsi,async-http]>=0.37,<0.38" in text, name
            assert "pyhanko-certvalidator>=0.32,<0.33" in text, name

    def test_resolved_versions_are_inside_the_bounds(self) -> None:
        from importlib.metadata import version

        assert version("pyhanko").startswith("0.37.")
        assert version("pyhanko-certvalidator").startswith("0.32.")

    def test_ci_runs_make_typecheck(self) -> None:
        from pathlib import Path

        ci = (
            Path(__file__).resolve().parent.parent / ".github/workflows/ci.yml"
        ).read_text()
        assert "run: make typecheck" in ci
