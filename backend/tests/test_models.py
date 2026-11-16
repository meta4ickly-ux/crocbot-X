import pytest

from app.models import (
    CHEAP_MODEL,
    CODE_MODEL,
    DEFAULT_MODEL,
    RetiredModelError,
    estimate_cost_usd,
    route_model,
    validate_model,
)


def test_retired_models_are_refused_not_redirected():
    for slug in ("grok-4", "grok-4-fast", "grok-4-1-fast-reasoning",
                 "grok-4-1-fast-non-reasoning", "grok-code-fast-1"):
        with pytest.raises(RetiredModelError, match="retired"):
            validate_model(slug)


def test_unknown_model_is_refused():
    with pytest.raises(ValueError, match="unknown model"):
        validate_model("grok-99")


def test_current_models_validate():
    assert validate_model("grok-4.7") == "grok-4.7"
    assert validate_model("grok-4.3") == "grok-4.3"
    assert validate_model("grok-build-0.1") == "grok-build-0.1"


def test_cost_estimate_and_long_context_multiplier():
    # grok-4.7: $2 in / $6 out per 1M, under the 200k threshold
    assert estimate_cost_usd("grok-4.7", 100_000, 0) == pytest.approx(0.2)
    assert estimate_cost_usd("grok-4.7", 0, 100_000) == pytest.approx(0.6)
    # >=200k prompt tokens doubles the whole request
    assert estimate_cost_usd("grok-4.7", 200_000, 0) == pytest.approx(0.8)


def test_route_model():
    assert route_model("simple") == CHEAP_MODEL
    assert route_model("code") == CODE_MODEL
    assert route_model("anything-else") == DEFAULT_MODEL
