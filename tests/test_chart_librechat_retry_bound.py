"""BFF-CHAT-ROUTE-ERRORS R9 (operator ruling Q2 = yes): the chart bounds the
LibreChat fork's retries for the AuditTrace endpoint.

Why: the fork's model client retries every 5xx six times (7 requests, about
70-120 s of silence per refused turn). ``addParams.maxRetries`` on the custom
endpoint is the bound the client honours (measured on the fork's built
``getOpenAIConfig``: absent -> 7 requests, ``1`` -> 2 requests). The value is a
chart value, so another target is a profile, not an edit of the template.

Neuters (each alone must redden): delete the ``addParams`` block of
``templates/console/configmap-librechat-yaml.yaml``; hard-code the number
instead of reading ``console.librechat.endpointMaxRetries`` (the override
test); drop the value from ``values.yaml`` (the default test).
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

import yaml

from tests.test_console_k3s_guards import (
    _LINT_SECRETS,
    CHART_DIR,
    NAMESPACE,
    RELEASE,
    _librechat_yaml_configmap_doc,
)


def _render_endpoint(*extra: str) -> dict[str, Any]:
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
            *extra,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    docs = [d for d in yaml.safe_load_all(result.stdout) if isinstance(d, dict)]
    endpoint: dict[str, Any] = _librechat_yaml_configmap_doc(docs)["endpoints"][
        "custom"
    ][0]
    assert endpoint["name"] == "AuditTrace"
    return endpoint


def test_the_audittrace_endpoint_bounds_the_fork_retries_to_one() -> None:
    assert _render_endpoint()["addParams"] == {"maxRetries": 1}


def test_the_bound_comes_from_the_chart_value() -> None:
    endpoint = _render_endpoint("--set", "console.librechat.endpointMaxRetries=0")
    assert endpoint.get("addParams") == {"maxRetries": 0}
