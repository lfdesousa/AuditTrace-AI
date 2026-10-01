"""#459 WU-459-1 distribution maths: T1 (renormalisation), T2 (T properties)."""

from __future__ import annotations

import math
import random

import pytest

from audittrace.services.decision.distribution import (
    choice_and_confidence,
    entropy,
    renormalise,
)

T_GRID = (0.25, 0.5, 0.7, 1.0, 1.5, 4.0)
N_GRID = (2, 5, 24)
EXTRA_IDS = tuple(range(1000, 1006))  # non-allowed tokens in the top list


def _absent_patterns(n: int) -> dict[str, frozenset[int]]:
    return {
        "none": frozenset(),
        "first": frozenset({0}),
        "last": frozenset({n - 1}),
        "alternate": frozenset(range(0, n, 2)) if n > 2 else frozenset({0}),
        "all_but_one": frozenset(range(n - 1)),
    }


def _fixture(
    rng: random.Random, n: int, absent: frozenset[int]
) -> tuple[dict[int, float], list[int]]:
    """Allowed ids 32..; non-allowed tokens hold >= 30% of the probability mass."""
    allowed = list(range(32, 32 + n))
    top: dict[int, float] = {}
    for i, token_id in enumerate(allowed):
        if i not in absent:
            top[token_id] = rng.uniform(-6.0, -0.3)
    # Extras: total mass in [0.30, 0.45] (exp(lp) summed), so a normaliser
    # that includes them is visibly different.
    mass = rng.uniform(0.30, 0.45)
    for token_id in EXTRA_IDS:
        top[token_id] = math.log(mass / len(EXTRA_IDS))
    return top, allowed


def _reference(top: dict[int, float], allowed: list[int], temp: float) -> list[float]:
    """Independent reference: plain softmax over allowed present ids (fsum)."""
    weights = [math.exp(top[i] / temp) if i in top else 0.0 for i in allowed]
    total = math.fsum(weights)
    return [w / total for w in weights]


@pytest.mark.parametrize("temp", T_GRID)
@pytest.mark.parametrize("n", N_GRID)
def test_t1_matches_independent_reference_over_grid(n: int, temp: float) -> None:
    rng = random.Random(f"t1-{n}-{temp}")
    for name, absent in _absent_patterns(n).items():
        for _ in range(5):
            top, allowed = _fixture(rng, n, absent)
            got = renormalise(top, allowed, temp)
            want = _reference(top, allowed, temp)
            assert len(got) == n, name
            for g, w in zip(got, want, strict=True):
                assert abs(g - w) <= 1e-12, (name, g, w)
            assert abs(math.fsum(got) - 1.0) <= 1e-12
            for i in absent:
                assert got[i] == 0.0, name


def test_t1_absent_allowed_id_is_probability_zero_not_a_default() -> None:
    got = renormalise({33: -0.5, 34: -0.5}, [32, 33, 34], 1.0)
    assert got[0] == 0.0
    assert got[1] == pytest.approx(0.5)
    assert got[2] == pytest.approx(0.5)


def test_t1_all_allowed_absent_raises_value_error() -> None:
    with pytest.raises(ValueError, match="no allowed token"):
        renormalise({1000: -0.1}, [32, 33], 1.0)


def test_t1_no_allowed_ids_at_all_raises_value_error() -> None:
    with pytest.raises(ValueError, match="no allowed token"):
        renormalise({1000: -0.1}, [], 1.0)


@pytest.mark.parametrize("temp", [0.0, -1.0, float("nan"), float("inf"), float("-inf")])
def test_t1_bad_temperature_raises_value_error(temp: float) -> None:
    with pytest.raises(ValueError, match="temperature"):
        renormalise({32: -0.1, 33: -2.0}, [32, 33], temp)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_t1_nan_or_plus_inf_logprob_raises_value_error(bad: float) -> None:
    with pytest.raises(ValueError, match="NaN or"):
        renormalise({32: bad, 33: -2.0}, [32, 33], 1.0)


def test_t1_very_negative_logprobs_stay_finite_log_sum_exp() -> None:
    # A naive exp(lp/T) underflows to 0/0 here; the stable form must not.
    got = renormalise({32: -1000.0, 33: -1001.0}, [32, 33], 1.0)
    assert got == pytest.approx(
        [1 / (1 + math.exp(-1)), math.exp(-1) / (1 + math.exp(-1))]
    )


def _random_vector(rng: random.Random) -> list[float]:
    # Neither one-hot nor uniform (generator excludes both, SF-4).
    while True:
        n = rng.choice(N_GRID)
        lps = [rng.uniform(-8.0, -0.1) for _ in range(n)]
        if max(lps) - min(lps) > 0.5:
            return lps


def test_t2_argmax_invariant_and_confidence_strictly_decreasing_in_t() -> None:
    rng = random.Random(459)
    for _ in range(250):
        lps = _random_vector(rng)
        allowed = list(range(32, 32 + len(lps)))
        top = dict(zip(allowed, lps, strict=True))
        picks: list[int] = []
        confs: list[float] = []
        for temp in T_GRID:
            index, conf = choice_and_confidence(renormalise(top, allowed, temp))
            picks.append(index)
            confs.append(conf)
        assert len(set(picks)) == 1, (lps, picks)
        assert picks[0] == lps.index(max(lps))
        for lo, hi in zip(confs, confs[1:], strict=False):
            assert hi < lo, (lps, confs)  # STRICT, not just non-increasing


def test_choice_and_confidence_tie_breaks_to_first_index() -> None:
    assert choice_and_confidence([0.4, 0.4, 0.2]) == (0, 0.4)
    assert choice_and_confidence([0.1, 0.7, 0.2]) == (1, 0.7)


def test_entropy_matches_reference_and_one_hot_is_positive_zero() -> None:
    dist = [0.5, 0.25, 0.25]
    want = -(0.5 * math.log(0.5) + 2 * 0.25 * math.log(0.25))
    assert entropy(dist) == pytest.approx(want, abs=1e-12)
    assert entropy([1.0, 0.0, 0.0]) == 0.0
    assert math.copysign(1.0, entropy([1.0, 0.0])) == 1.0  # not -0.0
    assert entropy([0.25] * 4) == pytest.approx(math.log(4), abs=1e-12)  # nats
