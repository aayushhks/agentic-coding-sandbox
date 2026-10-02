import json
from pathlib import Path

import pytest

import bench.prices
from bench.prices import price_for
from tests.bench_helpers import make_job


def test_the_pinned_price_says_where_it_came_from() -> None:
    price = price_for("qwen/qwen3.8-27b")
    assert price is not None
    assert (price.provider, price.currency) == ("groq", "USD")
    assert (price.input_per_million_tokens, price.output_per_million_tokens) == (0.8, 4.0)
    assert price.source.startswith("https://") and price.retrieved and price.how
    assert price_for("a-model-nobody-priced") is None


def test_cost_charges_prompt_and_completion_tokens_at_their_own_rates() -> None:
    price = price_for("qwen/qwen3.8-27b")
    assert price is not None
    assert price.cost(1_000_000, 0) == pytest.approx(0.8)
    assert price.cost(0, 1_000_000) == pytest.approx(4.0)
    assert price.cost(91_083, 17_721) == pytest.approx((91_083 * 0.8 + 17_721 * 4.0) / 1e6)


def test_a_trial_s_cost_is_the_sum_of_its_jobs_only_when_every_job_has_one() -> None:
    from bench.metrics import compute_metrics

    priced = [make_job(0, 1, cost_usd=0.25), make_job(1, 2, cost_usd=0.5)]
    assert compute_metrics(priced, workers=1).cost_usd == pytest.approx(0.75)
    assert compute_metrics([*priced, make_job(2, 3)], workers=1).cost_usd is None


def test_a_price_list_can_be_swapped_for_a_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prices = tmp_path / "prices.json"
    entry = {
        "provider": "mock",
        "input_per_million_tokens": 1.0,
        "output_per_million_tokens": 2.0,
        "source": "test",
        "retrieved": "today",
        "how": "made up for the test",
    }
    prices.write_text(json.dumps({"currency": "USD", "models": {"mock-model": entry}}))
    monkeypatch.setattr(bench.prices, "PRICES_PATH", prices)
    price = price_for("mock-model")
    assert price is not None and price.cost(2, 3) == pytest.approx(8e-6)
