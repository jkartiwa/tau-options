import pytest

from tau.cli import _rank_summary, _selected_strategies
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


def test_an_empty_rank_says_so_rather_than_printing_a_bare_header():
    """Refusing a variant that missed its requested delta means a thin day can
    leave every name without a structure. That has to read as a result — a
    table with nothing under the header looks like a tool that broke."""
    summary = _rank_summary(0, 15)
    assert "15" in summary
    assert "no structure" in summary


def test_a_partly_priced_rank_reports_how_many_names_produced_one():
    assert _rank_summary(3, 15) == "3 of 15 names produced a structure"


def test_bpr_formatting_marks_the_formula_estimate():
    """Broker figures render plain under the `BPR` header; the formula
    estimate carries the tilde, and a missing figure stays a dash."""
    from tau.cli import _bpr

    assert _bpr(3651.0, "broker") == "3,651"
    assert _bpr(3980.0, "estimate") == "3,980~"
    assert _bpr(None, "estimate") == "—"
