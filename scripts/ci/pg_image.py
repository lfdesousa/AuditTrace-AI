"""The ONE Postgres image every test/CI Postgres runs (LC-9 TEST-PG-PARITY-18).

The definition lives in ``scripts/ci/pg_image.mk`` (a make fragment the
Makefile ``include``s, so make needs no external tool and CI-shaped runs with
a stripped ``PATH`` still resolve it). This module parses that one line and
exposes it to Python spawn sites as :data:`PG_TEST_IMAGE`; it fails closed
if the assignment is missing or ambiguous.
"""

from __future__ import annotations

import re
from pathlib import Path

_MK = Path(__file__).with_name("pg_image.mk")
_ASSIGN_RX = re.compile(r"^PG_TEST_IMAGE\s*[:?]?=\s*(\S+)\s*$", re.MULTILINE)


def read_pg_test_image(mk_text: str) -> str:
    """Parse the single ``PG_TEST_IMAGE = <image>`` assignment from make text."""
    found = _ASSIGN_RX.findall(mk_text)
    if len(found) != 1:
        raise ValueError(
            f"expected exactly one PG_TEST_IMAGE assignment, found {len(found)}"
        )
    return found[0]


PG_TEST_IMAGE = read_pg_test_image(_MK.read_text(encoding="utf-8"))
