from datetime import date

from tau.screen import Candidate, apply_filters, rank

TODAY = date(2026, 7, 24)
FILTERS = dict(min_ivr=30.0, min_liquidity=3, earnings_days=45, today=TODAY)


def cand(**kw) -> Candidate:
    base = dict(
        symbol="TEST",
        ivr=50.0,
        ivp=60.0,
        iv30=25.0,
        hv30=20.0,
        liquidity=4,
        beta=1.0,
        earnings_date=None,
    )
    base.update(kw)
    return Candidate(**base)


def test_clean_candidate_passes():
    assert apply_filters(cand(), **FILTERS).passed


def test_low_ivr_excluded():
    c = apply_filters(cand(ivr=12.0), **FILTERS)
    assert not c.passed and "IVR 12 < 30" in c.excluded


def test_missing_ivr_excluded():
    assert "no IV rank" in apply_filters(cand(ivr=None), **FILTERS).excluded


def test_illiquid_excluded():
    c = apply_filters(cand(liquidity=1), **FILTERS)
    assert "liquidity 1 < 3" in c.excluded


def test_earnings_inside_window_excluded():
    c = apply_filters(cand(earnings_date=date(2026, 8, 12)), **FILTERS)
    assert c.excluded == ("earnings 2026-08-12",)


def test_earnings_outside_window_passes():
    assert apply_filters(cand(earnings_date=date(2026, 10, 1)), **FILTERS).passed


def test_earnings_filter_disabled():
    c = apply_filters(
        cand(earnings_date=date(2026, 7, 30)),
        min_ivr=30.0,
        min_liquidity=3,
        earnings_days=0,
        today=TODAY,
    )
    assert c.passed


def test_past_earnings_date_ignored():
    assert apply_filters(cand(earnings_date=date(2026, 7, 1)), **FILTERS).passed


def test_rank_ivr_desc_none_last():
    ranked = rank([cand(symbol="A", ivr=None), cand(symbol="B", ivr=80.0), cand(symbol="C", ivr=90.0)])
    assert [c.symbol for c in ranked] == ["C", "B", "A"]
