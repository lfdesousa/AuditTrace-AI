"""BFF-CHAT-ROUTE-ERRORS ADDENDUM-3 gate condition C1: an open quote.

``mesh_peer._split`` honours double quotes across the comma that Envoy's
APPEND_FORWARD inserts. A forwarded element that leaves a quote open would
therefore swallow the element this workload's own sidecar appends, and the
caller's own ``URI=`` would be read as the sidecar's statement. A header whose
quote state is still open at the end is not a header this parser can read:
no identity at all.

Neuter (C1): drop the open-quote refusal -> every ``open_quote`` cell below.
"""

from __future__ import annotations

import pytest

from audittrace.mesh_peer import peer_spiffe_id

BFF = "spiffe://cluster.local/ns/audittrace/sa/audittrace-librechat-bff"
GW = "spiffe://cluster.local/ns/istio-system/sa/istio-ingressgateway-service-account"
BY = "By=spiffe://cluster.local/ns/audittrace/sa/audittrace-memory-server;Hash=ab"
GATEWAY_ELEMENT = f'{BY};Subject="";URI={GW}'

OPEN_QUOTE: dict[str, str] = {
    "attribute_then_gateway": f'URI={BFF};a="' + "," + GATEWAY_ELEMENT,
    "subject_then_gateway": f'By=x;URI={BFF};Subject="x' + "," + GATEWAY_ELEMENT,
    "alone": f'{BY};Subject="x',
    "last_element_open": f'{BY};URI={BFF};Subject="CN=a',
    "escaped_closing_quote": f'{BY};Subject="x\\"' + "," + GATEWAY_ELEMENT,
}


@pytest.mark.parametrize("case", sorted(OPEN_QUOTE))
def test_an_open_quote_names_nobody(case: str) -> None:
    assert peer_spiffe_id(OPEN_QUOTE[case]) is None


def test_a_closed_quote_is_still_read() -> None:
    """Control: balanced quotes (including an escaped one inside) are read as before."""
    header = f'{BY};Subject="CN=a\\"b,c";URI={BFF}'
    assert peer_spiffe_id(header) == BFF
