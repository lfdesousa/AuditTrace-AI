"""Spec #460 / GH #366 — Swiss TSL builder: call shape, XSW, failure split,
composite metadata.

Resolved versions these mechanism claims were measured on: pyhanko 0.37.0,
pyhanko-certvalidator 0.32.1, signxml 5.1.0.

Individual neuters (each makes exactly the named test(s) RED):

* AC6   drop ``validation_time`` from the call           -> TestAc6CallShape
* AC7   catch the internal error in the composite        -> TestAc7FailureSplit
* AC8   derive ``contributing_builders`` from the config -> TestAc8Contributors
* AC14  pass ``tl_xml`` (not the verified XML) to the    -> TestAc14XmlSignatureWrapping
        extractor
"""

from __future__ import annotations

import ast
import asyncio
import base64
import datetime as dt
import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from pyhanko.sign.validation.errors import SignatureValidationError
from pyhanko.sign.validation.qualified import eutl_parse
from signxml.xades import XAdESSigner

from audittrace.services.trust_store import (
    CompositeTrustStoreBuilder,
    S3TrustStoreProvider,
    SwissTslTrustStoreBuilder,
    TrustStoreBuilder,
    TrustStoreBuilderInternalError,
    TrustStoreBuilderUnavailableError,
    TrustStoreBundle,
    TrustStoreMetadata,
    _bundle_from_pem,
)

FN_PATH = (
    "pyhanko.sign.validation.qualified.eutl_parse."
    "_validate_and_extract_tl_data_multiple_certs"
)
REAL_FN = eutl_parse._validate_and_extract_tl_data_multiple_certs
NS = "http://uri.etsi.org/02231/v2#"
C14N_11 = "http://www.w3.org/2006/12/xml-c14n11"
C14N_INCLUSIVE = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"


# ───────────────────────────── fixtures ─────────────────────────────────


def _mkcert(cn: str) -> tuple[Any, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _service(cert: x509.Certificate) -> str:
    b64 = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()
    return (
        "<tsl:TrustServiceProvider><tsl:TSPServices><tsl:TSPService>"
        "<tsl:ServiceInformation><tsl:ServiceTypeIdentifier>"
        "http://uri.etsi.org/TrstSvc/Svctype/CA/QC"
        "</tsl:ServiceTypeIdentifier><tsl:ServiceDigitalIdentity><tsl:DigitalId>"
        f"<tsl:X509Certificate>{b64}</tsl:X509Certificate>"
        "</tsl:DigitalId></tsl:ServiceDigitalIdentity></tsl:ServiceInformation>"
        "</tsl:TSPService></tsl:TSPServices></tsl:TrustServiceProvider>"
    )


class _Tsl:
    """A signed test TSL plus the wrapped variants of it."""

    def __init__(self, c14n: str, tmp_path: Path) -> None:
        tslo_key, self.tslo = _mkcert("Test TSLO")
        _, self.good = _mkcert("Good QC CA")
        _, self.evil = _mkcert("Evil CA")
        # B-3: an ``Id`` reference (``#TSL-1``) with c14n 1.1 or inclusive
        # c14n. ``URI=""`` and exclusive c14n cannot yield a verifying
        # wrapped variant (measured, pyhanko 0.37.0 / signxml 5.1.0).
        tsl = (
            f'<tsl:TrustServiceStatusList xmlns:tsl="{NS}" Id="TSL-1" '
            'TSLTag="http://uri.etsi.org/19612/TSLTag">'
            f"<tsl:TrustServiceProviderList>{_service(self.good)}"
            "</tsl:TrustServiceProviderList></tsl:TrustServiceStatusList>"
        )
        signer = XAdESSigner(
            signature_algorithm="rsa-sha256",
            digest_algorithm="sha256",
            c14n_algorithm=c14n,
        )
        signed = signer.sign(
            etree.fromstring(tsl.encode()),
            key=tslo_key,
            cert=[self.tslo],
            reference_uri="#TSL-1",
        )
        self.signed_xml = etree.tostring(signed).decode()
        self.tslo_path = tmp_path / "tslo.pem"
        self.tslo_path.write_bytes(self.tslo.public_bytes(serialization.Encoding.PEM))

    def _wrapper(self) -> Any:
        return etree.fromstring(
            (
                f'<tsl:TrustServiceStatusList xmlns:tsl="{NS}">'
                f"<tsl:TrustServiceProviderList>{_service(self.evil)}"
                "</tsl:TrustServiceProviderList><tsl:SchemeInformation/>"
                "</tsl:TrustServiceStatusList>"
            ).encode()
        )

    def xsw_nested(self) -> str:
        """New unsigned root carrying an evil provider; the original signed
        TSL nested inside it. The signature still verifies."""
        wrapper = self._wrapper()
        wrapper.find(f"{{{NS}}}SchemeInformation").append(
            etree.fromstring(self.signed_xml.encode())
        )
        return etree.tostring(wrapper).decode()

    def xsw_signature_at_root(self) -> str:
        orig = etree.fromstring(self.signed_xml.encode())
        sig = orig.find("{http://www.w3.org/2000/09/xmldsig#}Signature")
        orig.remove(sig)
        wrapper = self._wrapper()
        wrapper.find(f"{{{NS}}}SchemeInformation").append(orig)
        wrapper.append(sig)
        return etree.tostring(wrapper).decode()

    def der(self, cert: x509.Certificate) -> bytes:
        return cert.public_bytes(serialization.Encoding.DER)


def _fake_session(xml: str) -> Any:
    class _Resp:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *a: Any) -> bool:
            return False

        def raise_for_status(self) -> None:
            return None

        async def text(self) -> str:
            return xml

    class _Session:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *a: Any) -> bool:
            return False

        def get(self, *a: Any, **k: Any) -> Any:
            return _Resp()

    return _Session()


def _build(builder: SwissTslTrustStoreBuilder, xml: str) -> TrustStoreBundle:
    with patch("aiohttp.ClientSession", return_value=_fake_session(xml)):
        return asyncio.run(builder.build())


@pytest.fixture(params=[C14N_11, C14N_INCLUSIVE], ids=["c14n11", "c14n10-inclusive"])
def tsl(request: pytest.FixtureRequest, tmp_path: Path) -> _Tsl:
    return _Tsl(request.param, tmp_path)


# ─────────────── AC14: XML-signature-wrapping regression ───────────────


class TestAc14XmlSignatureWrapping:
    def test_fixture_is_a_real_xsw_pyhanko_verifies_and_the_raw_walk_ingests(
        self, tsl: _Tsl
    ) -> None:
        """Precondition (SF1): the wrapped variants VERIFY, the verified
        output excludes the injected CA, and a raw walk of the network bytes
        would ingest it. Without this the neuter below would be vacuous."""
        from audittrace.services.trust_store import _extract_qc_certs_from_swiss_tsl

        tslo_ax = _asn1(tsl.tslo)
        for wrapped in (tsl.xsw_nested(), tsl.xsw_signature_at_root()):
            verified = REAL_FN(wrapped, [tslo_ax], None)
            assert tsl.der(tsl.evil) not in _extract_qc_certs_from_swiss_tsl(verified)
            assert tsl.der(tsl.good) in _extract_qc_certs_from_swiss_tsl(verified)
            raw = _extract_qc_certs_from_swiss_tsl(wrapped)
            assert len(raw) == 2
            assert tsl.der(tsl.evil) in raw

    @pytest.mark.parametrize("variant", ["xsw_nested", "xsw_signature_at_root"])
    def test_injected_ca_is_absent_from_the_bundle(
        self, tsl: _Tsl, variant: str
    ) -> None:
        """Neuter: pass ``tl_xml`` instead of ``verified_xml`` to the
        extractor -> the injected CA is in the bundle -> RED."""
        builder = SwissTslTrustStoreBuilder(tslo_cert_path=tsl.tslo_path)
        bundle = _build(builder, getattr(tsl, variant)())
        pem = bundle.pem_bytes
        assert _pem_of(tsl.good) in pem
        assert _pem_of(tsl.evil) not in pem
        assert bundle.metadata.cert_count == 1

    def test_unwrapped_signed_tsl_yields_the_good_ca(self, tsl: _Tsl) -> None:
        builder = SwissTslTrustStoreBuilder(tslo_cert_path=tsl.tslo_path)
        bundle = _build(builder, tsl.signed_xml)
        assert _pem_of(tsl.good) in bundle.pem_bytes
        assert bundle.metadata.cert_count == 1

    def test_tampering_inside_the_signed_element_fails_verification(
        self, tsl: _Tsl
    ) -> None:
        root = etree.fromstring(tsl.signed_xml.encode())
        root.find(f"{{{NS}}}TrustServiceProviderList").append(
            etree.fromstring(f'<tsl:X xmlns:tsl="{NS}">{_service(tsl.evil)}</tsl:X>')[0]
        )
        builder = SwissTslTrustStoreBuilder(tslo_cert_path=tsl.tslo_path)
        with pytest.raises(TrustStoreBuilderUnavailableError):
            _build(builder, etree.tostring(root).decode())


def _asn1(cert: x509.Certificate) -> Any:
    from asn1crypto import x509 as ax

    return ax.Certificate.load(cert.public_bytes(serialization.Encoding.DER))


def _pem_of(cert: x509.Certificate) -> bytes:
    b64 = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()
    wrapped = "\n".join(b64[i : i + 64] for i in range(0, len(b64), 64))
    return (
        f"-----BEGIN CERTIFICATE-----\n{wrapped}\n-----END CERTIFICATE-----\n".encode()
    )


# ─────────────────────── AC6: the call shape ───────────────────────


class TestAc6CallShape:
    def test_the_exact_call_binds_against_the_real_pyhanko_signature(
        self, tsl: _Tsl
    ) -> None:
        """The bind test is the guard, not the mock: the recorder is NOT
        autospec'd, so a dropped ``validation_time`` is caught here.
        Neuter: drop the ``None`` argument -> ``TypeError`` at bind -> RED."""
        recorder = MagicMock(return_value=tsl.signed_xml)
        builder = SwissTslTrustStoreBuilder(tslo_cert_path=tsl.tslo_path)
        with patch(FN_PATH, recorder):
            _build(builder, tsl.signed_xml)
        recorder.assert_called_once()
        args, kwargs = recorder.call_args
        bound = inspect.signature(REAL_FN).bind(*args, **kwargs)
        # ``None`` means "verify at the current time" (pyhanko passes it as
        # ``verification_time`` to ``XAdESSignatureConfiguration``).
        assert bound.arguments["validation_time"] is None
        assert bound.arguments["tl_xml"] == tsl.signed_xml
        assert [
            c.subject.human_friendly for c in bound.arguments["tlso_cert_candidates"]
        ] == ["Common Name: Test TSLO"]

    def test_the_real_signature_requires_validation_time(self) -> None:
        """Pins the premise of AC6 on the resolved pyhanko: the parameter is
        required (no default), so omitting it is a ``TypeError`` at bind."""
        param = inspect.signature(REAL_FN).parameters["validation_time"]
        assert param.default is inspect.Parameter.empty
        with pytest.raises(TypeError):
            inspect.signature(REAL_FN).bind("<xml/>", [])

    def test_every_pyhanko_patch_in_tests_uses_autospec(self) -> None:
        """A mock without autospec accepts any call shape, which is how
        #366's drift reached main. Every ``patch`` of a pyhanko symbol in
        ``tests/`` (this file included) must pass ``autospec=True`` unless it
        replaces the target with an explicit object (positional ``new``).
        Targets are string literals OR module-level constants holding one.
        Neuter: remove ``autospec=True`` from any such patch, including a
        constant-target one -> RED."""
        offenders: list[str] = []
        for path in sorted(Path(__file__).parent.rglob("*.py")):
            offenders += find_unspecced_pyhanko_patches(path.read_text(), path.name)
        assert offenders == []

    @pytest.mark.parametrize(
        "source",
        [
            'import x\ndef t():\n    x.patch("pyhanko.a.b")\n',
            'T = "pyhanko.a.b"\ndef t():\n    patch(T, side_effect=E)\n',
            'T = "pyhanko_certvalidator.a"\ndef t():\n    patch(T, autospec=False)\n',
        ],
        ids=["literal", "constant-name", "autospec-false"],
    )
    def test_the_autospec_scanner_flags_unspecced_literal_and_constant_targets(
        self, source: str
    ) -> None:
        assert find_unspecced_pyhanko_patches(source, "t.py")

    @pytest.mark.parametrize(
        "source",
        [
            'T = "pyhanko.a"\ndef t():\n    patch(T, autospec=True)\n',
            'T = "pyhanko.a"\ndef t():\n    patch(T, replacement)\n',
            'T = "other.mod"\ndef t():\n    patch(T)\n',
            "def t(n):\n    patch(n)\n",
        ],
        ids=["autospec", "explicit-object", "non-pyhanko", "unresolvable-name"],
    )
    def test_the_autospec_scanner_accepts_specced_or_unrelated_patches(
        self, source: str
    ) -> None:
        assert find_unspecced_pyhanko_patches(source, "t.py") == []


def find_unspecced_pyhanko_patches(source: str, filename: str) -> list[str]:
    tree = ast.parse(source)
    constants = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    out: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        first = node.args[0]
        target = (
            first.value
            if isinstance(first, ast.Constant)
            else constants.get(first.id)
            if isinstance(first, ast.Name)
            else None
        )
        if name != "patch" or not isinstance(target, str):
            continue
        if not target.startswith(("pyhanko.", "pyhanko_certvalidator.")):
            continue
        if len(node.args) >= 2:  # explicit replacement object
            continue
        autospec = next((k.value for k in node.keywords if k.arg == "autospec"), None)
        if not (isinstance(autospec, ast.Constant) and autospec.value is True):
            out.append(f"{filename}:{node.lineno} {target}")
    return out


# ───────────────────────── AC7: the failure split ─────────────────────────


def _builder_with_patched_fn(tsl: _Tsl, **patch_kw: Any) -> Any:
    builder = SwissTslTrustStoreBuilder(tslo_cert_path=tsl.tslo_path)
    patcher = patch(FN_PATH, autospec=True, **patch_kw)
    return builder, patcher


class TestAc7FailureSplit:
    @pytest.mark.parametrize("exc", [TypeError("bad call"), AttributeError("nope")])
    def test_call_shape_drift_is_an_internal_error_never_blaming_the_list(
        self, tsl: _Tsl, exc: Exception
    ) -> None:
        """Neuter: fold ``TypeError``/``AttributeError`` back into
        ``Unavailable`` -> RED (and the message would say "tampered")."""
        builder, patcher = _builder_with_patched_fn(tsl, side_effect=exc)
        with patcher, pytest.raises(TrustStoreBuilderInternalError) as info:
            _build(builder, tsl.signed_xml)
        message = str(info.value).lower()
        assert "stale" not in message
        assert "tampered" not in message
        assert not isinstance(info.value, TrustStoreBuilderUnavailableError)

    def test_a_real_verification_failure_stays_unavailable(self, tsl: _Tsl) -> None:
        builder, patcher = _builder_with_patched_fn(
            tsl, side_effect=SignatureValidationError("bad sig")
        )
        with patcher, pytest.raises(TrustStoreBuilderUnavailableError):
            _build(builder, tsl.signed_xml)

    @pytest.mark.parametrize("payload", [None, "", b"", 5])
    def test_non_conforming_verified_payload_fails_closed(
        self, tsl: _Tsl, payload: Any
    ) -> None:
        """B1 rule 2. Neuter: drop the str/bytes + non-empty check -> RED."""
        builder, patcher = _builder_with_patched_fn(tsl, return_value=payload)
        with patcher, pytest.raises(TrustStoreBuilderInternalError):
            _build(builder, tsl.signed_xml)

    def test_bytes_payload_is_accepted(self, tsl: _Tsl) -> None:
        builder, patcher = _builder_with_patched_fn(
            tsl, return_value=tsl.signed_xml.encode()
        )
        with patcher:
            bundle = _build(builder, tsl.signed_xml)
        assert bundle.metadata.cert_count == 1

    def test_an_extractor_failure_is_unavailable(self, tsl: _Tsl) -> None:
        builder, patcher = _builder_with_patched_fn(tsl, return_value="<not-xml")
        with (
            patcher,
            pytest.raises(TrustStoreBuilderUnavailableError, match="XML walk failed"),
        ):
            _build(builder, tsl.signed_xml)

    def test_internal_error_is_not_a_subclass_of_unavailable(self) -> None:
        assert not issubclass(
            TrustStoreBuilderInternalError, TrustStoreBuilderUnavailableError
        )

    def test_composite_does_not_catch_the_internal_error(self, tsl: _Tsl) -> None:
        """A3. Neuter: ``except (Unavailable, Internal)`` in the composite ->
        the composite returns a bundle -> RED."""
        ok = _StubBuilder("eu_lotl", _pem_of(tsl.good))
        swiss, patcher = _builder_with_patched_fn(tsl, side_effect=TypeError("x"))
        composite = CompositeTrustStoreBuilder([ok, swiss])
        with (
            patch("aiohttp.ClientSession", return_value=_fake_session(tsl.signed_xml)),
            patcher,
            pytest.raises(TrustStoreBuilderInternalError),
        ):
            asyncio.run(composite.build())

    def test_refresh_route_fails_closed_and_keeps_the_stored_bundle(
        self, tsl: _Tsl, client: Any
    ) -> None:
        """A3 + AC7 extended: non-2xx typed error; the stored bundle is
        byte-identical (sha256 before = after) and ``store`` is never
        called. Neuter: catch the error in the route and store -> RED."""
        from audittrace.dependencies import (
            get_trust_store_builder,
            get_trust_store_provider,
        )
        from audittrace.services.trust_store import MockTrustStoreProvider

        provider = MockTrustStoreProvider()
        provider.store(
            _bundle_from_pem(
                _pem_of(tsl.good), builder_id="eu_lotl", source_url="x", cert_count=1
            )
        )
        before = provider.metadata().sha256  # type: ignore[union-attr]
        swiss, patcher = _builder_with_patched_fn(tsl, side_effect=TypeError("x"))
        composite = CompositeTrustStoreBuilder(
            [_StubBuilder("eu_lotl", _pem_of(tsl.good)), swiss]
        )
        client.app.dependency_overrides[get_trust_store_builder] = lambda: composite
        client.app.dependency_overrides[get_trust_store_provider] = lambda: provider
        try:
            with (
                patch(
                    "aiohttp.ClientSession",
                    return_value=_fake_session(tsl.signed_xml),
                ),
                patcher,
            ):
                response = client.post("/system/trust-store/refresh")
        finally:
            client.app.dependency_overrides.clear()
        assert response.status_code == 500
        detail = response.json()["detail"]
        assert detail["error"] == "trust_store_build_internal_error"
        assert "tampered" not in detail["cause"].lower()
        assert provider.metadata().sha256 == before  # type: ignore[union-attr]


class _StubBuilder(TrustStoreBuilder):
    def __init__(
        self, builder_id: str, pem: bytes = b"", fail: Exception | None = None
    ) -> None:
        self._id = builder_id
        self._pem = pem
        self._fail = fail

    @property
    def builder_id(self) -> str:
        return self._id

    async def build(self) -> TrustStoreBundle:
        if self._fail is not None:
            raise self._fail
        return _bundle_from_pem(
            self._pem,
            builder_id=self._id,
            source_url=f"stub://{self._id}",
            cert_count=self._pem.count(b"BEGIN CERTIFICATE"),
        )


# ─────────────────────── AC8: contributors metadata ───────────────────────


class TestAc8Contributors:
    def test_failed_swiss_is_recorded_as_failed_and_not_contributing(
        self, tsl: _Tsl
    ) -> None:
        """B-4: an UNAVAILABLE-type failure (pyhanko ``SignatureValidationError``
        from the TSL check). Neuter: derive ``contributing_builders`` from the
        configured list -> RED."""
        swiss, patcher = _builder_with_patched_fn(
            tsl, side_effect=SignatureValidationError("bad sig")
        )
        composite = CompositeTrustStoreBuilder(
            [_StubBuilder("eu_lotl", _pem_of(tsl.good)), swiss]
        )
        with (
            patch("aiohttp.ClientSession", return_value=_fake_session(tsl.signed_xml)),
            patcher,
        ):
            bundle = asyncio.run(composite.build())
        meta = bundle.metadata
        assert meta.builder_id == "eu_lotl+swiss_tsl"  # configured, unchanged
        assert meta.contributing_builders == ["eu_lotl"]
        assert meta.failed_builders is not None
        assert meta.failed_builders[0]["builder_id"] == "swiss_tsl"
        assert "bad sig" in meta.failed_builders[0]["reason"]

    def test_all_succeeding_lists_every_builder_in_order(self) -> None:
        composite = CompositeTrustStoreBuilder(
            [
                _StubBuilder("a", b"-----BEGIN CERTIFICATE-----\n"),
                _StubBuilder("b", b"-----BEGIN CERTIFICATE-----\n"),
            ]
        )
        meta = asyncio.run(composite.build()).metadata
        assert meta.contributing_builders == ["a", "b"]
        assert meta.failed_builders == []

    def test_non_composite_builders_record_themselves(self, tsl: _Tsl) -> None:
        bundle = _build(
            SwissTslTrustStoreBuilder(tslo_cert_path=tsl.tslo_path), tsl.signed_xml
        )
        assert bundle.metadata.contributing_builders == ["swiss_tsl"]
        assert bundle.metadata.failed_builders == []

    def test_old_sidecar_reads_back_as_unknown_never_inferred(self) -> None:
        """A bundle stored before #460 has no new fields: ``None`` means
        "unknown", never a list inferred from ``builder_id``."""
        old = {
            "sha256": "ab" * 32,
            "builder_id": "eu_lotl+swiss_tsl",
            "built_at": "2026-05-31T16:59:33+00:00",
            "cert_count": 908,
            "source_url": "eu_lotl, swiss_tsl",
        }
        meta = TrustStoreMetadata.from_dict(old)
        assert meta.contributing_builders is None
        assert meta.failed_builders is None
        assert meta.to_dict()["contributing_builders"] is None

    def test_s3_provider_round_trips_old_and_new_sidecars(self) -> None:
        store: dict[str, bytes] = {}

        class _Resp:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, *a: Any) -> bool:
                return False

            def read(self) -> bytes:
                return self._data

        client = MagicMock()
        client.get_object.side_effect = lambda b, k: _Resp(store[k])
        client.put_object.side_effect = lambda b, k, stream, **kw: store.__setitem__(
            k, stream.read()
        )
        provider = S3TrustStoreProvider(client, "bucket")
        bundle = _bundle_from_pem(
            b"-----BEGIN CERTIFICATE-----\n",
            builder_id="swiss_tsl",
            source_url="u",
            cert_count=1,
        )
        provider.store(bundle)
        assert provider.metadata().contributing_builders == ["swiss_tsl"]  # type: ignore[union-attr]
        assert provider.load().metadata.failed_builders == []
        # Now an OLD sidecar (no new keys) is read as unknown.
        legacy = json.loads(store["trust-store/eu-lotl-bundle.metadata.json"])
        del legacy["contributing_builders"], legacy["failed_builders"]
        store["trust-store/eu-lotl-bundle.metadata.json"] = json.dumps(legacy).encode()
        assert provider.metadata().contributing_builders is None  # type: ignore[union-attr]
        assert provider.load().metadata.failed_builders is None

    def test_metadata_route_exposes_the_new_fields(self, client: Any) -> None:
        from audittrace.dependencies import get_trust_store_provider
        from audittrace.services.trust_store import MockTrustStoreProvider

        provider = MockTrustStoreProvider()
        provider.store(
            _bundle_from_pem(
                b"-----BEGIN CERTIFICATE-----\n",
                builder_id="eu_lotl",
                source_url="u",
                cert_count=1,
            )
        )
        client.app.dependency_overrides[get_trust_store_provider] = lambda: provider
        try:
            body = client.get("/system/trust-store").json()
        finally:
            client.app.dependency_overrides.clear()
        assert body["contributing_builders"] == ["eu_lotl"]
        assert body["failed_builders"] == []
