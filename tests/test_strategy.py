import pytest

from tau.payoff import OptionType, Side
from tau.strategies import ALL, STRATEGIES
from tau.strategy import (
    MAX_VARIANTS,
    Atm,
    Bias,
    Delta,
    LegSpec,
    Moneyness,
    Ref,
    Require,
    Strategy,
)

C, P = OptionType.CALL, OptionType.PUT
LONG, SHORT = Side.LONG, Side.SHORT


def spec(id_, type_=P, side=SHORT, strike=None, qty=1):
    return LegSpec(id_, type=type_, side=side, strike=strike or Delta(0.16), qty=qty)


def test_every_shipped_strategy_parses_and_validates():
    """The loader test that matters: a malformed definition must fail at
    import, not halfway through a scan."""
    assert len(STRATEGIES) == len(ALL)
    for strategy in ALL:
        assert strategy.legs
        assert strategy.variant_count <= MAX_VARIANTS


def test_every_shipped_strategy_constrains_spread_cost():
    """A four-legger crosses four markets; ranked on return alone it would
    win on fills that never happen."""
    for strategy in ALL:
        assert any(rule.metric == "spread_cost" for rule in strategy.require), (
            f"{strategy.name} has no spread_cost constraint"
        )


def test_scalar_selector_is_a_one_element_search():
    strategy = Strategy(name="s", bias=Bias.NEUTRAL, legs=[spec("a", strike=Delta(0.16))])
    assert strategy.variant_count == 1
    assert len(strategy.variants()) == 1


def test_list_selectors_multiply_into_variants():
    strategy = Strategy(
        name="s",
        bias=Bias.NEUTRAL,
        legs=[
            spec("a", strike=Delta([0.16, 0.30])),
            spec("b", type_=C, strike=Delta([0.16, 0.20, 0.30])),
        ],
    )
    assert strategy.variant_count == 6
    assert len(strategy.variants()) == 6


def test_variant_labels_name_the_shape():
    strategy = Strategy(
        name="s",
        bias=Bias.BULLISH,
        legs=[
            spec("short_put", strike=Delta(0.20)),
            spec("short_call", type_=C, strike=Delta(0.25)),
            spec("long_call", type_=C, side=LONG, strike=Ref("short_call", offset=10)),
        ],
    )
    assert [label for label, _ in strategy.variants()] == ["20Δ/25Δ+10"]


def test_selector_labels():
    assert Delta(0.16).label() == "16Δ"
    assert Moneyness(-0.05).label() == "-5%"
    assert Atm().label() == "ATM"
    assert Ref("x", offset=-10).label() == "-10"
    assert Ref("x", strikes=2).label() == "+2k"


def test_forward_reference_is_a_load_time_error():
    with pytest.raises(ValueError, match="not declared before it"):
        Strategy(
            name="s",
            bias=Bias.NEUTRAL,
            legs=[
                spec("a", strike=Ref("b", offset=5)),
                spec("b", strike=Delta(0.16)),
            ],
        )


def test_duplicate_leg_id_is_rejected():
    with pytest.raises(ValueError, match="duplicate leg id"):
        Strategy(name="s", bias=Bias.NEUTRAL, legs=[spec("a"), spec("a")])


def test_ref_needs_exactly_one_of_offset_or_strikes():
    with pytest.raises(ValueError, match="exactly one"):
        Strategy(
            name="s",
            bias=Bias.NEUTRAL,
            legs=[spec("a"), spec("b", strike=Ref("a", offset=5, strikes=1))],
        )
    with pytest.raises(ValueError, match="exactly one"):
        Strategy(
            name="s",
            bias=Bias.NEUTRAL,
            legs=[spec("a"), spec("b", strike=Ref("a"))],
        )


def test_unknown_metric_in_a_constraint_is_rejected():
    with pytest.raises(ValueError, match="unknown metric"):
        Strategy(
            name="s",
            bias=Bias.NEUTRAL,
            legs=[spec("a")],
            require=[Require("worst_loss_upp", "<=", 0)],
        )


def test_unknown_rank_metric_is_rejected():
    with pytest.raises(ValueError, match="unknown rank metric"):
        Strategy(name="s", bias=Bias.NEUTRAL, legs=[spec("a")], rank="sharpe")


def test_variant_cap_fails_loudly_rather_than_truncating():
    ladder = [round(0.05 * i, 2) for i in range(1, 10)]  # 9 values
    with pytest.raises(ValueError, match="exceeds the"):
        Strategy(
            name="s",
            bias=Bias.NEUTRAL,
            legs=[
                spec("a", strike=Delta(ladder)),
                spec("b", type_=C, strike=Delta(ladder)),
            ],
        )


def test_legs_are_frozen_into_tuples_for_a_stable_identity():
    strategy = Strategy(name="s", bias=Bias.NEUTRAL, legs=[spec("a")])
    assert isinstance(strategy.legs, tuple)
    assert isinstance(strategy.require, tuple)
