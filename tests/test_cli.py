import pytest

from tau.cli import _selected_strategies
from tau.strategies import ALL, MIN_POP
from tau.strategy import Require


def test_selected_strategies_default_to_the_shipped_pop_floor():
    strategies = _selected_strategies(None)
    assert len(strategies) == len(ALL)
    for strategy in strategies:
        assert Require("pop", ">=", MIN_POP) in strategy.require


def test_min_pop_flag_overrides_the_shipped_floor():
    strategies = _selected_strategies(None, min_pop=0.80)
    for strategy in strategies:
        assert Require("pop", ">=", 0.80) in strategy.require
        assert Require("pop", ">=", MIN_POP) not in strategy.require


def test_unknown_strategy_name_is_a_hard_error():
    with pytest.raises(SystemExit, match="unknown strategy"):
        _selected_strategies(["not-a-real-strategy"])
