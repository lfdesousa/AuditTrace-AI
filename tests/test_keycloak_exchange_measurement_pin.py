"""BFF-CHAT-ROUTE-ERRORS ADDENDUM-2, item 7: the BFF matches on what Keycloak
ANSWERS, not on what an RFC says, so a Keycloak upgrade must re-measure it.

``bff.exchange`` turns exactly ``400`` + ``"error": "invalid_token"`` into the
caller's 401. That pair is Keycloak 24's own wording (RFC 8693 section 2.2.2
says ``invalid_request``). A version that words it differently makes every
refused subject token a 502 without any test failing. This guard fails instead:
when the Keycloak the repository pins is not the Keycloak the recorded
measurement was taken on, or when the BFF's pair is not the measured one.

To turn it green after a Keycloak bump: run
``scripts/keycloak/run-exchange-refusals-measurement.sh`` (one throwaway
container), which rewrites ``tests/fixtures/keycloak_exchange_refusals.json``.

Neuters (each alone): U1 bump the chart's Keycloak tag -> ``test_every_pinned
_keycloak_is_the_measured_one``; U2 change the BFF's status or error constant ->
``test_the_bff_pair_is_the_measured_pair``; U3 make a configuration cause in the
fixture answer the pair -> ``test_the_pair_tells_the_two_causes_apart``; U4
delete the instrument -> ``test_the_instrument_is_in_the_repository``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from tests.test_console_k3s_guards import _LINT_SECRETS, CHART_DIR, NAMESPACE, RELEASE

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "keycloak_exchange_refusals.json"
INSTRUMENT = (
    "scripts/keycloak/run-exchange-refusals-measurement.sh",
    "scripts/keycloak/measure_exchange_refusals.py",
)


def _measured() -> dict[str, Any]:
    assert FIXTURE.is_file(), f"no recorded measurement: run {INSTRUMENT[0]}"
    measured: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return measured


def _tag(image: str) -> str:
    return image.rsplit("@", 1)[0].rsplit(":", 1)[1]


def _chart_keycloak_images() -> set[str]:
    helm = shutil.which("helm")
    assert helm is not None, "helm CLI not on PATH: the chart render tests need it"
    result = subprocess.run(
        [
            helm,
            "template",
            RELEASE,
            str(CHART_DIR),
            "-n",
            NAMESPACE,
            "--set",
            "console.enabled=true",
            *_LINT_SECRETS,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    images: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            image = node.get("image")
            if isinstance(image, str) and "keycloak" in image:
                images.add(image)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for doc in yaml.safe_load_all(result.stdout):
        walk(doc)
    return images


def _compose_keycloak_images() -> set[str]:
    images: set[str] = set()
    for path in sorted(REPO_ROOT.glob("docker-compose*.yml")):
        services = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(
            "services", {}
        )
        for service in services.values():
            image = service.get("image", "")
            if "keycloak" in image:
                images.add(image)
    return images


def test_every_pinned_keycloak_is_the_measured_one() -> None:
    measured = _measured()
    series = _tag(measured["image"])  # e.g. "24.0"
    assert measured["keycloak_version"].startswith(series + ".")
    chart, compose = _chart_keycloak_images(), _compose_keycloak_images()
    assert chart, "the chart renders no Keycloak container"
    assert chart == {measured["image"]}, (
        f"the chart pins {sorted(chart)} but the token-exchange refusals were "
        f"measured on {measured['image']} ({measured['keycloak_version']}): "
        f"re-run {INSTRUMENT[0]}"
    )
    assert compose, "no compose file pins a Keycloak image"
    for image in compose:
        assert _tag(image).split("-")[0] == series, (image, series)


def test_the_bff_pair_is_the_measured_pair() -> None:
    from bff.exchange import SUBJECT_REFUSAL_ERROR, SUBJECT_REFUSAL_STATUS

    refused = {
        (case["status"], case["error"])
        for case in _measured()["cases"].values()
        if case["cause_class"] == "subject-token" and case["status"] != 200
    }
    assert refused == {(SUBJECT_REFUSAL_STATUS, SUBJECT_REFUSAL_ERROR)}


def test_the_pair_tells_the_two_causes_apart() -> None:
    """No configuration cause may answer the pair: it would become a 401 that
    tells a user to sign in again for a server fault."""
    from bff.exchange import SUBJECT_REFUSAL_ERROR, SUBJECT_REFUSAL_STATUS

    cases = _measured()["cases"]
    configuration = [
        c for c in cases.values() if c["cause_class"] == "bff-configuration"
    ]
    assert len(configuration) >= 5
    assert not [
        case
        for case in configuration
        if (case["status"], case["error"])
        == (SUBJECT_REFUSAL_STATUS, SUBJECT_REFUSAL_ERROR)
    ]


def test_the_instrument_is_in_the_repository() -> None:
    for relative in INSTRUMENT:
        assert (REPO_ROOT / relative).is_file(), relative
