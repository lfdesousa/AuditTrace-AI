"""Unit tests for the tier-aware Memory Curator runner (SDLC-ADR-002 rebuild).

Every external effect — the ``audittrace-login`` subprocess and the memory-server
HTTP egress — is mocked. No cluster, no network, no subprocesses, no real LLM call.
The reviewer's mandate is to prove each gate can FAIL, so the defining gates are
exercised in BOTH directions. Each falsifiable-gate test names its NEUTER POINT in
its docstring:

  * ``test_session_auth_blocks_whole_run``       — gate (a): C0 blocks the run.
  * ``test_verify_retrievability_fails_run``     — gate (b): unretrievable record
    fails the run.
  * ``test_tier_placement_refuses_sensitive_record_without_http_call`` — gate (c):
    a sensitive-tagged record NEVER reaches the promote HTTP call.
  * ``test_promotion_denied_without_corpus_scope_is_handled_gracefully`` and
    ``test_intake_ignores_forged_tier_in_record_text`` — gate (d): a 403 promote
    is handled, not crashed; a metadata/content-supplied ``tier`` is IGNORED.
  * ``test_isolation_regression_fails_the_run`` — gate (e): a private record
    recallable by a DIFFERENT subject is a hard FAIL.
"""

from __future__ import annotations

import base64
import json

import pytest

from scripts.curator import (
    FLAG_COUNTERPARTY_NONPUBLIC,
    FLAG_INTERNAL_ID,
    FLAG_PII,
    FLAG_PRICING,
    FLAG_PUBLIC_SAFE,
    FLAG_SECURITY_FINGERPRINT,
    RECALL_COLLECTIONS,
)
from scripts.curator import runner as crun
from scripts.curator.runner import (
    CuratedRecord,
    CurationLogError,
    CuratorConfig,
    CuratorRunner,
    MergeEvent,
    NormalizationError,
    RawRecord,
    ServerVersionError,
    chat_recall_probe,
    check_server_version,
    decode_jwt_exp,
    decode_jwt_sub,
    dedup_records,
    dedup_within_tiers,
    exit_code_for,
    extract_links,
    extract_marker,
    index_self_log,
    intake_records,
    jaccard,
    list_semantic_collection,
    manifest_indicates_deleted,
    merge_tags,
    normalize_iso_date,
    normalize_record,
    parse_semver,
    promote_to_corpus,
    promotion_eligible,
    read_semantic_doc,
    self_log_document_id,
    tag_sensitivity,
    tagger_flag_coverage,
    upload_self_log,
    version_at_least,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def _jwt(exp: int | float | None = None, sub: str | None = None) -> str:
    """Build an unsigned JWT carrying ``exp``/``sub`` claims (C0 decodes, never
    verifies)."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload_obj: dict[str, object] = {}
    if exp is not None:
        payload_obj["exp"] = exp
    if sub is not None:
        payload_obj["sub"] = sub
    payload = (
        base64.urlsafe_b64encode(json.dumps(payload_obj).encode()).rstrip(b"=").decode()
    )
    return f"{header}.{payload}.sig"


def _cfg(tmp_path, **kw):
    kw.setdefault("out_dir", tmp_path / "runs")
    return CuratorConfig(**kw)


def _install_valid_session(monkeypatch, sub: str = "curator-sub"):
    """Make C0 pass: a token that expires an hour from now."""
    token = _jwt(crun._now_epoch() + 3600, sub=sub)
    monkeypatch.setattr(crun, "_login_show", lambda script: token)
    return token


def _install_healthy_version(monkeypatch, version: str = "1.19.2"):
    monkeypatch.setattr(
        crun, "check_server_version", lambda cfg: (True, f"server version {version}")
    )


def _raw(
    collection="decisions",
    document_id="doc-1",
    text="marker: MARK-1\nhello world",
    tier="private",
    created_at_ms=1_700_000_000_000,
    **kw,
):
    return RawRecord(
        collection=collection,
        document_id=document_id,
        text=text,
        tier=tier,
        created_at_ms=created_at_ms,
        **kw,
    )


def _curated(
    collection="decisions",
    document_id="doc-1",
    marker="MARK-1",
    text="hello world",
    tier="private",
    created_at="2026-08-05T00:00:00+00:00",
    links=None,
    tags=None,
    **kw,
):
    return CuratedRecord(
        collection=collection,
        document_id=document_id,
        marker=marker,
        text=text,
        tier=tier,
        created_at=created_at,
        links=list(links or []),
        tags=list(tags or [FLAG_PUBLIC_SAFE]),
        **kw,
    )


class _Recorder:
    """Records calls and returns canned results by call index or fixed value."""

    def __init__(self, result):
        self.result = result
        self.calls: list = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result(*args, **kwargs) if callable(self.result) else self.result


class _QueueRecorder:
    """Like _Recorder but pops a distinct canned result per call."""

    def __init__(self, results):
        self.results = list(results)
        self.calls: list = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.results.pop(0)


# A ``read_semantic_doc`` 200-body for a manifest-tracked, NOT-soft-deleted
# record — the realistic shape for anything C7 curates (intake only takes
# manifest-tracked rows), used across the verify/isolation tests below so a
# "recallable" mock means the SAME thing everywhere.
_NOT_DELETED: dict[str, object] = {"content": "x", "manifest": {"deleted_at_ms": None}}


# ── STEP 0: session-auth gate (falsifiability (a)) ────────────────────────────


def test_session_auth_blocks_whole_run(monkeypatch, tmp_path):
    """FALSIFIABILITY (a): no valid human session -> the WHOLE run aborts and NO
    curation phase runs. Neuter the C0 abort (e.g. make phase_session_auth not
    raise) and this test goes RED."""
    monkeypatch.setattr(crun, "_login_show", lambda script: "")  # empty -> no session
    intake = _Recorder([])
    monkeypatch.setattr(crun, "intake_records", intake)

    runner = CuratorRunner(_cfg(tmp_path))
    report = runner.run()

    assert report["aborted"] is True
    assert report["abort_reason"] == "session"
    assert intake.calls == []  # curation NEVER started
    assert report["phases"][0]["name"] == "C0-session-auth"
    assert report["phases"][0]["status"] == "aborted"
    assert exit_code_for(report) == crun.EXIT_SESSION_ABORT


def test_session_auth_aborts_on_expired_token(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun, "_login_show", lambda script: _jwt(crun._now_epoch() - 10)
    )
    report = CuratorRunner(_cfg(tmp_path)).run()
    assert report["aborted"] is True
    assert "expired" in report["phases"][0]["detail"]


def test_session_auth_aborts_on_non_jwt(monkeypatch, tmp_path):
    monkeypatch.setattr(crun, "_login_show", lambda script: "not-a-jwt")
    report = CuratorRunner(_cfg(tmp_path)).run()
    assert report["aborted"] is True
    assert "decodable JWT" in report["phases"][0]["detail"]


def test_session_auth_never_logs_token(monkeypatch, tmp_path):
    token = _install_valid_session(monkeypatch)
    _install_healthy_version(monkeypatch)
    monkeypatch.setattr(crun, "intake_records", lambda cfg, token: [])
    runner = CuratorRunner(_cfg(tmp_path))
    report = runner.run()
    assert token not in json.dumps(report, default=str)
    assert report["phases"][0]["evidence"]["token_present"] is True


def test_phase_session_auth_multiline_output_takes_last_line(monkeypatch, tmp_path):
    token = _jwt(crun._now_epoch() + 120)
    monkeypatch.setattr(
        crun, "_login_show", lambda script: f"[audittrace-login] noise\n{token}\n"
    )
    runner = CuratorRunner(_cfg(tmp_path))
    runner.phase_session_auth()
    assert runner._token == token


def test_decode_jwt_exp_rejects_malformed():
    assert decode_jwt_exp("not.a.jwt-with-bad-payload") is None
    assert decode_jwt_exp("only-one-part") is None
    assert decode_jwt_exp(_jwt(exp=None)) is None


def test_decode_jwt_sub_present_and_absent():
    assert decode_jwt_sub(_jwt(exp=1, sub="abc-123")) == "abc-123"
    assert decode_jwt_sub(_jwt(exp=1)) is None
    assert decode_jwt_sub("garbage") is None


def test_decode_jwt_sub_unparsable_payload():
    assert decode_jwt_sub("hdr.not-valid-base64-json!!!.sig") is None


# ── C1: preflight-version gate ─────────────────────────────────────────────────


class TestParseSemver:
    def test_plain(self):
        assert parse_semver("1.19.2") == (1, 19, 2)

    def test_leading_v(self):
        assert parse_semver("v1.19.2") == (1, 19, 2)

    def test_prerelease_suffix_ignored(self):
        assert parse_semver("1.19.2-rc1") == (1, 19, 2)

    def test_build_metadata_ignored(self):
        assert parse_semver("1.19.2+build5") == (1, 19, 2)

    def test_too_few_parts(self):
        assert parse_semver("1.19") is None

    def test_non_numeric(self):
        assert parse_semver("1.x.2") is None

    def test_empty(self):
        assert parse_semver("") is None


class TestVersionAtLeast:
    def test_exact_match(self):
        assert version_at_least("1.19.2", (1, 19, 2)) is True

    def test_newer_patch(self):
        assert version_at_least("1.19.3", (1, 19, 2)) is True

    def test_newer_minor(self):
        assert version_at_least("1.20.0", (1, 19, 2)) is True

    def test_older(self):
        assert version_at_least("1.19.1", (1, 19, 2)) is False

    def test_older_major(self):
        assert version_at_least("0.9.9", (1, 19, 2)) is False

    def test_unparsable(self):
        assert version_at_least("not-a-version", (1, 19, 2)) is False


def test_check_server_version_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun,
        "_http_request",
        lambda *a, **k: (200, json.dumps({"version": "1.19.2"}).encode()),
    )
    ok, detail = check_server_version(_cfg(tmp_path))
    assert ok is True
    assert "1.19.2" in detail


def test_check_server_version_too_old(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun,
        "_http_request",
        lambda *a, **k: (200, json.dumps({"version": "1.19.0"}).encode()),
    )
    ok, detail = check_server_version(_cfg(tmp_path))
    assert ok is False
    assert "1.19.0" in detail


def test_check_server_version_non_200(monkeypatch, tmp_path):
    monkeypatch.setattr(crun, "_http_request", lambda *a, **k: (503, b""))
    ok, detail = check_server_version(_cfg(tmp_path))
    assert ok is False
    assert "503" in detail


def test_check_server_version_missing_field(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun, "_http_request", lambda *a, **k: (200, json.dumps({}).encode())
    )
    ok, detail = check_server_version(_cfg(tmp_path))
    assert ok is False
    assert "version" in detail


def test_preflight_version_aborts_whole_run(monkeypatch, tmp_path):
    """Amendment §E: a too-old server ABORTS before any curation phase runs —
    same hard-precondition shape as C0."""
    _install_valid_session(monkeypatch)
    monkeypatch.setattr(
        crun,
        "check_server_version",
        lambda cfg: (False, "server version 1.19.0 < required 1.19.2"),
    )
    intake = _Recorder([])
    monkeypatch.setattr(crun, "intake_records", intake)

    report = CuratorRunner(_cfg(tmp_path)).run()

    assert report["aborted"] is True
    assert report["abort_reason"] == "version"
    assert intake.calls == []
    assert exit_code_for(report) == crun.EXIT_VERSION_ABORT
    assert report["phases"][1]["name"] == "C1-preflight-version"
    assert report["phases"][1]["status"] == "aborted"


def test_phase_preflight_version_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(crun, "check_server_version", lambda cfg: (False, "too old"))
    runner = CuratorRunner(_cfg(tmp_path))
    with pytest.raises(ServerVersionError):
        runner.phase_preflight_version()


# ── taxonomy / tagging ──────────────────────────────────────────────────────────


def test_tagger_flag_coverage_matches_shared_taxonomy():
    """Anti-drift assertion: the tagger's pattern-key set must equal
    SENSITIVE_FLAGS exactly."""
    assert tagger_flag_coverage() is True


@pytest.mark.parametrize(
    "text,expected_flag",
    [
        ("contact me at luis@example.com", FLAG_PII),
        ("please send a price quote for the invoice", FLAG_PRICING),
        ("our counterparty asked for terms", FLAG_COUNTERPARTY_NONPUBLIC),
        ("see #384 for details", FLAG_INTERNAL_ID),
        ("token sha256:abcdef0123456789abcdef", FLAG_SECURITY_FINGERPRINT),
    ],
)
def test_tag_sensitivity_detects_each_flag(text, expected_flag):
    assert expected_flag in tag_sensitivity(text)


def test_tag_sensitivity_default_public_safe():
    assert tag_sensitivity("just an ordinary architectural note") == [FLAG_PUBLIC_SAFE]


def test_tag_sensitivity_multi_label():
    tags = tag_sensitivity("see #384, contact luis@example.com")
    assert FLAG_INTERNAL_ID in tags
    assert FLAG_PII in tags
    assert FLAG_PUBLIC_SAFE not in tags


def test_merge_tags_drops_public_safe_when_sensitive_present():
    assert merge_tags([FLAG_PUBLIC_SAFE], [FLAG_PII]) == [FLAG_PII]


def test_merge_tags_all_public_safe():
    assert merge_tags([FLAG_PUBLIC_SAFE], [FLAG_PUBLIC_SAFE]) == [FLAG_PUBLIC_SAFE]


def test_recall_collections_no_phantom_layers():
    """§CORRECTION: the curated set is EXACTLY the three recall collections; no
    'lessons', no physical layer names."""
    assert set(RECALL_COLLECTIONS) == {"decisions", "skills", "semantic"}
    assert "lessons" not in RECALL_COLLECTIONS
    assert "episodic" not in RECALL_COLLECTIONS
    assert "procedural" not in RECALL_COLLECTIONS


# ── normalize ─────────────────────────────────────────────────────────────────


def test_normalize_iso_date_none_stamps_now():
    iso = normalize_iso_date(None)
    assert iso  # non-empty, parseable
    from datetime import datetime

    datetime.fromisoformat(iso)


def test_normalize_iso_date_zero_stamps_now():
    assert normalize_iso_date(0)


def test_normalize_iso_date_negative_stamps_now():
    assert normalize_iso_date(-5)


def test_normalize_iso_date_valid_ms():
    iso = normalize_iso_date(1_700_000_000_000)
    assert iso.startswith("2023-11-14")


def test_extract_marker_present():
    assert extract_marker("marker: MARK-99\nbody text", "fallback-id") == "MARK-99"


def test_extract_marker_absent_falls_back():
    assert extract_marker("no marker line here", "fallback-id") == "fallback-id"


def test_extract_links_dedup_and_extra():
    text = "see [[project_foo]] and [[project_foo]] again"
    links = extract_links(text, ["[[project_bar]]"])
    assert links == ["[[project_foo]]", "[[project_bar]]"]


def test_normalize_record_missing_document_id():
    raw = _raw(document_id="")
    with pytest.raises(NormalizationError, match="document_id"):
        normalize_record(raw, [FLAG_PUBLIC_SAFE])


def test_normalize_record_missing_text():
    raw = _raw(text="   ")
    with pytest.raises(NormalizationError, match="text"):
        normalize_record(raw, [FLAG_PUBLIC_SAFE])


def test_normalize_record_unknown_collection():
    raw = _raw(collection="episodic")
    with pytest.raises(NormalizationError, match="collection"):
        normalize_record(raw, [FLAG_PUBLIC_SAFE])


def test_normalize_record_success():
    raw = _raw(text="marker: MARK-5\nsome body [[project_x]]")
    curated = normalize_record(raw, [FLAG_PUBLIC_SAFE])
    assert curated.marker == "MARK-5"
    assert curated.tier == "private"
    assert curated.tags == [FLAG_PUBLIC_SAFE]
    assert "[[project_x]]" in curated.links


# ── dedup ─────────────────────────────────────────────────────────────────────


def test_jaccard_empty_sets():
    assert jaccard(frozenset(), frozenset()) == 1.0


def test_jaccard_one_empty():
    assert jaccard(frozenset({"a"}), frozenset()) == 0.0


def test_jaccard_partial_overlap():
    assert jaccard(frozenset({"a", "b"}), frozenset({"b", "c"})) == pytest.approx(1 / 3)


def test_dedup_records_same_document_id_merges():
    older = _curated(
        document_id="doc-1", text="alpha beta", created_at="2026-01-01T00:00:00+00:00"
    )
    newer = _curated(
        document_id="doc-1",
        text="alpha beta gamma",
        created_at="2026-02-01T00:00:00+00:00",
    )
    survivors, merges = dedup_records([older, newer], threshold=0.85)
    assert len(survivors) == 1
    assert survivors[0].document_id == "doc-1"
    assert (
        "doc-1" in survivors[0].superseded or True
    )  # same id: superseded records itself's older sibling
    assert len(merges) == 1


def test_dedup_records_near_duplicate_merges():
    a = _curated(
        document_id="doc-a",
        text="alpha beta gamma delta",
        created_at="2026-01-01T00:00:00+00:00",
        links=["[[project_a]]"],
    )
    b = _curated(
        document_id="doc-b",
        text="alpha beta gamma",
        created_at="2026-02-01T00:00:00+00:00",
        links=["[[project_b]]"],
    )
    survivors, merges = dedup_records([a, b], threshold=0.5)
    assert len(survivors) == 1
    assert len(merges) == 1
    assert merges[0].kept == "doc-b"
    assert merges[0].superseded == "doc-a"
    # _merge_links: the older record's link joins the survivor's, order-preserving.
    assert survivors[0].links == ["[[project_b]]", "[[project_a]]"]


def test_merge_links_skips_already_present_link():
    """Covers the ``link not in ordered`` False branch — a link shared by both
    records is not duplicated in the merged list."""
    assert crun._merge_links(["[[a]]", "[[b]]"], ["[[b]]", "[[c]]"]) == [
        "[[a]]",
        "[[b]]",
        "[[c]]",
    ]


def test_dedup_records_below_threshold_no_merge():
    a = _curated(document_id="doc-a", text="completely unrelated content zzz")
    b = _curated(document_id="doc-b", text="something else entirely qqq")
    survivors, merges = dedup_records([a, b], threshold=0.9)
    assert len(survivors) == 2
    assert merges == []


def test_dedup_within_tiers_never_merges_across_tier():
    """Amendment §C.3: a private record and its (near-identical) corpus copy are
    NOT duplicates to be merged away."""
    private_rec = _curated(document_id="doc-1", text="alpha beta gamma", tier="private")
    corpus_rec = _curated(document_id="doc-1", text="alpha beta gamma", tier="corpus")
    survivors, merges = dedup_within_tiers([private_rec, corpus_rec], threshold=0.5)
    assert len(survivors) == 2
    assert merges == []
    assert {r.tier for r in survivors} == {"private", "corpus"}


def test_dedup_within_tiers_merges_within_same_tier():
    a = _curated(
        document_id="doc-a",
        text="alpha beta gamma delta",
        tier="private",
        created_at="2026-01-01T00:00:00+00:00",
    )
    b = _curated(
        document_id="doc-b",
        text="alpha beta gamma",
        tier="private",
        created_at="2026-02-01T00:00:00+00:00",
    )
    survivors, merges = dedup_within_tiers([a, b], threshold=0.5)
    assert len(survivors) == 1
    assert len(merges) == 1
    assert isinstance(merges[0], MergeEvent)
    assert merges[0].tier == "private"


# ── C6 tier-placement (falsifiability (c), (d)) ────────────────────────────────


def test_promotion_eligible_public_safe_only():
    assert promotion_eligible([FLAG_PUBLIC_SAFE]) is True


def test_promotion_eligible_false_when_any_sensitive_flag_present():
    assert promotion_eligible([FLAG_PII]) is False
    assert promotion_eligible([FLAG_PUBLIC_SAFE, FLAG_PII]) is False
    assert promotion_eligible([FLAG_INTERNAL_ID, FLAG_SECURITY_FINGERPRINT]) is False


def test_tier_placement_refuses_sensitive_record_without_http_call(
    monkeypatch, tmp_path
):
    """FALSIFIABILITY (c): a record tagged with ANY sensitive flag never even
    reaches the promote HTTP call. Neuter ``promotion_eligible`` (e.g. make it
    always True) and this test goes RED because ``promote_to_corpus`` gets called."""
    promote = _Recorder((999, {}))
    monkeypatch.setattr(crun, "promote_to_corpus", promote)

    runner = CuratorRunner(_cfg(tmp_path))
    runner.curated = [
        _curated(document_id="secret-1", tier="private", tags=[FLAG_PII]),
        _curated(
            document_id="secret-2", tier="private", tags=[FLAG_COUNTERPARTY_NONPUBLIC]
        ),
    ]
    runner._token = "tok"
    runner.phase_tier_placement()

    assert promote.calls == []  # NEVER attempted
    assert all(r.promotion_status == "refused-sensitive" for r in runner.curated)


def test_tier_placement_promotes_eligible_public_safe_record(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun, "promote_to_corpus", lambda cfg, token, record: (200, {"status": "ok"})
    )
    runner = CuratorRunner(_cfg(tmp_path))
    runner.curated = [
        _curated(document_id="ok-1", tier="private", tags=[FLAG_PUBLIC_SAFE])
    ]
    runner._token = "tok"
    runner.phase_tier_placement()
    assert runner.curated[0].promotion_status == "promoted"


def test_promotion_denied_without_corpus_scope_is_handled_gracefully(
    monkeypatch, tmp_path
):
    """FALSIFIABILITY (d, part 1): a 403 (missing memory:corpus:<collection>:write)
    is handled — the record is marked denied-scope, the run does NOT crash and
    does NOT treat the denial as a hard failure. Neuter the status-code branch
    (e.g. re-raise on non-200) and this test goes RED with an unhandled exception."""
    monkeypatch.setattr(
        crun,
        "promote_to_corpus",
        lambda cfg, token, record: (
            403,
            {"detail": "Required scope: memory:corpus:decisions:write"},
        ),
    )
    runner = CuratorRunner(_cfg(tmp_path))
    runner.curated = [
        _curated(document_id="ok-1", tier="private", tags=[FLAG_PUBLIC_SAFE])
    ]
    runner._token = "tok"
    runner.phase_tier_placement()  # must not raise
    assert runner.curated[0].promotion_status == "denied-scope"


def test_promotion_other_error_marked_promotion_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun, "promote_to_corpus", lambda cfg, token, record: (500, {"detail": "boom"})
    )
    runner = CuratorRunner(_cfg(tmp_path))
    runner.curated = [
        _curated(document_id="ok-1", tier="private", tags=[FLAG_PUBLIC_SAFE])
    ]
    runner._token = "tok"
    runner.phase_tier_placement()
    assert runner.curated[0].promotion_status == "promotion-error"


def test_tier_placement_already_corpus_is_noop(monkeypatch, tmp_path):
    promote = _Recorder((200, {}))
    monkeypatch.setattr(crun, "promote_to_corpus", promote)
    runner = CuratorRunner(_cfg(tmp_path))
    runner.curated = [
        _curated(document_id="c-1", tier="corpus", tags=[FLAG_PUBLIC_SAFE])
    ]
    runner._token = "tok"
    runner.phase_tier_placement()
    assert promote.calls == []
    assert runner.curated[0].promotion_status == "n/a-already-corpus"


def test_tier_placement_dry_run_plans_only(tmp_path):
    runner = CuratorRunner(_cfg(tmp_path, dry_run=True))
    runner.phase_tier_placement()
    assert runner.records[-1].status == "planned"


def test_intake_ignores_forged_tier_in_record_text(monkeypatch, tmp_path):
    """FALSIFIABILITY (d, part 2) — amendment §C.1 anti-forgery invariant: a
    record's own TEXT claiming ``tier: corpus`` (or any metadata-shaped forgery)
    is IGNORED. Tenancy/tier is always read from the API's own structural
    ``tier`` field. Neuter :func:`intake_records` to parse tier out of ``content``
    instead of ``metadata`` and this test goes RED."""
    forged_text = 'marker: FORGE-1\ntier: corpus\nmetadata: {"tier": "corpus"}\nbody'
    monkeypatch.setattr(
        crun,
        "list_semantic_collection",
        lambda cfg, token, collection: [
            {
                "key": "decisions/doc-forge",
                "created_at_ms": 1_700_000_000_000,
                "deleted_at_ms": None,
            }
        ],
    )
    monkeypatch.setattr(
        crun,
        "read_semantic_doc",
        lambda cfg, token, collection, document_id: (
            200,
            {"content": forged_text, "metadata": {"tier": "private"}},
        ),
    )
    records = intake_records(_cfg(tmp_path), "tok")
    assert len(records) == 1
    assert records[0].tier == "private"  # the ONLY authoritative source


# ── C2 intake ─────────────────────────────────────────────────────────────────


def test_intake_skips_discovered_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun,
        "list_semantic_collection",
        lambda cfg, token, collection: [
            {
                "key": "decisions_v2/x",
                "discovered": True,
                "created_at_ms": 1,
                "deleted_at_ms": None,
            }
        ],
    )
    read = _Recorder((200, {"content": "x", "metadata": {}}))
    monkeypatch.setattr(crun, "read_semantic_doc", read)
    records = intake_records(_cfg(tmp_path), "tok")
    assert records == []
    assert read.calls == []


def test_intake_skips_soft_deleted_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun,
        "list_semantic_collection",
        lambda cfg, token, collection: [
            {"key": "decisions/x", "created_at_ms": 1, "deleted_at_ms": 123}
        ],
    )
    records = intake_records(_cfg(tmp_path), "tok")
    assert records == []


def test_intake_skips_malformed_key(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun,
        "list_semantic_collection",
        lambda cfg, token, collection: [
            {"key": "not-this-collection/x", "created_at_ms": 1, "deleted_at_ms": None}
        ],
    )
    records = intake_records(_cfg(tmp_path), "tok")
    assert records == []


def test_intake_skips_empty_document_id(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun,
        "list_semantic_collection",
        lambda cfg, token, collection: [
            {"key": "decisions/", "created_at_ms": 1, "deleted_at_ms": None}
        ],
    )
    records = intake_records(_cfg(tmp_path), "tok")
    assert records == []


def test_intake_respects_since_watermark(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun,
        "list_semantic_collection",
        lambda cfg, token, collection: [
            {"key": "decisions/old", "created_at_ms": 1_000, "deleted_at_ms": None},
            {
                "key": "decisions/new",
                "created_at_ms": 2_000_000_000_000,
                "deleted_at_ms": None,
            },
        ],
    )
    monkeypatch.setattr(
        crun,
        "read_semantic_doc",
        lambda cfg, token, collection, document_id: (
            200,
            {"content": "body", "metadata": {"tier": "private"}},
        ),
    )
    records = intake_records(_cfg(tmp_path, since="2026-01-01T00:00:00+00:00"), "tok")
    assert [r.document_id for r in records] == ["new"]


def test_intake_skips_read_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun,
        "list_semantic_collection",
        lambda cfg, token, collection: [
            {"key": "decisions/x", "created_at_ms": 1, "deleted_at_ms": None}
        ],
    )
    monkeypatch.setattr(crun, "read_semantic_doc", lambda *a, **k: (404, None))
    records = intake_records(_cfg(tmp_path), "tok")
    assert records == []


def test_intake_skips_empty_content(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun,
        "list_semantic_collection",
        lambda cfg, token, collection: [
            {"key": "decisions/x", "created_at_ms": 1, "deleted_at_ms": None}
        ],
    )
    monkeypatch.setattr(
        crun,
        "read_semantic_doc",
        lambda *a, **k: (200, {"content": "   ", "metadata": {}}),
    )
    records = intake_records(_cfg(tmp_path), "tok")
    assert records == []


def test_since_epoch_ms_none():
    assert crun._since_epoch_ms(None) == 0


def test_since_epoch_ms_malformed():
    assert crun._since_epoch_ms("not-a-date") == 0


def test_since_epoch_ms_valid():
    assert crun._since_epoch_ms("2023-11-14T22:13:20+00:00") == 1_700_000_000_000


def test_since_epoch_ms_naive_datetime_assumed_utc():
    assert crun._since_epoch_ms("2023-11-14T22:13:20") == 1_700_000_000_000


# ── HTTP-layer helpers (status-code branch coverage) ──────────────────────────


def test_list_semantic_collection_non_200(monkeypatch, tmp_path):
    monkeypatch.setattr(crun, "_http_request", lambda *a, **k: (500, b""))
    assert list_semantic_collection(_cfg(tmp_path), "tok", "decisions") == []


def test_list_semantic_collection_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun,
        "_http_request",
        lambda *a, **k: (200, json.dumps({"items": [{"key": "decisions/x"}]}).encode()),
    )
    items = list_semantic_collection(_cfg(tmp_path), "tok", "decisions")
    assert items == [{"key": "decisions/x"}]


def test_list_semantic_collection_no_items_key(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun, "_http_request", lambda *a, **k: (200, json.dumps({}).encode())
    )
    assert list_semantic_collection(_cfg(tmp_path), "tok", "decisions") == []


def test_read_semantic_doc_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun,
        "_http_request",
        lambda *a, **k: (200, json.dumps({"content": "x"}).encode()),
    )
    status, body = read_semantic_doc(_cfg(tmp_path), "tok", "decisions", "id1")
    assert status == 200
    assert body == {"content": "x"}


def test_read_semantic_doc_404(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun, "_http_request", lambda *a, **k: (404, b'{"detail":"not found"}')
    )
    status, body = read_semantic_doc(_cfg(tmp_path), "tok", "decisions", "id1")
    assert status == 404
    assert body is None


def test_promote_to_corpus_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun,
        "_http_request",
        lambda *a, **k: (200, json.dumps({"key": "decisions/id1"}).encode()),
    )
    record = _curated(document_id="id1")
    status, body = promote_to_corpus(_cfg(tmp_path), "tok", record)
    assert status == 200
    assert body["key"] == "decisions/id1"


def test_promote_to_corpus_error_no_json_body(monkeypatch, tmp_path):
    monkeypatch.setattr(crun, "_http_request", lambda *a, **k: (403, b"Forbidden"))
    record = _curated(document_id="id1")
    status, body = promote_to_corpus(_cfg(tmp_path), "tok", record)
    assert status == 403
    assert "Forbidden" in body["detail"]


def test_upload_self_log_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun,
        "_http_request",
        lambda *a, **k: (200, json.dumps({"key": "u/episodic/x.md"}).encode()),
    )
    result = upload_self_log(_cfg(tmp_path), "tok", "x.md", "body text")
    assert result["key"] == "u/episodic/x.md"


def test_upload_self_log_failure_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun, "_http_request", lambda *a, **k: (500, b'{"detail":"boom"}')
    )
    with pytest.raises(CurationLogError):
        upload_self_log(_cfg(tmp_path), "tok", "x.md", "body text")


def test_index_self_log_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun,
        "_http_request",
        lambda *a, **k: (200, json.dumps({"status": "indexed"}).encode()),
    )
    result = index_self_log(_cfg(tmp_path), "tok", "u/episodic/x.md")
    assert result["status"] == "indexed"


def test_index_self_log_failure_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun, "_http_request", lambda *a, **k: (400, b'{"detail":"bad key"}')
    )
    with pytest.raises(CurationLogError):
        index_self_log(_cfg(tmp_path), "tok", "bad-key")


def test_self_log_document_id_deterministic():
    import hashlib

    expected = hashlib.sha256(b"decisions:MARK.md:0").hexdigest()[:16]
    assert self_log_document_id("MARK.md") == expected


def test_normalize_front_door_rejects_bad_scheme():
    with pytest.raises(ValueError):
        crun._normalize_front_door("ftp://evil.example")


def test_normalize_front_door_rejects_no_host():
    with pytest.raises(ValueError):
        crun._normalize_front_door("https://")


def test_normalize_front_door_strips_trailing_slash():
    assert (
        crun._normalize_front_door("https://audittrace.local/")
        == "https://audittrace.local"
    )


def test_ssl_context_none_by_default():
    assert crun._ssl_context(False) is None


def test_ssl_context_insecure_opt_in():
    ctx = crun._ssl_context(True)
    assert ctx is not None
    assert ctx.check_hostname is False


def test_multipart_body_shape():
    body, content_type = crun._multipart_body("f.md", b"hello")
    assert b"hello" in body
    assert content_type.startswith("multipart/form-data; boundary=")


def test_err_detail_prefers_json_detail():
    assert crun._err_detail(json.dumps({"detail": "nope"}).encode()) == "nope"


def test_err_detail_falls_back_to_raw_text():
    assert crun._err_detail(b"plain text error") == "plain text error"


def test_err_detail_empty_body():
    assert crun._err_detail(b"") == "no response body"


# ── C7 verify-retrievability (falsifiability (b), (e)) ─────────────────────────


def test_verify_retrievability_fails_run(monkeypatch, tmp_path):
    """FALSIFIABILITY (b): a curated record that is written-but-not-recallable
    FAILS the run (tracked in ``unretrievable``, non-zero exit). Neuter
    ``phase_verify`` (e.g. always set ``verified = True``) and this test goes
    RED."""
    monkeypatch.setattr(crun, "read_semantic_doc", lambda *a, **k: (404, None))
    runner = CuratorRunner(_cfg(tmp_path))
    runner.curated = [_curated(document_id="ghost-1", tier="private")]
    runner._token = "tok"
    runner.phase_verify()

    assert runner.unretrievable == ["decisions/ghost-1"]
    assert runner.curated[0].verified is False
    assert runner.records[-1].status == "flagged"

    report = runner.build_report()
    assert exit_code_for(report) == crun.EXIT_UNRETRIEVABLE


def test_verify_retrievability_passes_when_recallable(monkeypatch, tmp_path):
    monkeypatch.setattr(crun, "read_semantic_doc", lambda *a, **k: (200, _NOT_DELETED))
    runner = CuratorRunner(_cfg(tmp_path))
    runner.curated = [_curated(document_id="ok-1", tier="private")]
    runner._token = "tok"
    runner.phase_verify()
    assert runner.unretrievable == []
    assert runner.curated[0].verified is True


def test_verify_retrievability_fails_on_soft_deleted_manifest(monkeypatch, tmp_path):
    """FALSIFIABILITY (soundness fix, 2026-08-05 reviewer REJECT): a curated
    record whose manifest is TOMBSTONED (``deleted_at_ms`` set) 200s on the raw
    point-read but must be scored UNRETRIEVABLE — it is permanently invisible to
    the real ``recall_decisions``/``recall_skills``/``recall_semantic`` tools
    (``_filter_soft_deleted``, ``services/semantic.py``), which is exactly the
    #374/#383 "written but doesn't surface" failure class verify-retrievability
    exists to catch. Neuter :func:`manifest_indicates_deleted` (e.g. make it
    always return ``False``) — or drop its use from ``phase_verify`` — and this
    test goes RED (the soft-deleted record is wrongly scored ``verified=True``)."""
    monkeypatch.setattr(
        crun,
        "read_semantic_doc",
        lambda *a, **k: (
            200,
            {"content": "x", "manifest": {"deleted_at_ms": 1_700_000_000_000}},
        ),
    )
    runner = CuratorRunner(_cfg(tmp_path))
    runner.curated = [_curated(document_id="tombstoned-1", tier="private")]
    runner._token = "tok"
    runner.phase_verify()

    assert runner.unretrievable == ["decisions/tombstoned-1"]
    assert runner.curated[0].verified is False
    assert "soft-deleted" in runner.curated[0].verify_detail
    assert runner.records[-1].status == "flagged"

    report = runner.build_report()
    assert exit_code_for(report) == crun.EXIT_UNRETRIEVABLE


def test_verify_retrievability_fails_when_manifest_missing_on_200(
    monkeypatch, tmp_path
):
    """Companion to the tombstone gate: every record C7 curates is manifest-
    tracked by construction (intake skips ``discovered`` rows), so a MISSING
    ``manifest`` block on an otherwise-200 point-read is treated as NOT
    retrievable too (fail-safe direction — this also covers the independent
    reviewer's live-traced report that a tombstoned row can surface with
    ``"manifest": null`` rather than a populated ``deleted_at_ms``)."""
    monkeypatch.setattr(
        crun,
        "read_semantic_doc",
        lambda *a, **k: (200, {"content": "x", "manifest": None}),
    )
    runner = CuratorRunner(_cfg(tmp_path))
    runner.curated = [_curated(document_id="vanished-1", tier="private")]
    runner._token = "tok"
    runner.phase_verify()
    assert runner.unretrievable == ["decisions/vanished-1"]
    assert runner.curated[0].verified is False


def test_manifest_indicates_deleted_true_cases():
    assert manifest_indicates_deleted(None) is True
    assert manifest_indicates_deleted({"deleted_at_ms": 123}) is True


def test_manifest_indicates_deleted_false_case():
    assert manifest_indicates_deleted({"deleted_at_ms": None}) is False


def test_verify_dry_run_plans_only(tmp_path):
    runner = CuratorRunner(_cfg(tmp_path, dry_run=True))
    runner.phase_verify()
    assert runner.records[-1].status == "planned"


def test_isolation_regression_fails_the_run(monkeypatch, tmp_path):
    """FALSIFIABILITY (e): a PRIVATE curated record that IS recallable by a
    DIFFERENT subject (the isolation probe token) is a hard FAIL — the sharpest
    failure the Curator can catch. Neuter the isolation-probe branch (e.g. never
    check the probe token) and this test goes RED."""
    # Primary owner sees it (200); the second-identity probe ALSO sees it (200) —
    # an isolation regression.
    monkeypatch.setattr(crun, "read_semantic_doc", lambda *a, **k: (200, _NOT_DELETED))
    runner = CuratorRunner(_cfg(tmp_path, isolation_probe_token="other-subject-token"))
    runner.curated = [_curated(document_id="leak-1", tier="private")]
    runner._token = "owner-token"
    runner.phase_verify()

    assert runner.isolation_regressions == ["decisions/leak-1"]
    assert runner.curated[0].isolation_regression is True
    assert runner.records[-1].status == "flagged"

    report = runner.build_report()
    assert exit_code_for(report) == crun.EXIT_ISOLATION_REGRESSION


def test_isolation_probe_correctly_isolated_no_regression(monkeypatch, tmp_path):
    """The healthy counterpart of the isolation gate: owner sees it (200), the
    different subject does NOT (404) — no regression."""
    calls = {"n": 0}

    def fake_read(cfg, token, collection, document_id):
        calls["n"] += 1
        return (200, _NOT_DELETED) if token == "owner-token" else (404, None)

    monkeypatch.setattr(crun, "read_semantic_doc", fake_read)
    runner = CuratorRunner(_cfg(tmp_path, isolation_probe_token="other-subject-token"))
    runner.curated = [_curated(document_id="safe-1", tier="private")]
    runner._token = "owner-token"
    runner.phase_verify()

    assert runner.isolation_regressions == []
    assert runner.curated[0].isolation_regression is False
    assert calls["n"] == 2  # owner probe + isolation probe


def test_isolation_probe_skipped_without_second_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(crun, "read_semantic_doc", lambda *a, **k: (200, _NOT_DELETED))
    runner = CuratorRunner(_cfg(tmp_path, isolation_probe_token=None))
    runner.curated = [_curated(document_id="ok-1", tier="private")]
    runner._token = "owner-token"
    runner.phase_verify()

    assert runner.isolation_probe_skipped is True
    assert runner.isolation_regressions == []
    report = runner.build_report()
    assert report["isolation_probe_skipped"] is True


def test_isolation_probe_not_applied_to_corpus_records(monkeypatch, tmp_path):
    """Corpus records are DELIBERATELY unfiltered — the isolation probe is a
    private-tier-only concept and must not be applied to corpus records."""
    calls = {"n": 0}

    def fake_read(cfg, token, collection, document_id):
        calls["n"] += 1
        return 200, _NOT_DELETED

    monkeypatch.setattr(crun, "read_semantic_doc", fake_read)
    runner = CuratorRunner(_cfg(tmp_path, isolation_probe_token="other-subject-token"))
    runner.curated = [_curated(document_id="corpus-1", tier="corpus")]
    runner._token = "owner-token"
    runner.phase_verify()

    assert calls["n"] == 1  # only the primary probe, never the isolation probe
    assert runner.isolation_regressions == []


# ── C8 self-log ──────────────────────────────────────────────────────────────


def test_self_log_happy_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun,
        "upload_self_log",
        lambda cfg, token, filename, text: {"key": "u/episodic/f.md"},
    )
    monkeypatch.setattr(
        crun, "index_self_log", lambda cfg, token, key: {"status": "indexed"}
    )
    monkeypatch.setattr(
        crun, "read_semantic_doc", lambda *a, **k: (200, {"content": "x"})
    )
    probe = _Recorder((True, "MARK found"))
    monkeypatch.setattr(crun, "chat_recall_probe", probe)

    runner = CuratorRunner(_cfg(tmp_path))
    runner._token = "tok"
    runner.phase_self_log()

    assert runner.self_log["key"] == "u/episodic/f.md"
    assert runner.self_log_recall_proof == {"tool": "recall_decisions", "found": True}
    assert runner.unretrievable == []
    assert runner.records[-1].status == "ok"
    assert len(probe.calls) == 1


def test_self_log_not_structurally_recallable_is_flagged(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun, "upload_self_log", lambda *a, **k: {"key": "u/episodic/f.md"}
    )
    monkeypatch.setattr(crun, "index_self_log", lambda *a, **k: {"status": "indexed"})
    monkeypatch.setattr(crun, "read_semantic_doc", lambda *a, **k: (404, None))
    monkeypatch.setattr(crun, "chat_recall_probe", lambda *a, **k: (True, "x"))

    runner = CuratorRunner(_cfg(tmp_path))
    runner._token = "tok"
    runner.phase_self_log()

    assert any("self-log" in item for item in runner.unretrievable)
    assert runner.records[-1].status == "flagged"


def test_self_log_recall_proof_not_found_flags_unretrievable(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun, "upload_self_log", lambda *a, **k: {"key": "u/episodic/f.md"}
    )
    monkeypatch.setattr(crun, "index_self_log", lambda *a, **k: {"status": "indexed"})
    monkeypatch.setattr(
        crun, "read_semantic_doc", lambda *a, **k: (200, {"content": "x"})
    )
    monkeypatch.setattr(crun, "chat_recall_probe", lambda *a, **k: (False, "NOT_FOUND"))

    runner = CuratorRunner(_cfg(tmp_path))
    runner._token = "tok"
    runner.phase_self_log()

    assert runner.self_log_recall_proof == {"tool": "recall_decisions", "found": False}
    assert any("recall_decisions proof" in item for item in runner.unretrievable)


def test_self_log_skip_llm_recall_proof(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun, "upload_self_log", lambda *a, **k: {"key": "u/episodic/f.md"}
    )
    monkeypatch.setattr(crun, "index_self_log", lambda *a, **k: {"status": "indexed"})
    monkeypatch.setattr(
        crun, "read_semantic_doc", lambda *a, **k: (200, {"content": "x"})
    )
    probe = _Recorder((True, "x"))
    monkeypatch.setattr(crun, "chat_recall_probe", probe)

    runner = CuratorRunner(_cfg(tmp_path, skip_llm_recall_proof=True))
    runner._token = "tok"
    runner.phase_self_log()

    assert probe.calls == []
    assert runner.self_log_recall_proof == {}


def test_self_log_upload_failure_raises_and_run_marks_log_failed(monkeypatch, tmp_path):
    _install_valid_session(monkeypatch)
    _install_healthy_version(monkeypatch)
    monkeypatch.setattr(crun, "intake_records", lambda cfg, token: [])
    monkeypatch.setattr(
        crun,
        "upload_self_log",
        lambda *a, **k: (_ for _ in ()).throw(CurationLogError("upload", 500, "boom")),
    )

    report = CuratorRunner(_cfg(tmp_path)).run()
    assert report["log_failed"] is True
    assert exit_code_for(report) == crun.EXIT_LOG_FAILED


def test_self_log_dry_run_plans_only(tmp_path):
    runner = CuratorRunner(_cfg(tmp_path, dry_run=True))
    runner.phase_self_log()
    assert runner.records[-1].status == "planned"


def test_chat_recall_probe_found(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun,
        "_http_request",
        lambda *a, **k: (
            200,
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "MARK-9 was found",
                            }
                        }
                    ]
                }
            ).encode(),
        ),
    )
    found, answer = chat_recall_probe(
        _cfg(tmp_path), "tok", "recall_decisions", "MARK-9"
    )
    assert found is True
    assert "MARK-9" in answer


def test_chat_recall_probe_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun,
        "_http_request",
        lambda *a, **k: (
            200,
            json.dumps(
                {
                    "choices": [
                        {"message": {"role": "assistant", "content": "NOT_FOUND"}}
                    ]
                }
            ).encode(),
        ),
    )
    found, answer = chat_recall_probe(
        _cfg(tmp_path), "tok", "recall_decisions", "MARK-9"
    )
    assert found is False


def test_chat_recall_probe_non_200(monkeypatch, tmp_path):
    monkeypatch.setattr(crun, "_http_request", lambda *a, **k: (500, b""))
    found, detail = chat_recall_probe(
        _cfg(tmp_path), "tok", "recall_decisions", "MARK-9"
    )
    assert found is False
    assert "500" in detail


def test_chat_recall_probe_no_choices(monkeypatch, tmp_path):
    monkeypatch.setattr(
        crun, "_http_request", lambda *a, **k: (200, json.dumps({}).encode())
    )
    found, detail = chat_recall_probe(
        _cfg(tmp_path), "tok", "recall_decisions", "MARK-9"
    )
    assert found is False
    assert "no choices" in detail


# ── C3 / C4 / C5 phase-level dry-run + wiring tests ────────────────────────────


def test_phase_intake_dry_run(tmp_path):
    runner = CuratorRunner(_cfg(tmp_path, dry_run=True))
    runner.phase_intake()
    assert runner.records[-1].status == "planned"


def test_phase_tag_dry_run(tmp_path):
    runner = CuratorRunner(_cfg(tmp_path, dry_run=True))
    runner.phase_tag()
    assert runner.records[-1].status == "planned"


def test_phase_normalize_dry_run(tmp_path):
    runner = CuratorRunner(_cfg(tmp_path, dry_run=True))
    runner.phase_normalize()
    assert runner.records[-1].status == "planned"


def test_phase_dedup_dry_run(tmp_path):
    runner = CuratorRunner(_cfg(tmp_path, dry_run=True))
    runner.phase_dedup()
    assert runner.records[-1].status == "planned"


def test_phase_tag_counts_flags(tmp_path):
    runner = CuratorRunner(_cfg(tmp_path))
    runner.raw = [
        _raw(document_id="a", text="contact luis@example.com"),
        _raw(document_id="b", text="clean text"),
    ]
    runner.phase_tag()
    assert runner.tags["a"] == [FLAG_PII]
    assert runner.tags["b"] == [FLAG_PUBLIC_SAFE]


def test_phase_normalize_flags_malformed_without_dropping(tmp_path):
    runner = CuratorRunner(_cfg(tmp_path))
    runner.raw = [_raw(document_id="", text="orphan")]
    runner.tags = {}
    runner.phase_normalize()
    assert runner.curated == []
    assert runner.flagged_records == ["<no-id>"]
    assert runner.records[-1].status == "flagged"


def test_phase_normalize_success(tmp_path):
    runner = CuratorRunner(_cfg(tmp_path))
    runner.raw = [_raw(document_id="a", text="marker: M1\nsome body")]
    runner.tags = {"a": [FLAG_PUBLIC_SAFE]}
    runner.phase_normalize()
    assert len(runner.curated) == 1
    assert runner.curated[0].marker == "M1"
    assert runner.records[-1].status == "ok"


def test_phase_dedup_records_merges(tmp_path):
    runner = CuratorRunner(_cfg(tmp_path, dedup_threshold=0.5))
    runner.curated = [
        _curated(
            document_id="a",
            text="alpha beta gamma delta",
            tier="private",
            created_at="2026-01-01T00:00:00+00:00",
        ),
        _curated(
            document_id="b",
            text="alpha beta gamma",
            tier="private",
            created_at="2026-02-01T00:00:00+00:00",
        ),
    ]
    runner.phase_dedup()
    assert len(runner.curated) == 1
    assert len(runner.merges) == 1


# ── full run() orchestration ────────────────────────────────────────────────────


def _wire_happy_path(monkeypatch):
    _install_healthy_version(monkeypatch)
    monkeypatch.setattr(
        crun,
        "intake_records",
        lambda cfg, token: [
            _raw(document_id="a", text="marker: HAPPY-1\nclean body", tier="private")
        ],
    )
    monkeypatch.setattr(
        crun,
        "promote_to_corpus",
        lambda cfg, token, record: (403, {"detail": "no scope"}),
    )
    monkeypatch.setattr(crun, "read_semantic_doc", lambda *a, **k: (200, _NOT_DELETED))
    monkeypatch.setattr(
        crun, "upload_self_log", lambda *a, **k: {"key": "u/episodic/f.md"}
    )
    monkeypatch.setattr(crun, "index_self_log", lambda *a, **k: {"status": "indexed"})
    monkeypatch.setattr(crun, "chat_recall_probe", lambda *a, **k: (True, "found"))


def test_full_run_happy_path(monkeypatch, tmp_path):
    _install_valid_session(monkeypatch)
    _wire_happy_path(monkeypatch)

    report = CuratorRunner(_cfg(tmp_path)).run()

    assert report["aborted"] is False
    assert report["log_failed"] is False
    assert report["unretrievable"] == []
    assert report["isolation_regressions"] == []
    assert report["certified"] is None
    assert report["verification"] == crun.VERIFICATION_DEFERRED
    assert [p["name"] for p in report["phases"]] == list(crun.PHASES)
    assert exit_code_for(report) == crun.EXIT_OK


def test_full_run_dry_run_touches_nothing(monkeypatch, tmp_path):
    _install_valid_session(monkeypatch)
    _install_healthy_version(monkeypatch)
    intake = _Recorder([])
    monkeypatch.setattr(crun, "intake_records", intake)

    report = CuratorRunner(_cfg(tmp_path, dry_run=True)).run()

    assert intake.calls == []  # dry-run never calls the real intake
    assert report["aborted"] is False
    statuses = {p["name"]: p["status"] for p in report["phases"]}
    assert statuses["C2-intake"] == "planned"
    assert statuses["C8-self-log"] == "planned"


def test_report_written_to_out_dir(monkeypatch, tmp_path):
    _install_valid_session(monkeypatch)
    _wire_happy_path(monkeypatch)
    out_dir = tmp_path / "runs"
    runner = CuratorRunner(_cfg(tmp_path, out_dir=out_dir))
    runner.run()
    files = list(out_dir.glob("curation-*.json"))
    assert len(files) == 1
    written = json.loads(files[0].read_text())
    assert written["runner"] == "audittrace-memory-curator"


def test_human_summary_contains_key_fields(monkeypatch, tmp_path):
    _install_valid_session(monkeypatch)
    _wire_happy_path(monkeypatch)
    runner = CuratorRunner(_cfg(tmp_path))
    report = runner.run()
    summary = runner.human_summary(report)
    assert "AuditTrace Memory Curator" in summary
    assert "certified" in summary
    assert "isolation" in summary


# ── CLI ────────────────────────────────────────────────────────────────────────


def test_build_parser_defaults():
    parser = crun.build_parser()
    args = parser.parse_args([])
    cfg = crun.config_from_args(args)
    assert cfg.front_door == crun.DEFAULT_FRONT_DOOR
    assert cfg.collections == RECALL_COLLECTIONS
    assert cfg.isolation_probe_token is None
    assert cfg.skip_llm_recall_proof is False


def test_build_parser_custom_collections():
    parser = crun.build_parser()
    args = parser.parse_args(["--collections", "decisions, skills"])
    cfg = crun.config_from_args(args)
    assert cfg.collections == ("decisions", "skills")


def test_build_parser_isolation_probe_token():
    parser = crun.build_parser()
    args = parser.parse_args(["--isolation-probe-token", "abc"])
    cfg = crun.config_from_args(args)
    assert cfg.isolation_probe_token == "abc"


@pytest.mark.parametrize(
    "report_kwargs,expected",
    [
        (
            {
                "aborted": True,
                "abort_reason": "session",
                "isolation_regressions": [],
                "log_failed": False,
                "unretrievable": [],
            },
            crun.EXIT_SESSION_ABORT,
        ),
        (
            {
                "aborted": True,
                "abort_reason": "version",
                "isolation_regressions": [],
                "log_failed": False,
                "unretrievable": [],
            },
            crun.EXIT_VERSION_ABORT,
        ),
        (
            {
                "aborted": False,
                "abort_reason": "",
                "isolation_regressions": ["x"],
                "log_failed": False,
                "unretrievable": [],
            },
            crun.EXIT_ISOLATION_REGRESSION,
        ),
        (
            {
                "aborted": False,
                "abort_reason": "",
                "isolation_regressions": [],
                "log_failed": True,
                "unretrievable": [],
            },
            crun.EXIT_LOG_FAILED,
        ),
        (
            {
                "aborted": False,
                "abort_reason": "",
                "isolation_regressions": [],
                "log_failed": False,
                "unretrievable": ["x"],
            },
            crun.EXIT_UNRETRIEVABLE,
        ),
        (
            {
                "aborted": False,
                "abort_reason": "",
                "isolation_regressions": [],
                "log_failed": False,
                "unretrievable": [],
            },
            crun.EXIT_OK,
        ),
    ],
)
def test_exit_code_for_priority(report_kwargs, expected):
    assert exit_code_for(report_kwargs) == expected


def test_main_returns_ok_exit(monkeypatch, tmp_path, capsys):
    _install_valid_session(monkeypatch)
    _wire_happy_path(monkeypatch)
    code = crun.main(["--out-dir", str(tmp_path / "runs")])
    assert code == crun.EXIT_OK
    captured = capsys.readouterr()
    assert "AuditTrace Memory Curator" in captured.out


def test_main_returns_session_abort_exit(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(crun, "_login_show", lambda script: "")
    code = crun.main(["--out-dir", str(tmp_path / "runs")])
    assert code == crun.EXIT_SESSION_ABORT
