"""Tests for the ratified Mongo-audit classification (WU-A1, spec §6, §9 T12).

Spec: ``2026-09-25-SPEC-mongo-audit-sanctioned-read-route-CONSOLIDATED-v5.md``
(``sha256:b47de202681f36114e5568531e8ed11d320dbc63e3c4a976292d34663035ff72``).

T12 — ``CLASSIFICATION_VERSION == sha256(canonical(mapping))``; every value in the three
classes; the 47 names of spec §6 present with spec §6's class (USER 34, OPERATIONAL 13);
``keyv``/``keyv.files``/``keyv.chunks`` absent (removed from v1's table — spec §6.2, §1 #37).
Neuter: flip one class → the version test and the membership test both go RED.
"""

from __future__ import annotations

import hashlib
import json

from audittrace.services.mongo_audit_classification import (
    CLASSIFICATION,
    CLASSIFICATION_VERSION,
    classify,
)

# The exact ratified table (ADDENDUM S, spec §6.1/§6.2) — reproduced here as the test's own
# independent literal so a code change to the module's mapping is caught by a value mismatch,
# not just a version-hash mismatch.
_EXPECTED_USER = {
    "conversations",
    "messages",
    "presets",
    "prompts",
    "promptgroups",
    "chatprojects",
    "files",
    "agents",
    "conversationtags",
    "toolfavorites",
    "memoryentries",
    "sharedlinks",
    "pluginauths",
    "keys",
    "actions",
    "assistants",
    "toolcalls",
    "schedules",
    "scheduleruns",
    "transactions",
    "balances",
    "agentapikeys",
    "mcpservers",
    "aclentries",
    "skills",
    "skillfiles",
    "skillsynccredentials",
    "skillsyncstatuses",
    "agenttriggerdeliveries",
    "agenttriggerlanesequences",
    "tokens",
    "agent_checkpoints",
    "agent_checkpoint_writes",
    "projects",
}
_EXPECTED_OPERATIONAL = {
    "users",
    "sessions",
    "roles",
    "accessroles",
    "systemgrants",
    "groups",
    "banners",
    "configs",
    "auditlogs",
    "agentcategories",
    "agenttriggeruserpurges",
    "logs",
    "__transaction_test__",
}


class TestClassificationCensus:
    """The 47-row table reconciles to 34 USER + 13 OPERATIONAL (spec §6, §9 T12)."""

    def test_total_is_47(self) -> None:
        assert len(CLASSIFICATION) == 47

    def test_user_count_is_34(self) -> None:
        user = {name for name, cls in CLASSIFICATION.items() if cls == "USER"}
        assert user == _EXPECTED_USER
        assert len(user) == 34

    def test_operational_count_is_13(self) -> None:
        operational = {
            name for name, cls in CLASSIFICATION.items() if cls == "OPERATIONAL"
        }
        assert operational == _EXPECTED_OPERATIONAL
        assert len(operational) == 13

    def test_every_value_is_one_of_the_three_classes(self) -> None:
        assert set(CLASSIFICATION.values()) <= {"USER", "OPERATIONAL", "UNCLASSIFIED"}
        # Every declared row is USER or OPERATIONAL — UNCLASSIFIED is never a mapping value,
        # only classify()'s fallback for a name absent from the mapping and outside the
        # `system.` prefix rule.
        assert set(CLASSIFICATION.values()) == {"USER", "OPERATIONAL"}

    def test_removed_v1_names_are_absent(self) -> None:
        # v1's table wrongly listed these three; ADDENDUM S removed them (spec §6.2, §1 #37):
        # `keyv` is the keyv library's class default, overridden at the sole instantiation site
        # (`keyvMongo.ts:274-276`), and `keyv.files`/`keyv.chunks` never existed.
        for removed in ("keyv", "keyv.files", "keyv.chunks"):
            assert removed not in CLASSIFICATION


# The ratified table's version, pinned as a literal (derived once from the correct mapping via
# the module's own hashing formula — see the docstring below — never hand-typed). A version
# test that instead recomputed this from `CLASSIFICATION` itself would be tautological: it would
# stay GREEN even if a row's class silently flipped, because both sides of the assertion read
# the same (possibly wrong) mapping. Pinning the literal is what makes T12's neuter ("flip one
# class") turn this test RED, not just the membership test
# ([[feedback_vacuous_neuter_test_antipattern]]).
_RATIFIED_CLASSIFICATION_VERSION = (
    "edbb3b70fed61e1a1d87d2e8440da16793459f70600ca6dfbc4f08ef080a515c"
)


class TestClassificationVersion:
    """``CLASSIFICATION_VERSION`` is the sha256 of the canonical (sorted, compact) JSON
    serialisation of the mapping (spec §6)."""

    def test_version_matches_the_ratified_literal(self) -> None:
        assert CLASSIFICATION_VERSION == _RATIFIED_CLASSIFICATION_VERSION

    def test_version_formula_is_the_documented_canonical_hash(self) -> None:
        # Proves the *formula* independently of the *value* pinned above: recomputing the
        # canonical hash from today's live mapping must equal the module's own constant.
        canonical = json.dumps(
            dict(sorted(CLASSIFICATION.items())), separators=(",", ":")
        )
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        assert CLASSIFICATION_VERSION == expected

    def test_version_is_hex_sha256_shape(self) -> None:
        assert len(CLASSIFICATION_VERSION) == 64
        int(CLASSIFICATION_VERSION, 16)  # raises ValueError if not hex


class TestClassify:
    """``classify()`` — exact match, then the single ``system.`` prefix rule (spec §5.4)."""

    def test_exact_match_user(self) -> None:
        assert classify("conversations") == "USER"

    def test_exact_match_operational(self) -> None:
        assert classify("users") == "OPERATIONAL"

    def test_unknown_name_is_unclassified(self) -> None:
        assert classify("somethingnew") == "UNCLASSIFIED"

    def test_system_prefix_is_operational(self) -> None:
        assert classify("system.views") == "OPERATIONAL"
        assert classify("system.profile") == "OPERATIONAL"

    def test_system_buckets_carve_out_is_unclassified(self) -> None:
        # MongoDB >= 5 time-series *data* lives under system.buckets.<name> — that is data, not
        # Mongo's own bookkeeping (spec §5.4, v2-A-5, §9 T6b).
        assert classify("system.buckets.readings") == "UNCLASSIFIED"

    def test_non_system_names_resembling_the_prefix_are_unclassified(self) -> None:
        assert classify("systemfoo") == "UNCLASSIFIED"
        assert classify("xsystem.views") == "UNCLASSIFIED"
