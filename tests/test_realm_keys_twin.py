"""BFF-CHAT-ROUTE-ERRORS ADDENDUM-3: the BFF and the memory server select the
realm's verifying key with the SAME code.

They ship in separate images and share no package, so the module exists
twice. If the two copies drift, the two services disagree about which key may
verify a token. Neuter: change one byte of either file -> red.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_the_two_copies_are_byte_equal() -> None:
    bff = (REPO_ROOT / "bff" / "realm_keys.py").read_bytes()
    server = (REPO_ROOT / "src" / "audittrace" / "realm_keys.py").read_bytes()
    assert bff == server
    assert b"def select(" in bff  # not two empty files
