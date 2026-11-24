"""VERIFY-PROBE11-FALSE-PASS: the gate-r2 conditions (ADDENDUM-2) on top of the
red module ``tests/test_verify_probe11.py``.

* **C1** every alias-edge negative asserts the EXACT match kind and
  ``alias is False``, parametrised over BOTH policy values, so the default
  policy (``fail``) cannot mask a classifier that wrongly says "alias".
* **C2** the verifier (``scripts/deploy/verify_alias.py``) and the deployer
  (``scripts/deploy/runner/alias.py``) judge independently: a parity test runs
  both over one case set and requires equal kinds; a structural test pins that
  neither module imports the other (nor the runner package for the verifier).
* **S2 / G7'** completeness of ``verify.FIRST_PARTY_WORKLOADS`` by posture, by
  prefix and by kind.

Every test names the neuter that turns it RED.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.deploy import registry, verify, verify_alias
from scripts.deploy.runner import alias as runner_alias
from tests.test_verify_probe11 import (
    AMD64,
    ATT,
    RC2_BODY,
    RC2_DIGEST,
    RC3_BODY,
    TAG,
    Registry,
    _cfg,
    _kube,
    child,
    dgst,
    index_body,
    probe11,
)

POLICIES = ("fail", "pass")
BFF_REPO = "localhost:5000/audittrace/librechat-bff"


@pytest.fixture(params=POLICIES)
def policy(request, monkeypatch):
    monkeypatch.setattr(registry, "ALIAS_POLICY", request.param)
    return request.param


def _bff_item(res):
    return res.evidence["items"]["librechat-bff"]


def _assert_negative(res, kind):
    item = _bff_item(res)
    assert res.status == verify.FAIL
    assert item["match"] == kind, item
    assert res.evidence["alias"] is False
    assert "ALIAS:" not in res.detail


# ── C1: the negative cases (live side carries the defect) ────────────────────


def _probe(tmp_path, monkeypatch, *, live_body, pub_body=RC3_BODY):
    reg = Registry(bff_tag_body=pub_body, extra={"live": live_body})
    return probe11(tmp_path, monkeypatch, reg, _kube(bff_live=dgst(live_body)))


def test_c1_differing_amd64_pair_is_mismatch(tmp_path, monkeypatch, policy):
    """Neuter: compare platform KEY sets only (ignore the child digests) -> alias -> RED."""
    live = index_body([child("sha256:" + "d" * 64), ATT])
    _assert_negative(_probe(tmp_path, monkeypatch, live_body=live), "mismatch")


def test_c1_empty_platform_sets_are_mismatch(tmp_path, monkeypatch, policy):
    """Neuter: drop the non-empty check ({} == {} -> alias) -> RED."""
    live = index_body([ATT])
    pub = index_body(
        [child("sha256:" + "c" * 64, "unknown", "unknown", attestation=True)]
    )
    _assert_negative(
        _probe(tmp_path, monkeypatch, live_body=live, pub_body=pub), "mismatch"
    )


@pytest.mark.parametrize("order", ["amd64-first", "amd64-last"])
@pytest.mark.parametrize("side", ["live", "published"])
def test_c1_duplicate_platform_is_unresolved_in_both_orders(
    tmp_path, monkeypatch, policy, order, side
):
    """Neuter first-wins -> alias in one order; last-wins -> mismatch in the other
    (a kind change the assertion catches); allow duplicates -> RED either way."""
    other = child("sha256:" + "e" * 64)
    kids = [child(AMD64), other] if order == "amd64-first" else [other, child(AMD64)]
    dup = index_body([*kids, ATT])
    if side == "live":
        res = _probe(tmp_path, monkeypatch, live_body=dup)
    else:
        reg = Registry(bff_tag_body=dup, extra={"live": RC2_BODY})
        res = probe11(tmp_path, monkeypatch, reg, _kube(bff_live=RC2_DIGEST))
    _assert_negative(res, "unresolved")


def test_c1_variant_is_part_of_the_platform_key(tmp_path, monkeypatch, policy):
    """live (v6=A, v7=B) vs published (v6=X, v7=B). Neuter: key on os/arch only -> alias -> RED."""
    a, b, x = ("sha256:" + ch * 64 for ch in "abx")
    live = index_body(
        [child(a, arch="arm", variant="v6"), child(b, arch="arm", variant="v7")]
    )
    pub = index_body(
        [child(x, arch="arm", variant="v6"), child(b, arch="arm", variant="v7")]
    )
    _assert_negative(
        _probe(tmp_path, monkeypatch, live_body=live, pub_body=pub), "mismatch"
    )


def test_c1_annotated_child_with_a_real_platform_is_compared(
    tmp_path, monkeypatch, policy
):
    """Isolates the filter: the SAME amd64 child, plus an attestation-ANNOTATED
    linux/arm64 child whose digest differs (no duplicate platform). Neuter: also
    exclude annotated children (the OR filter) -> alias -> RED."""
    live = index_body(
        [child(AMD64), child("sha256:" + "1" * 64, arch="arm64", attestation=True)]
    )
    pub = index_body(
        [child(AMD64), child("sha256:" + "2" * 64, arch="arm64", attestation=True)]
    )
    _assert_negative(
        _probe(tmp_path, monkeypatch, live_body=live, pub_body=pub), "mismatch"
    )


def test_c1_a_child_without_a_platform_is_unresolved_never_a_keyerror(
    tmp_path, monkeypatch, policy
):
    """Neuter: ``child["platform"]`` (KeyError) or skip the child -> RED."""
    bare = {"mediaType": "x", "digest": "sha256:" + "9" * 64, "size": 1}
    live = index_body([child(AMD64), bare])
    _assert_negative(_probe(tmp_path, monkeypatch, live_body=live), "unresolved")


def test_c1_a_body_that_does_not_hash_to_its_digest_is_unresolved(
    tmp_path, monkeypatch, policy
):
    """B2.6. Neuter: skip the sha256 check in ``fetch_by_digest`` -> alias -> RED."""
    reg = Registry(corrupt={RC2_DIGEST})
    res = probe11(tmp_path, monkeypatch, reg, _kube(bff_live=RC2_DIGEST))
    _assert_negative(res, "unresolved")


def test_c1_a_live_digest_the_registry_does_not_serve_is_unresolved(
    tmp_path, monkeypatch, policy
):
    res = probe11(
        tmp_path, monkeypatch, Registry(), _kube(bff_live="sha256:" + "7" * 64)
    )
    _assert_negative(res, "unresolved")


def test_c1_a_single_manifest_never_aliases_an_index(tmp_path, monkeypatch, policy):
    """No ``manifests`` key = a bare image manifest = {"manifest": digest}; two
    different digests never share a platform set. Neuter: treat it as an empty
    index and compare {} == {} -> alias -> RED."""
    single = json.dumps({"schemaVersion": 2, "layers": []}).encode()
    _assert_negative(_probe(tmp_path, monkeypatch, live_body=single), "mismatch")


@pytest.mark.parametrize(
    "body", [b"not json", b"[]", b'{"manifests": 5}', b'{"manifests": [3]}']
)
def test_c1_a_malformed_body_is_unresolved(tmp_path, monkeypatch, policy, body):
    _assert_negative(_probe(tmp_path, monkeypatch, live_body=body), "unresolved")


def test_c1_a_child_without_a_digest_is_unresolved(tmp_path, monkeypatch, policy):
    live = index_body([{"platform": {"os": "linux", "architecture": "amd64"}}])
    _assert_negative(_probe(tmp_path, monkeypatch, live_body=live), "unresolved")


def test_c1_never_pushed_tag_is_a_named_fail_with_no_alias(
    tmp_path, monkeypatch, policy
):
    """ref.digest is None (404 on the local tag). Neuter: ``if ref.digest and ...`` -> RED."""
    res = probe11(tmp_path, monkeypatch, Registry(bff_tag_body=None), _kube())
    assert res.status == verify.FAIL and "librechat-bff" in res.detail
    assert _bff_item(res)["match"] == "no-published-digest"
    assert res.evidence["alias"] is False


def test_c1_a_docker_manifest_list_aliases_like_an_oci_index(tmp_path, monkeypatch):
    """An index is detected by its ``manifests`` key, not its media type: a docker
    manifest-list body with the same children is still an alias. Neuter: detect by
    the OCI media type only -> {"manifest": d} on both sides -> mismatch -> RED."""
    monkeypatch.setattr(registry, "ALIAS_POLICY", "pass")
    body = json.loads(RC2_BODY)
    body["mediaType"] = "application/vnd.docker.distribution.manifest.list.v2+json"
    live = json.dumps(body).encode()
    res = _probe(tmp_path, monkeypatch, live_body=live)
    assert (
        res.status == verify.PASS and _bff_item(res)["match"] == "content-equal-alias"
    )


def test_c1_os_version_is_part_of_the_platform_key(tmp_path, monkeypatch, policy):
    """Windows images differ by ``os.version``. Neuter: drop it from the key -> RED."""

    live = index_body(
        [_win("10.0.1", "sha256:" + "a" * 64), _win("10.0.2", "sha256:" + "b" * 64)]
    )
    pub = index_body(
        [_win("10.0.1", "sha256:" + "b" * 64), _win("10.0.2", "sha256:" + "b" * 64)]
    )
    _assert_negative(
        _probe(tmp_path, monkeypatch, live_body=live, pub_body=pub), "mismatch"
    )


# ── the alias itself, under each policy, from the measured bodies ────────────


def test_alias_evidence_names_the_kind_and_the_build_revisions_na_without_attestation(
    tmp_path, monkeypatch, policy
):
    res = probe11(tmp_path, monkeypatch, Registry(), _kube(bff_live=RC2_DIGEST))
    item = _bff_item(res)
    assert item["match"] == "content-equal-alias" and res.evidence["alias"] is True
    assert res.status == (verify.FAIL if policy == "fail" else verify.PASS)
    assert "build revisions live=n/a published=n/a" in res.detail
    assert res.detail.endswith("(checked 2 of 2)")


# ── C2: two independent classifiers, one policy, equal outcomes ─────────────


def _win(version: str, digest: str) -> dict[str, Any]:
    c = child(digest, os_="windows")
    c["platform"]["os.version"] = version
    return c


def _cases() -> dict[str, tuple[bytes, bytes]]:
    """name -> (live index body, published index body); the measured rc2/rc3
    pair plus every C1 edge."""
    a, b, x = ("sha256:" + ch * 64 for ch in "abx")
    return {
        "rc2-rc3-alias": (RC2_BODY, RC3_BODY),
        "differing-amd64": (index_body([child("sha256:" + "d" * 64), ATT]), RC3_BODY),
        "empty-sets": (
            index_body([ATT]),
            index_body(
                [child("sha256:" + "c" * 64, "unknown", "unknown", attestation=True)]
            ),
        ),
        "duplicate-live-first": (
            index_body([child(AMD64), child("sha256:" + "e" * 64)]),
            RC3_BODY,
        ),
        "duplicate-live-last": (
            index_body([child("sha256:" + "e" * 64), child(AMD64)]),
            RC3_BODY,
        ),
        "duplicate-published": (
            RC2_BODY,
            index_body([child(AMD64), child("sha256:" + "e" * 64)]),
        ),
        "variant": (
            index_body(
                [child(a, arch="arm", variant="v6"), child(b, arch="arm", variant="v7")]
            ),
            index_body(
                [child(x, arch="arm", variant="v6"), child(b, arch="arm", variant="v7")]
            ),
        ),
        "annotated-arm64": (
            index_body(
                [
                    child(AMD64),
                    child("sha256:" + "1" * 64, arch="arm64", attestation=True),
                ]
            ),
            index_body(
                [
                    child(AMD64),
                    child("sha256:" + "2" * 64, arch="arm64", attestation=True),
                ]
            ),
        ),
        "no-platform": (
            index_body([child(AMD64), {"digest": "sha256:" + "9" * 64}]),
            RC3_BODY,
        ),
        "single-manifest": (json.dumps({"schemaVersion": 2}).encode(), RC3_BODY),
        "malformed": (b"[]", RC3_BODY),
        "not-json": (b"nope", RC3_BODY),
        "manifests-not-a-list": (b'{"manifests": 5}', RC3_BODY),
        "child-not-an-object": (b'{"manifests": [3]}', RC3_BODY),
        "child-no-digest": (
            index_body([{"platform": {"os": "linux", "architecture": "amd64"}}]),
            RC3_BODY,
        ),
        "os-version": (
            index_body([_win("10.0.1", a), _win("10.0.2", b)]),
            index_body([_win("10.0.1", b), _win("10.0.2", b)]),
        ),
        "exact": (RC3_BODY, RC3_BODY),
    }


@pytest.mark.parametrize("name", sorted(_cases()))
def test_c2_the_two_classifiers_agree_on_every_case(monkeypatch, name):
    """Parity (C2). Neuter: change ONE classifier (e.g. last-wins in the runner's
    ``_platforms``) -> its kind diverges on a duplicate case -> RED."""
    live_body, pub_body = _cases()[name]
    live, pub = dgst(live_body), dgst(pub_body)
    monkeypatch.setattr(
        registry, "_http_get", Registry(bff_tag_body=pub_body, extra={"l": live_body})
    )
    verifier = verify_alias.classify(live, pub, "local", repo_path="bff")
    deployer = runner_alias.classify(live, pub, BFF_REPO, "librechat-bff")
    assert deployer is not None
    assert verifier.kind == deployer.kind, name


def test_c2_the_parity_corpus_covers_every_kind(monkeypatch):
    """The corpus must exercise all four kinds, else the parity is vacuous.
    Neuter: drop the alias case (or every unresolved case) -> RED."""
    kinds = set()
    for live_body, pub_body in _cases().values():
        reg = Registry(bff_tag_body=pub_body, extra={"l": live_body})
        monkeypatch.setattr(registry, "_http_get", reg)
        kinds.add(
            verify_alias.classify(
                dgst(live_body), dgst(pub_body), "local", repo_path="bff"
            ).kind
        )
    assert kinds == {"exact", "content-equal-alias", "mismatch", "unresolved"}


def test_c2_each_classifier_has_its_own_judgment_no_shared_code():
    """Structural independence: ``verify_alias`` never imports the runner
    package, ``runner.alias`` never imports ``verify_alias``/``verify``; the only
    shared names are the registry module (reads + the one policy constant).
    Neuter: ``from scripts.deploy.runner import alias`` inside verify_alias -> RED."""

    def imports(path: str) -> set[str]:
        tree = ast.parse(Path(path).read_text())
        out: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                out |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                out |= {f"{node.module}.{a.name}" for a in node.names}
        return out

    verifier = imports(verify_alias.__file__)
    deployer = imports(runner_alias.__file__)
    assert not any(m.startswith("scripts.deploy.runner") for m in verifier), verifier
    assert not any("verify" in m for m in deployer), deployer
    assert "scripts.deploy.registry" in verifier or "scripts.deploy.registry" in {
        m.rsplit(".", 1)[0] for m in verifier
    }
    assert verify_alias.classify is not runner_alias.classify


def test_c2_the_policy_is_one_constant_default_fail_per_the_operator_ruling_q1():
    """AC8: ``registry.ALIAS_POLICY`` equals the operator's Q1 ruling
    (decisions-in/2026-11-24-probe11-q1-alias-policy-fail.md): 'fail'."""
    assert registry.ALIAS_POLICY == "fail"
    assert not hasattr(verify_alias, "ALIAS_POLICY")
    assert not hasattr(runner_alias, "ALIAS_POLICY")


# ── probe 2 shares the policy and the diagnosis ─────────────────────────────


def test_probe2_a_memory_server_mismatch_is_not_an_alias(tmp_path, monkeypatch, policy):
    """Neuter: probe 2 calls an alias on a differing platform set -> RED."""
    other = index_body([child("sha256:" + "d" * 64), ATT])
    reg = Registry(extra={"other": other})
    monkeypatch.setattr(registry, "_http_get", reg)
    monkeypatch.setattr(verify, "_run", _kube(ms_live=dgst(other)))
    res = verify.VerifyRunner(_cfg(tmp_path)).probe_digest_matches_published()
    assert res.status == verify.FAIL and res.evidence["alias"] is False
    assert res.evidence["match"] == "mismatch" and "ALIAS:" not in res.detail


def test_probe2_alias_under_the_pass_policy_passes_with_the_diagnosis(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(registry, "ALIAS_POLICY", "pass")
    other = index_body(
        [
            child("sha256:" + "b" * 64),
            child("sha256:" + "c" * 64, "unknown", "unknown", attestation=True),
        ]
    )
    monkeypatch.setattr(registry, "_http_get", Registry(extra={"other": other}))
    monkeypatch.setattr(verify, "_run", _kube(ms_live=dgst(other)))
    res = verify.VerifyRunner(_cfg(tmp_path)).probe_digest_matches_published()
    assert res.status == verify.PASS and res.evidence["alias"] is True
    assert res.detail.startswith("ALIAS: memory-server")


# ── probe 11 posture edges ───────────────────────────────────────────────────


def test_probe11_an_unreadable_console_anchor_fails_closed(tmp_path, monkeypatch):
    """The anchor read fails (not NotFound): the BFF cannot be ruled out, so it
    is expected AND the unreadable anchor is named. Neuter: treat None as
    "console absent" -> the probe passes 1 of 1 -> RED."""
    inner = _kube(present=("reaper",))

    def run(cmd):
        if (
            "deployment" in " ".join(cmd)
            and "audittrace-librechat" in " ".join(cmd)
            and "-bff" not in " ".join(cmd)
        ):
            return subprocess.CompletedProcess(["x"], 1, "", "connection refused")
        return inner(cmd)

    res = probe11(tmp_path, monkeypatch, Registry(), run)
    assert res.status == verify.FAIL
    assert "could not determine whether the console is deployed" in res.detail
    assert res.detail.endswith("(checked 1 of 2)")


def test_probe11_a_bff_without_its_console_anchor_is_still_checked(
    tmp_path, monkeypatch
):
    """The BFF Deployment exists but ``audittrace-librechat`` does not: the BFF
    is still compared (checked 2 of 2), never silently ignored. Neuter: gate the
    BFF on the anchor alone -> it is skipped -> checked 1 of 1 -> RED."""
    res = probe11(tmp_path, monkeypatch, Registry(), _kube(present=("bff", "reaper")))
    assert res.status == verify.PASS and res.detail.endswith("(checked 2 of 2)")


def test_probe11_checked_counts_only_completed_comparisons(tmp_path, monkeypatch):
    """A resolve failure compares nothing: the count must say so."""
    res = probe11(tmp_path, monkeypatch, Registry(bff_tag_body=None), _kube())
    assert res.evidence["checked"] == 1 and res.evidence["expected"] == 2


def test_probe11_unresolvable_registry_names_the_item(tmp_path, monkeypatch):
    def boom(v, reg, *, repo_path="memory-server"):
        raise registry.DigestResolutionError("HTTP 503")

    monkeypatch.setattr(registry, "resolve", boom)
    monkeypatch.setattr(verify, "_run", _kube())
    res = verify.VerifyRunner(
        verify.VerifyConfig(
            out_dir=Path("/nonexistent"), target_version=TAG, registry="local"
        )
    ).probe_first_party_images_match_published()
    assert res.status == verify.FAIL
    assert "pod-reaper: could not resolve" in res.detail
    assert res.detail.endswith("(checked 0 of 2)")


# ── S2 / G7': completeness by kind, by prefix, by posture ───────────────────

_RENDER_KEYS = (
    "externalLLM.host",
    "observability.external.langfuseHost",
    "observability.external.lokiHost",
    "observability.external.tempoHost",
    "secrets.chromadb.token",
    "secrets.keycloak.adminPassword",
    "secrets.minio.kmsKey",
    "secrets.minio.secretKey",
    "secrets.postgres.appPassword",
    "secrets.postgres.password",
    "secrets.redis.password",
    "secrets.summariser.password",
)
_KINDS = (
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "ReplicaSet",
    "Job",
    "CronJob",
    "Pod",
)
FIRST_PARTY_PREFIXES = ("docker.io/lfds/", "localhost:5000/audittrace/")
# Rendered third-party REBUILDS that carry "audittrace" in the name (measured on
# the gate: five frozen images). Anything else with "audittrace" in its image
# name that is not first-party must be added here deliberately.
MIRROR_ALLOWLIST = ("ghcr.io/lfdesousa/audittrace-",)
# Named exclusions (workload -> why it is not a probe-11 item).
EXCLUSIONS = {
    ("Deployment", "audittrace-memory-server"): "probe 2's item",
    (
        "Deployment",
        "audittrace-librechat",
    ): "the fork's own pin, not versioned by --target-version",
    ("Pod", "audittrace-test-rls"): "a test Pod, render-level only",
    (
        "Deployment",
        "audittrace-llm-stub",
    ): "kind-only (llmStub.enabled), never in production",
}


def _pod_spec(doc: dict[str, Any]) -> dict[str, Any] | None:
    spec = doc.get("spec") or {}
    kind = doc.get("kind")
    if kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"):
        return (spec.get("template") or {}).get("spec")
    if kind == "CronJob":
        return (
            ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}
        ).get("spec")
    return spec if kind == "Pod" else None


def _prefixes(monkeypatch_env: str | None) -> tuple[str, ...]:
    extra = tuple(p.strip() for p in (monkeypatch_env or "").split(",") if p.strip())
    return FIRST_PARTY_PREFIXES + extra


def _scan(
    rendered: str, prefixes: tuple[str, ...]
) -> tuple[set[tuple[str, str]], set[str]]:
    """(first-party workloads, images naming 'audittrace' that are neither
    first-party nor on the mirror allowlist). Every kind, every container, init
    included."""
    workloads: set[tuple[str, str]] = set()
    unclassified: set[str] = set()
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") not in _KINDS:
            continue
        pod = _pod_spec(doc)
        for container in ((pod or {}).get("initContainers") or []) + (
            (pod or {}).get("containers") or []
        ):
            image = str(container.get("image", ""))
            if image.startswith(prefixes):
                workloads.add((doc["kind"], doc["metadata"]["name"]))
            elif "audittrace" in image and not image.startswith(MIRROR_ALLOWLIST):
                unclassified.add(image)
    return workloads, unclassified


def _render(*extra: str) -> str:
    chart = Path(__file__).resolve().parents[1] / "charts" / "audittrace"
    cmd = ["helm", "template", "audittrace", str(chart), *extra]
    for key in _RENDER_KEYS:
        cmd += ["--set", f"{key}=placeholder"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not on PATH")
def test_g7_every_rendered_first_party_workload_is_a_table_row_or_a_named_exclusion(
    monkeypatch,
):
    """Two cells (console+vault; kind-only llmStub). Every kind incl. ReplicaSet,
    every container incl. init. Honours AUDITTRACE_FIRST_PARTY_IMAGE_PREFIXES.
    Neuters (executed below): a CronJob / ReplicaSet / init container on a
    first-party image, an unknown 'audittrace' image, an extra env prefix."""
    prefixes = _prefixes(os.environ.get("AUDITTRACE_FIRST_PARTY_IMAGE_PREFIXES"))
    base_workloads, base_unclassified = _scan(
        _render("--set", "console.enabled=true", "--set", "vault.enabled=true"),
        prefixes,
    )
    stub_workloads, stub_unclassified = _scan(
        _render("--set", "llmStub.enabled=true", "--set", "externalLLM.enabled=false"),
        prefixes,
    )
    table = {("Deployment", w.deployment) for w in verify.FIRST_PARTY_WORKLOADS}
    assert base_workloads | stub_workloads == table | set(EXCLUSIONS)
    assert base_unclassified | stub_unclassified == set()


@pytest.mark.parametrize(
    "doc,expected",
    [
        (
            {
                "kind": "CronJob",
                "metadata": {"name": "audittrace-x"},
                "spec": {
                    "jobTemplate": {
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [
                                        {
                                            "name": "c",
                                            "image": "docker.io/lfds/audittrace-x:1",
                                        }
                                    ]
                                }
                            }
                        }
                    }
                },
            },
            ("CronJob", "audittrace-x"),
        ),
        (
            {
                "kind": "ReplicaSet",
                "metadata": {"name": "audittrace-rs"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {"name": "c", "image": "docker.io/lfds/audittrace-x:1"}
                            ]
                        }
                    }
                },
            },
            ("ReplicaSet", "audittrace-rs"),
        ),
        (
            {
                "kind": "Deployment",
                "metadata": {"name": "audittrace-init"},
                "spec": {
                    "template": {
                        "spec": {
                            "initContainers": [
                                {"name": "i", "image": "localhost:5000/audittrace/x:1"}
                            ],
                            "containers": [],
                        }
                    }
                },
            },
            ("Deployment", "audittrace-init"),
        ),
        (
            {
                "kind": "Pod",
                "metadata": {"name": "audittrace-p"},
                "spec": {
                    "containers": [
                        {"name": "c", "image": "docker.io/lfds/audittrace-x:1"}
                    ]
                },
            },
            ("Pod", "audittrace-p"),
        ),
    ],
)
def test_g7_neuter_a_new_first_party_workload_of_any_kind_is_caught(doc, expected):
    """The neuter, executed: each shape appears in the difference set, so the
    equality in the render test would be RED."""
    found, _ = _scan(yaml.safe_dump(doc), FIRST_PARTY_PREFIXES)
    table = {("Deployment", w.deployment) for w in verify.FIRST_PARTY_WORKLOADS}
    assert found - (table | set(EXCLUSIONS)) == {expected}


def test_g7_neuter_an_unclassified_audittrace_image_is_caught():
    """By PREFIX: an image naming 'audittrace' outside the first-party prefixes
    and the mirror allowlist must be reported (a future first-party image pushed
    to another registry cannot escape the table)."""
    doc = {
        "kind": "Deployment",
        "metadata": {"name": "d"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": "a", "image": "quay.io/acme/audittrace-new:1"},
                        {"name": "b", "image": "ghcr.io/lfdesousa/audittrace-frozen:1"},
                        {"name": "c", "image": "docker.io/library/nginx:1"},
                    ]
                }
            }
        },
    }
    found, unclassified = _scan(yaml.safe_dump(doc), FIRST_PARTY_PREFIXES)
    assert found == set() and unclassified == {"quay.io/acme/audittrace-new:1"}


def test_g7_the_env_var_prefix_override_is_honoured():
    """AUDITTRACE_FIRST_PARTY_IMAGE_PREFIXES extends the prefix set additively."""
    doc = {
        "kind": "Deployment",
        "metadata": {"name": "d"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": "a", "image": "quay.io/acme/audittrace-new:1"}
                    ]
                }
            }
        },
    }
    found, unclassified = _scan(yaml.safe_dump(doc), _prefixes("quay.io/acme/ , "))
    assert found == {("Deployment", "d")} and unclassified == set()
    assert _prefixes(None) == FIRST_PARTY_PREFIXES


def test_g7_the_probe_prefixes_equal_the_runners_default_prefixes():
    """The test derives its prefixes itself (verify.py never imports the runner);
    this pins them to the runner's committed default so they cannot drift."""
    from scripts.deploy.runner.images import FIRST_PARTY_PREFIXES as RUNNER

    assert FIRST_PARTY_PREFIXES == tuple(RUNNER)
