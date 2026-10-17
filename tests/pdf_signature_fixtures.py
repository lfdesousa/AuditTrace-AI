"""Generated-in-test PAdES fixtures: a throwaway CA, a leaf, a signed PDF.

Nothing here is copied from a real signed document. Every certificate and
key is generated per call, so the fixtures are reproducible and licence-clean
(spec #460 AC1). Signing uses pyhanko's native async signer so the helpers
are safe to call from inside a running event loop.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from dataclasses import dataclass

import pymupdf
from asn1crypto import pem as asn1_pem
from asn1crypto import x509 as asn1_x509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pyhanko.keys import load_private_key_from_pemder_data
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.sign import signers
from pyhanko.sign.fields import SigFieldSpec, append_signature_field
from pyhanko_certvalidator.registry import SimpleCertificateStore


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _to_asn1(cert: x509.Certificate) -> asn1_x509.Certificate:
    pem_bytes = cert.public_bytes(serialization.Encoding.PEM)
    return asn1_x509.Certificate.load(asn1_pem.unarmor(pem_bytes)[2])


@dataclass
class TestPki:
    """A self-signed CA plus one leaf that signs PDFs."""

    __test__ = False  # not a pytest class

    ca_cert: x509.Certificate
    leaf_cert: x509.Certificate
    leaf_key_pem: bytes

    @property
    def ca_pem(self) -> bytes:
        return self.ca_cert.public_bytes(serialization.Encoding.PEM)

    def signer(self) -> signers.SimpleSigner:
        return signers.SimpleSigner(
            signing_cert=_to_asn1(self.leaf_cert),
            signing_key=load_private_key_from_pemder_data(self.leaf_key_pem, None),
            cert_registry=SimpleCertificateStore.from_certs([_to_asn1(self.ca_cert)]),
        )


def make_test_pki(
    *,
    leaf_not_before: dt.datetime | None = None,
    leaf_not_after: dt.datetime | None = None,
    ca_name: str = "CA",
) -> TestPki:
    """Generate a CA + leaf. Leaf validity defaults to [now-1d, now+10d].

    Serial numbers are fixed (1 and 2) so the CMS byte layout, and therefore
    the corruption offsets in the AC5 grid, is deterministic across runs.
    """
    now = dt.datetime.now(dt.UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name(ca_name))
        .issuer_name(_name(ca_name))
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(now - dt.timedelta(days=2))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("Signer"))
        .issuer_name(_name(ca_name))
        .public_key(leaf_key.public_key())
        .serial_number(2)
        .not_valid_before(leaf_not_before or now - dt.timedelta(days=1))
        .not_valid_after(leaf_not_after or now + dt.timedelta(days=10))
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key_pem = leaf_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return TestPki(ca_cert=ca_cert, leaf_cert=leaf_cert, leaf_key_pem=key_pem)


def blank_pdf(text: str = "audit fixture") -> bytes:
    """One-page unsigned PDF with a text layer."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    data: bytes = doc.tobytes()
    doc.close()
    return data


async def sign_pdf_bytes(
    raw: bytes,
    pki: TestPki,
    *,
    field_name: str = "Sig1",
    moment: dt.datetime | None = None,
) -> bytes:
    """PAdES-sign *raw* with the fixture leaf via the async signer."""
    writer = IncrementalPdfFileWriter(io.BytesIO(raw))
    meta = signers.PdfSignatureMetadata(field_name=field_name)
    pdf_signer = signers.PdfSigner(meta, signer=pki.signer())
    out = await pdf_signer.async_sign_pdf(writer)
    return out.getvalue()


def with_empty_signature_field(raw: bytes) -> bytes:
    """Unsigned PDF carrying an empty signature field."""
    writer = IncrementalPdfFileWriter(io.BytesIO(raw))
    append_signature_field(writer, SigFieldSpec("Empty"))
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _contents_span(signed: bytes) -> tuple[int, int]:
    match = re.search(rb"/Contents\s*<([0-9a-fA-F]+)>", signed)
    assert match is not None
    return match.span(1)


def corrupt_contents_head(signed: bytes) -> bytes:
    """G1: garbage at the head of /Contents."""
    start, _ = _contents_span(signed)
    out = bytearray(signed)
    out[start : start + 64] = b"ab" * 32
    return bytes(out)


def zero_contents(signed: bytes) -> bytes:
    """G2: /Contents all zeros."""
    start, end = _contents_span(signed)
    out = bytearray(signed)
    out[start:end] = b"0" * (end - start)
    return bytes(out)


def corrupt_contents_at(signed: bytes, offset: int) -> bytes:
    """G3/G4: 40 bytes of garbage at *offset* hex chars into /Contents."""
    start, _ = _contents_span(signed)
    out = bytearray(signed)
    out[start + offset : start + offset + 40] = b"f" * 40
    return bytes(out)


def wrong_byte_range(signed: bytes) -> bytes:
    """G5: shrink the second ByteRange value by 10."""
    match = re.search(rb"/ByteRange\s*\[\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*\]", signed)
    assert match is not None
    original = match.group(0)
    second = match.group(2)
    patched = original.replace(
        second, str(int(second) - 10).encode().rjust(len(second))
    )
    return signed.replace(original, patched.ljust(len(original)))


def truncated_xref(signed: bytes) -> bytes:
    """G7: cut the tail off the file (EOF/xref lost)."""
    return signed[: len(signed) - 200]
