"""Closed set of exception types that mean "this DOCUMENT could not be validated".

Spec #460 Addendum B-1. ``_pdf_signature_status`` splits every exception from
its pyhanko call sites by TYPE, with no call-site guessing:

* an instance of :data:`DOCUMENT_FAILURE_TYPES` -> ``check_failed`` ("the
  document could not be validated");
* anything else (``RuntimeError``, ``TypeError``, ``AttributeError``, a plain
  ``KeyError``, ...) -> ``check_error`` ("our call into the validator failed").

Why a literal tuple and not a base class: pyhanko raises document problems
from several unrelated hierarchies, and asn1crypto parses lazily, so the SAME
corrupt-signature defect surfaces as a builtin ``ValueError`` from the reader
step or the validate step depending on the byte offset (measured on pyhanko
0.37.0 / pyhanko-certvalidator 0.32.1: grid cells G1-G4).

This module imports pyhanko at module level on purpose: the caller imports it
inside its existing ``try/except ImportError`` so a missing extra degrades to
``check_unavailable`` rather than breaking module import. Every name below is
imported by its real module path on the resolved versions; a test asserts each
one resolves (``WeakHashAlgorithmError`` does NOT exist and must never be
listed: an ImportError here would turn every document into
``check_unavailable``).
"""

from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from pyhanko.pdf_utils.misc import PdfError
from pyhanko.sign.general import NonexistentAttributeError
from pyhanko_certvalidator.errors import (
    CRLValidationError,
    OCSPValidationError,
    PathError,
    ValidationError,
)

DOCUMENT_FAILURE_TYPES: tuple[type[BaseException], ...] = (
    # Covers pyhanko ValueErrorWithMessage -> SignatureValidationError,
    # SuspiciousModification, MultivaluedAttributeError and asn1crypto's
    # lazy-parse errors.
    ValueError,
    PdfError,  # pyhanko.pdf_utils.misc (PdfReadError and friends)
    NonexistentAttributeError,  # pyhanko.sign.general (a KeyError subclass)
    ValidationError,  # pyhanko_certvalidator.errors (PathValidationError, ...)
    PathError,  # pyhanko_certvalidator.errors (PathBuildingError, ...)
    CRLValidationError,  # pyhanko_certvalidator.errors
    OCSPValidationError,  # pyhanko_certvalidator.errors
    # cryptography.exceptions; the three pyhanko_certvalidator subclasses
    # (PSSParameterMismatch, DSAParametersUnavailable, AlgorithmNotSupported)
    # are instances of it.
    InvalidSignature,
)
