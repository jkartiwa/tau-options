"""The v0 screen: bulk market-metrics pull → parse → filter → rank.

Filtering is split from parsing so the filter logic stays pure and testable
without constructing SDK response models: `parse()` turns one
MarketMetricInfo into a plain Candidate, `apply_filters()` stamps exclusion
reasons, `evaluate()` composes and ranks. Percent-scale convention:
everything on a Candidate is 0–100 — the API sends rank/percentile as 0–1
(scaled here) but the 30-day vols already as percents (verified live
2026-07-26: SMH iv30 arrives 34.81, ivr 0.938).
"""

from dataclasses import dataclass, replace
from datetime import date, timedelta

from tastytrade import Session
from tastytrade.metrics import MarketMetricInfo, get_market_metrics

# The symbols land comma-joined in one query string; chunk to keep URLs sane.
CHUNK = 90


@dataclass(frozen=True)
class Candidate:
    symbol: str
    ivr: float | None  # IV rank, 0–100
    ivp: float | None  # IV percentile, 0–100
    iv30: float | None  # 30-day implied vol, %
    hv30: float | None  # 30-day historical vol, %
    liquidity: int | None  # tasty liquidity rating, 4 best
    beta: float | None
    earnings_date: date | None  # next expected report, if any
    excluded: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.excluded


def _pct(value) -> float | None:
    return None if value is None else float(value) * 100


def parse(m: MarketMetricInfo) -> Candidate:
    earnings = None
    if m.earnings is not None and m.earnings.expected_report_date is not None:
        earnings = m.earnings.expected_report_date
    return Candidate(
        symbol=m.symbol,
        ivr=_pct(m.implied_volatility_index_rank),
        ivp=_pct(m.implied_volatility_percentile),
        iv30=None if m.implied_volatility_30_day is None else float(m.implied_volatility_30_day),
        hv30=None if m.historical_volatility_30_day is None else float(m.historical_volatility_30_day),
        liquidity=m.liquidity_rating,
        beta=None if m.beta is None else float(m.beta),
        earnings_date=earnings,
    )


def apply_filters(
    c: Candidate,
    *,
    min_ivr: float,
    min_liquidity: int,
    earnings_days: int,
    today: date,
) -> Candidate:
    reasons: list[str] = []
    if c.ivr is None:
        reasons.append("no IV rank")
    elif c.ivr < min_ivr:
        reasons.append(f"IVR {c.ivr:.0f} < {min_ivr:.0f}")
    if c.liquidity is None:
        reasons.append("no liquidity rating")
    elif c.liquidity < min_liquidity:
        reasons.append(f"liquidity {c.liquidity} < {min_liquidity}")
    if (
        earnings_days > 0
        and c.earnings_date is not None
        and today <= c.earnings_date <= today + timedelta(days=earnings_days)
    ):
        reasons.append(f"earnings {c.earnings_date.isoformat()}")
    return replace(c, excluded=tuple(reasons))


def rank(candidates: list[Candidate]) -> list[Candidate]:
    return sorted(
        candidates,
        key=lambda c: (c.ivr is None, -(c.ivr or 0), c.symbol),
    )


async def fetch_metrics(
    session: Session, symbols: list[str]
) -> list[MarketMetricInfo]:
    out: list[MarketMetricInfo] = []
    for i in range(0, len(symbols), CHUNK):
        out.extend(await get_market_metrics(session, symbols[i : i + CHUNK]))
    return out


def evaluate(
    metrics: list[MarketMetricInfo],
    *,
    min_ivr: float,
    min_liquidity: int,
    earnings_days: int,
    today: date,
) -> list[Candidate]:
    return rank(
        [
            apply_filters(
                parse(m),
                min_ivr=min_ivr,
                min_liquidity=min_liquidity,
                earnings_days=earnings_days,
                today=today,
            )
            for m in metrics
        ]
    )
