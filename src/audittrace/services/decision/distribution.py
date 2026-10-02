"""Pure distribution maths (#459 WU-459-1). No I/O.

The softmax is computed HERE from the raw logprobs (never taken from the
runtime). Measured on llama.cpp b10288: ``n_probs`` logprobs are pre-sampler,
so a client-side temperature over them is valid. The argmax never depends on
T; the probabilities, confidence and entropy do.

"Exactly" (recompute == recorded) means THE SAME IMPLEMENTATION on the same
float inputs. A third party recomputing with other maths libraries should
compare with ``abs <= 1e-9``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def renormalise(
    top_logprobs_by_id: Mapping[int, float],
    allowed_ids: Sequence[int],
    temperature: float,
) -> list[float]:
    """``p_i = exp(lp_i/T) / sum_j exp(lp_j/T)`` over ``allowed_ids`` only.

    Log-sum-exp stable. An allowed id ABSENT from the top list gets ``-inf``
    (probability 0). Matching is by token id only (a stripped-string match
    once let " B" overwrite "B"). Raises ``ValueError`` when T is not > 0 and
    finite, when a returned logprob is NaN/+inf, or when NO allowed id is
    present in the top list.
    """
    if not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("temperature must be > 0 and finite")
    scaled: list[float] = []
    for token_id in allowed_ids:
        lp = top_logprobs_by_id.get(token_id)
        if lp is None:
            scaled.append(-math.inf)
            continue
        if math.isnan(lp) or lp == math.inf:
            raise ValueError("logprob must not be NaN or +inf")
        scaled.append(lp / temperature)
    peak = max(scaled, default=-math.inf)
    if peak == -math.inf:
        raise ValueError("no allowed token id present in the top list")
    exps = [math.exp(x - peak) for x in scaled]
    total = math.fsum(exps)
    return [e / total for e in exps]


def choice_and_confidence(dist: Sequence[float]) -> tuple[int, float]:
    """Argmax index (first on ties) and its probability."""
    index = max(range(len(dist)), key=lambda i: (dist[i], -i))
    return index, dist[index]


def entropy(dist: Sequence[float]) -> float:
    """Shannon entropy in nats (0 * ln 0 := 0)."""
    # ``+ 0.0`` turns the -0.0 of a one-hot distribution into 0.0.
    return -math.fsum(p * math.log(p) for p in dist if p > 0) + 0.0
