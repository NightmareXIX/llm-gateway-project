"""D46's simulated cost, and the one distinction the whole feature rests on:
an unpriced model costs ``None``, a priced model that used nothing costs
``Decimal("0")``. Conflating the two makes a dashboard total quietly
understate itself in the flattering direction (trap 7).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from app.config import ConfigError, PricingConfig, _load_yaml_model, get_pricing_config
from app.usage.pricing import simulated_cost


def test_a_known_pair_of_token_counts_prices_correctly() -> None:
    """1,000,000 in + 1,000,000 out at $0.15/$0.75 per Mtok is exactly $0.90 —
    picked so the arithmetic is checkable by eye rather than trusted blind."""
    cost = simulated_cost("groq", "openai/gpt-oss-120b", tokens_in=1_000_000, tokens_out=1_000_000)

    assert cost == Decimal("0.90")


def test_an_unknown_model_returns_none_not_zero() -> None:
    assert simulated_cost("groq", "does-not-exist", tokens_in=1000, tokens_out=1000) is None
    assert simulated_cost("anthropic", "claude-opus", tokens_in=1000, tokens_out=1000) is None


def test_zero_tokens_on_a_priced_model_is_zero_not_none() -> None:
    """A priced model that used nothing cost nothing — a different fact from
    an unpriced model, and the two must never collapse into the same value."""
    cost = simulated_cost("groq", "openai/gpt-oss-120b", tokens_in=0, tokens_out=0)

    assert cost == Decimal("0")
    assert cost is not None


def test_the_result_is_a_decimal_with_no_float_involved() -> None:
    """A hand sum in float would round-trip fine for these inputs too — the
    point is that the type itself is Decimal, so it stays exact under
    thousands of accumulations rather than merely happening to today."""
    cost = simulated_cost("gemini", "gemini-3.5-flash-lite", tokens_in=333, tokens_out=777)

    assert isinstance(cost, Decimal)


def test_the_committed_pricing_table_loads() -> None:
    config = get_pricing_config()

    assert config.version == 1
    assert config.for_model("groq", "openai/gpt-oss-120b") is not None


def test_a_pricing_entry_rejects_a_stray_key(tmp_path: Path) -> None:
    document = {
        "version": 1,
        "pricing": {
            "groq": {
                "m": {
                    "input_per_mtok": "0.10",
                    "output_per_mtok": "0.50",
                    "currency": "usd",
                    "surprise": "field",
                }
            }
        },
    }
    path = tmp_path / "pricing.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        _load_yaml_model(PricingConfig, path)

    assert "surprise" in str(excinfo.value)


def test_an_unlisted_model_has_no_price_rather_than_a_wrong_one(tmp_path: Path) -> None:
    document = {
        "version": 1,
        "pricing": {
            "groq": {"m": {"input_per_mtok": "0.10", "output_per_mtok": "0.50", "currency": "usd"}}
        },
    }
    path = tmp_path / "pricing.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    config = _load_yaml_model(PricingConfig, path)

    assert config.for_model("groq", "not-in-the-table") is None
    assert config.for_model("anthropic", "claude") is None
