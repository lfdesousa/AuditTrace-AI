"""RED tests v2 for VERIFY-PROBE11-FALSE-PASS ADDENDUM-1 (closes gate r1 B1-B4, S1).

Every test is expected RED on origin/main d35f8261 and GREEN after the build.
Registry traffic is stubbed at ``registry._http_get`` (the module's sole egress)
with the MEASURED rc2/rc3 BFF index bodies (fixtures, sha256(body) == digest) and
synthetic variants; kubectl is stubbed at ``verify._run``. No cluster, no network.
Fixture location after the build: ``tests/fixtures/registry/bff-index-rc{2,3}.json``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.deploy import registry, verify
from scripts.deploy.verify import FAIL, PASS, VerifyConfig, VerifyRunner

FIX = Path(__file__).resolve().parent / "fixtures" / "registry"
RC2_BODY = (FIX / "bff-index-rc2.json").read_bytes()
RC3_BODY = (FIX / "bff-index-rc3.json").read_bytes()
INDEX_MT = "application/vnd.oci.image.index.v1+json"
MANIFEST_MT = "application/vnd.oci.image.manifest.v1+json"
BFF_PATH = "/v2/audittrace/librechat-bff/manifests/"
MS_PATH = "/v2/audittrace/memory-server/manifests/"
TAG = "1.34.0-rc3-cc47cc7"
NOT_FOUND = 'Error from server (NotFound): deployments.apps "x" not found'


def dgst(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


RC2_DIGEST, RC3_DIGEST = dgst(RC2_BODY), dgst(RC3_BODY)
AMD64 = json.loads(RC3_BODY)["manifests"][0]["digest"]


def index_body(children: list[dict]) -> bytes:
    return json.dumps(
        {"schemaVersion": 2, "mediaType": INDEX_MT, "manifests": children}, indent=2
    ).encode()


def child(
    digest: str,
    os_: str = "linux",
    arch: str = "amd64",
    variant: str | None = None,
    attestation: bool = False,
) -> dict:
    c = {
        "mediaType": MANIFEST_MT,
        "digest": digest,
        "size": 1,
        "platform": {"architecture": arch, "os": os_},
    }
    if variant:
        c["platform"]["variant"] = variant
    if attestation:
        c["annotations"] = {
            "vnd.docker.reference.type": "attestation-manifest",
            "vnd.docker.reference.digest": AMD64,
        }
    return c


ATT = child("sha256:" + "a" * 64, "unknown", "unknown", attestation=True)
MS_BODY = index_body([child("sha256:" + "b" * 64), ATT])
MS_DIGEST = dgst(MS_BODY)


class Registry:
    """``registry._http_get`` stub: tag -> (digest, body); by-digest -> body; else 404.
    ``served_tag_gets`` counts tag fetches per repo path (R15)."""

    def __init__(
        self,
        bff_tag_body: bytes | None = RC3_BODY,
        ms_tag_body: bytes | None = MS_BODY,
        extra: dict[str, bytes] | None = None,
        corrupt: set[str] | None = None,
    ):
        self.by_digest: dict[str, bytes] = {}
        self.tags: dict[str, bytes] = {}
        for path, body in ((BFF_PATH, bff_tag_body), (MS_PATH, ms_tag_body)):
            if body is not None:
                self.tags[path + TAG] = body
                self.by_digest[dgst(body)] = body
        for body in (RC2_BODY, RC3_BODY, *(extra or {}).values()):
            self.by_digest[dgst(body)] = body
        self.corrupt = corrupt or set()
        self.served_tag_gets: dict[str, int] = {}
        self.urls: list[str] = []

    def __call__(self, url: str, headers=None):
        self.urls.append(url)
        path = url.split("localhost:5000", 1)[-1]
        if path in self.tags:
            self.served_tag_gets[path] = self.served_tag_gets.get(path, 0) + 1
            body = self.tags[path]
            return (
                200,
                {"docker-content-digest": dgst(body), "content-type": INDEX_MT},
                body,
            )
        ref = path.rsplit("/", 1)[-1]
        if ref in self.by_digest:
            body = self.by_digest[ref]
            if ref in self.corrupt:
                body = body + b"\n"  # sha256(body) != requested digest
            return 200, {"docker-content-digest": ref, "content-type": INDEX_MT}, body
        return 404, {}, b""


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["x"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _cfg(tmp_path, **kw):
    kw.setdefault("out_dir", tmp_path / "runs")
    kw.setdefault("target_version", TAG)
    kw.setdefault("registry", "local")
    kw.setdefault("front_door", "https://audittrace.local:30952")
    return VerifyConfig(**kw)


def _kube(
    bff_live=RC3_DIGEST,
    reaper_live=MS_DIGEST,
    ms_live=MS_DIGEST,
    present=("bff", "librechat", "reaper"),
):
    def _run(cmd):
        joined = " ".join(str(c) for c in cmd)
        if "deployment" in joined:
            if "audittrace-librechat-bff" in joined:
                return (
                    _proc(0, "{}") if "bff" in present else _proc(1, stderr=NOT_FOUND)
                )
            if "audittrace-librechat" in joined:
                return (
                    _proc(0, "{}")
                    if "librechat" in present
                    else _proc(1, stderr=NOT_FOUND)
                )
            if "pod-reaper" in joined:
                return (
                    _proc(0, "{}")
                    if "reaper" in present
                    else _proc(1, stderr=NOT_FOUND)
                )
            return _proc(0, "{}")
        if "imageID" in joined:
            if "librechat-bff" in joined:
                return _proc(0, f"repo@{bff_live}" if bff_live else "")
            if "pod-reaper" in joined:
                return _proc(0, f"repo@{reaper_live}" if reaper_live else "")
            return _proc(0, f"repo@{ms_live}" if ms_live else "")
        return _proc(0, "{}")

    return _run


def probe11(tmp_path, monkeypatch, reg: Registry, kube, **cfg):
    monkeypatch.setattr(registry, "_http_get", reg)
    monkeypatch.setattr(verify, "_run", kube)
    return VerifyRunner(
        _cfg(tmp_path, **cfg)
    ).probe_first_party_images_match_published()


# ── B1: D6 — expected is derived, zero or fewer-than-expected = FAIL ─────────


def test_b1_local_compares_both_items_exact(tmp_path, monkeypatch):
    """G1 + G5. Neuter: restore the ``registry != "hub"`` early PASS → RED."""
    res = probe11(tmp_path, monkeypatch, Registry(), _kube())
    assert res.status == PASS
    assert res.evidence["checked"] == 2 and res.evidence["expected"] == 2
    assert res.detail.endswith("(checked 2 of 2)")
    assert res.evidence["items"]["pod-reaper"]["match"] == "exact"
    assert (
        res.evidence["items"]["librechat-bff"]["repository"]
        == "localhost:5000/audittrace/librechat-bff"
    )  # N4
    assert res.evidence["alias"] is False  # N5


def test_b1_zero_items_present_is_fail_reaper_always_expected(tmp_path, monkeypatch):
    """D6: the reaper is always expected (reaper.enabled=true, no overlay). Neuter: SKIPPED or PASS on zero → RED."""
    res = probe11(tmp_path, monkeypatch, Registry(), _kube(present=()))
    assert res.status == FAIL
    assert "pod-reaper" in res.detail and res.detail.endswith("(checked 0 of 1)")


def test_b1_bff_expected_when_librechat_present_but_absent_is_fail(
    tmp_path, monkeypatch
):
    """Expected derived from a signal other than the item's own presence. Neuter: expected := present → RED."""
    res = probe11(
        tmp_path, monkeypatch, Registry(), _kube(present=("librechat", "reaper"))
    )
    assert res.status == FAIL
    assert "librechat-bff" in res.detail and res.detail.endswith("(checked 1 of 2)")


def test_b1_no_console_posture_checks_reaper_only(tmp_path, monkeypatch):
    """Contrast: console off (no librechat, no bff) → BFF not expected → PASS 1 of 1."""
    res = probe11(tmp_path, monkeypatch, Registry(), _kube(present=("reaper",)))
    assert res.status == PASS and res.detail.endswith("(checked 1 of 1)")


def test_b1_stale_pod_reaper_fails(tmp_path, monkeypatch):
    res = probe11(
        tmp_path, monkeypatch, Registry(), _kube(reaper_live="sha256:" + "f" * 64)
    )
    assert res.status == FAIL and "pod-reaper" in res.detail


# ── B2 + B3: the alias path at the HTTP level, under ONE policy ───────────────


def _alias_assertions(res):
    item = res.evidence["items"]["librechat-bff"]
    assert item["match"] == "content-equal-alias"
    assert res.evidence["alias"] is True
    assert res.detail.startswith("ALIAS:") or "ALIAS:" in res.detail
    assert RC2_DIGEST[7:19] in res.detail and RC3_DIGEST[7:19] in res.detail
    assert "remedy" in res.detail.lower()
    return item


@pytest.mark.parametrize("policy,expected", [("fail", FAIL), ("pass", PASS)])
def test_b3_alias_measured_rc2_rc3_bodies_under_each_policy(
    tmp_path, monkeypatch, policy, expected
):
    """D3/D4 with the measured bodies: live = rc2 index, published = rc3 index, shared amd64.
    Neuter: compare index digests only → no ALIAS marker → RED under 'pass'; drop the policy switch → RED under one of the two."""
    monkeypatch.setattr(registry, "ALIAS_POLICY", policy)
    res = probe11(tmp_path, monkeypatch, Registry(), _kube(bff_live=RC2_DIGEST))
    item = _alias_assertions(res)
    assert res.status == expected
    assert item["platforms"] == {"linux/amd64": AMD64}


def test_b3_alias_policy_default_is_fail_until_operator_rules_q1():
    """Orchestrator default (Q1 open): FAIL + ALIAS diagnosis. Flips to 'pass' only on the operator's ruling."""
    assert registry.ALIAS_POLICY == "fail"


def test_b3_probe2_memory_server_alias_follows_the_same_policy(tmp_path, monkeypatch):
    """v1.33.2-rc2 shape: memory-server live = another index with the same platform manifests. Neuter: probe 2 keeps raw inequality → no ALIAS → RED."""
    other = index_body(
        [
            child("sha256:" + "b" * 64),
            child("sha256:" + "c" * 64, "unknown", "unknown", attestation=True),
        ]
    )
    reg = Registry(extra={"other": other})
    monkeypatch.setattr(registry, "_http_get", reg)
    monkeypatch.setattr(verify, "_run", _kube(ms_live=dgst(other)))
    monkeypatch.setattr(registry, "ALIAS_POLICY", "fail")
    res = VerifyRunner(_cfg(tmp_path)).probe_digest_matches_published()
    assert (
        res.status == FAIL and "ALIAS:" in res.detail and res.evidence["alias"] is True
    )


def test_b2_differing_amd64_pair_fails(tmp_path, monkeypatch):
    """B2.1: platform keys carry the child DIGESTS. Neuter: compare key sets only → RED."""
    other = index_body([child("sha256:" + "d" * 64), ATT])
    res = probe11(
        tmp_path, monkeypatch, Registry(extra={"o": other}), _kube(bff_live=dgst(other))
    )
    assert (
        res.status == FAIL
        and res.evidence["items"]["librechat-bff"]["match"] == "mismatch"
    )


def test_b2_empty_platform_sets_fail(tmp_path, monkeypatch):
    """B2.2: attestation-only indexes on both sides → {} == {} must not be alias. Neuter: drop the non-empty check → RED."""
    live = index_body([ATT])
    pub = index_body(
        [child("sha256:" + "c" * 64, "unknown", "unknown", attestation=True)]
    )
    res = probe11(
        tmp_path,
        monkeypatch,
        Registry(bff_tag_body=pub, extra={"l": live}),
        _kube(bff_live=dgst(live)),
    )
    assert res.status == FAIL


def test_b2_never_pushed_tag_404_is_fail_naming_the_item(tmp_path, monkeypatch):
    """B2.3 + S1: local soft-fail digest=None (404 for a tag not in localhost:5000) → FAIL. Neuter: ``if ref.digest and live != ref.digest`` → RED."""
    res = probe11(tmp_path, monkeypatch, Registry(bff_tag_body=None), _kube())
    assert res.status == FAIL and "librechat-bff" in res.detail


def test_b2_duplicate_platform_entries_fail(tmp_path, monkeypatch):
    """B2.4: two linux/amd64 children in one index → FAIL (containerd may run either). Neuter: last-wins dict → RED."""
    dup = index_body([child(AMD64), child("sha256:" + "e" * 64), ATT])
    res = probe11(
        tmp_path, monkeypatch, Registry(extra={"d": dup}), _kube(bff_live=dgst(dup))
    )
    assert res.status == FAIL


def test_b2_variant_is_part_of_the_platform_key(tmp_path, monkeypatch):
    """B2.4: live (v6=A, v7=B) vs published (v6=X, v7=B): an os/arch-only key collapses to B==B. Neuter: drop variant → RED."""
    a, b, x = ("sha256:" + ch * 64 for ch in "abx")
    live = index_body(
        [child(a, arch="arm", variant="v6"), child(b, arch="arm", variant="v7")]
    )
    pub = index_body(
        [child(x, arch="arm", variant="v6"), child(b, arch="arm", variant="v7")]
    )
    res = probe11(
        tmp_path,
        monkeypatch,
        Registry(bff_tag_body=pub, extra={"l": live}),
        _kube(bff_live=dgst(live)),
    )
    assert res.status == FAIL


def test_b2_annotated_child_with_real_platform_is_compared(tmp_path, monkeypatch):
    """B2.5: exclude only unknown/unknown. An attestation-annotated child on linux/amd64 differs → FAIL. Neuter: OR-filter → RED."""
    live = index_body([child(AMD64), child("sha256:" + "1" * 64, attestation=True)])
    pub = index_body([child(AMD64), child("sha256:" + "2" * 64, attestation=True)])
    res = probe11(
        tmp_path,
        monkeypatch,
        Registry(bff_tag_body=pub, extra={"l": live}),
        _kube(bff_live=dgst(live)),
    )
    assert res.status == FAIL


def test_b2_unknown_platform_without_annotation_is_excluded(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "ALIAS_POLICY", "pass")
    live = index_body([child(AMD64), child("sha256:" + "1" * 64, "unknown", "unknown")])
    pub = index_body([child(AMD64), child("sha256:" + "2" * 64, "unknown", "unknown")])
    res = probe11(
        tmp_path,
        monkeypatch,
        Registry(bff_tag_body=pub, extra={"l": live}),
        _kube(bff_live=dgst(live)),
    )
    assert (
        res.status == PASS
        and res.evidence["items"]["librechat-bff"]["match"] == "content-equal-alias"
    )


def test_b2_by_digest_body_sha_mismatch_fails(tmp_path, monkeypatch):
    """B2.6: sha256(body) must equal the requested digest on every by-digest fetch. Neuter: skip the hash → alias PASS/ALIAS → RED."""
    monkeypatch.setattr(registry, "ALIAS_POLICY", "pass")
    res = probe11(
        tmp_path,
        monkeypatch,
        Registry(corrupt={RC2_DIGEST}),
        _kube(bff_live=RC2_DIGEST),
    )
    assert res.status == FAIL


def test_b2_published_index_is_fetched_by_digest_not_by_tag_again(
    tmp_path, monkeypatch
):
    """B2.6: one tag read per repo (resolve); the alias path re-fetches by digest. Neuter: fetch the tag again → 2 → RED."""
    reg = Registry()
    probe11(tmp_path, monkeypatch, reg, _kube(bff_live=RC2_DIGEST))
    assert reg.served_tag_gets.get(BFF_PATH + TAG) == 1
    assert any(u.endswith(BFF_PATH + RC3_DIGEST) for u in reg.urls)


# ── B4: G7 completeness by render, all kinds, all containers, init included ────

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
_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod")
_EXCLUSIONS = {
    ("Deployment", "audittrace-memory-server"),
    ("Deployment", "audittrace-librechat"),
    ("Pod", "audittrace-test-rls"),
}


def _pod_spec(doc):
    spec = doc.get("spec") or {}
    kind = doc.get("kind")
    if kind in ("Deployment", "StatefulSet", "DaemonSet", "Job"):
        return (spec.get("template") or {}).get("spec")
    if kind == "CronJob":
        return (
            ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}
        ).get("spec")
    if kind == "Pod":
        return spec
    return None


def _first_party_workloads(
    rendered: str, prefixes: tuple[str, ...]
) -> set[tuple[str, str]]:
    found = set()
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") not in _KINDS:
            continue
        pod = _pod_spec(doc)
        if not pod:
            continue
        for key in ("initContainers", "containers"):
            for c in pod.get(key) or []:
                if str(c.get("image", "")).startswith(prefixes):
                    found.add((doc["kind"], doc["metadata"]["name"]))
    return found


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not on PATH")
def test_b4_g7_first_party_workloads_table_equals_render_plus_exclusions(tmp_path):
    """Render console+vault on; every kind, every container incl. init, by first-party prefix (derived here,
    not imported from the runner). Neuter: a CronJob on docker.io/lfds/audittrace-x in the render → RED."""
    chart = Path(__file__).resolve().parents[1] / "charts" / "audittrace"
    cmd = [
        "helm",
        "template",
        "audittrace",
        str(chart),
        "--set",
        "console.enabled=true",
        "--set",
        "vault.enabled=true",
    ]
    for k in _RENDER_KEYS:
        cmd += ["--set", f"{k}=placeholder"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    found = _first_party_workloads(
        out.stdout, ("docker.io/lfds/", "localhost:5000/audittrace/")
    )
    table = {("Deployment", row[0]) for row in verify.FIRST_PARTY_WORKLOADS}
    assert found == table | _EXCLUSIONS


def test_b4_g7_neuter_cronjob_on_first_party_image_is_caught():
    """The neuter, executed: an extra CronJob on lfds/audittrace-x must not equal the table + exclusions."""
    extra = yaml.safe_dump(
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
                                        "name": "x",
                                        "image": "docker.io/lfds/audittrace-x:1",
                                    }
                                ]
                            }
                        }
                    }
                }
            },
        }
    )
    found = _first_party_workloads(extra, ("docker.io/lfds/",))
    table = {("Deployment", row[0]) for row in verify.FIRST_PARTY_WORKLOADS}
    assert found - (table | _EXCLUSIONS) == {("CronJob", "audittrace-x")}


# ── carried over from v1 ──


def test_r6_evidence_sources_name_the_pod_reaper_read(tmp_path):
    srcs = VerifyRunner(_cfg(tmp_path)).evidence_sources()
    assert (
        "cluster-api:kubectl:pod-reaper-pod-imageID" in srcs
        and "registry:local:platform-manifests" in srcs
    )


def test_r7_old_skip_assertion_is_now_false(tmp_path, monkeypatch):
    res = probe11(tmp_path, monkeypatch, Registry(), _kube())
    assert res.evidence != {"bff": "skipped", "reason": "registry local"}


def test_fixtures_are_the_measured_indexes():
    """Positive control: the fixture bodies hash to the measured rc2/rc3 index digests."""
    assert (
        RC2_DIGEST
        == "sha256:0580d1fb82fd84c1daea2303f896246770bc59b887004706450ee96fdf0cc805"
    )
    assert (
        RC3_DIGEST
        == "sha256:861b43561e63360233a00522cf2ea9f1c3a33dcff35a6bd83b12766fb7caa95b"
    )
    assert json.loads(RC2_BODY)["manifests"][0]["digest"] == AMD64
