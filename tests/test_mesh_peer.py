"""BFF-CHAT-ROUTE-ERRORS ADDENDUM-3: reading the mesh peer out of
``X-Forwarded-Client-Cert`` (``audittrace.mesh_peer``).

The LAST element is the one this workload's own sidecar appended; anything
before it travelled with the request and proves nothing. Exactly one URI, or
no identity at all.

Neuters: M1 split on every comma (quotes ignored) -> ``comma_in_a_quoted_
subject``; M2 take the first element -> ``earlier_hop_is_ignored``; M3 take the
first URI of several -> ``two_uris``; M4 match ``URI=`` anywhere in the element
-> ``uri_inside_a_quoted_subject``.
"""

from __future__ import annotations

import pytest

from audittrace.mesh_peer import peer_spiffe_id

BFF = "spiffe://cluster.local/ns/audittrace/sa/audittrace-librechat-bff"
GW = "spiffe://cluster.local/ns/istio-system/sa/istio-ingressgateway-service-account"
BY = "By=spiffe://cluster.local/ns/audittrace/sa/audittrace-memory-server;Hash=ab"

CASES: dict[str, tuple[str | None, str | None]] = {
    "absent": (None, None),
    "empty": ("", None),
    "one_element": (f'{BY};Subject="";URI={BFF}', BFF),
    "uri_key_in_lower_case": (f'{BY};Subject="";uri={BFF}', BFF),
    "quoted_uri": (f'{BY};URI="{BFF}"', BFF),
    "earlier_hop_is_ignored": (f"{BY};URI={BFF},{BY};URI={GW}", GW),
    "last_hop_wins": (f"{BY};URI={GW},{BY};URI={BFF}", BFF),
    "comma_in_a_quoted_subject": (f'{BY};Subject="CN=a,URI={BFF}";URI={GW}', GW),
    "uri_inside_a_quoted_subject": (f'{BY};Subject="x;URI={BFF}"', None),
    "two_uris": (f"{BY};URI={BFF};URI={GW}", None),
    "no_uri": (f'{BY};Subject="CN=a"', None),
    "empty_uri": (f"{BY};URI=", None),
    "trailing_comma": (f"{BY};URI={BFF},", None),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_peer_spiffe_id(case: str) -> None:
    header, expected = CASES[case]
    assert peer_spiffe_id(header) == expected
