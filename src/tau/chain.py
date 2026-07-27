"""Option-chain reads for one symbol, and the short-premium structures built
over them.

Fetching is one DXLink pass per symbol, measured at ~1.4s all-in (0.4s chain
metadata, 1.0s quotes+greeks over ~40 legs), which is what makes per-symbol
on-demand viable in the TUI instead of batch-pulling the whole shortlist.

Spot has to be known before strikes can be chosen sensibly, so the pass is
two-phase over a single connection: underlying quote first, then a strike
window around it.

Degradation follows the same rule as the rest of the stack: a leg missing a
quote or greeks is dropped rather than defaulted, and a structure that loses
either side reports itself incomplete rather than quoting a partial credit —
half a strangle's credit is a wrong number, not an imprecise one.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import sqrt

from tastytrade import DXLinkStreamer, Session
from tastytrade.dxfeed import Greeks, Quote
from tastytrade.instruments import NestedOptionChain

EVENT_TIMEOUT = 10.0
TARGET_DTE = 45
TARGET_DELTA = 0.16
# The strike window is scaled by expected move, not by a fixed percentage:
# a 16-delta wing sits near one standard deviation, so 2.5 sigma contains it
# on any name. Capping by strike *count* instead was the original bug — on a
# densely struck name like QQQ the window stopped well inside the wings and
# the delta pick silently degraded to the nearest available strike (0.38 in
# place of 0.16). Count is capped by striding the window instead, which keeps
# the streamer pass ~1s while still spanning it.
SIGMA_SPAN = 2.5
MIN_WINDOW = 0.08  # floor, for a low-vol name or a missing IV hint
FALLBACK_IV = 0.35
MAX_STRIKES_PER_SIDE = 26
DELTA_TOLERANCE = 0.05  # beyond this, the pick is reported as off-target
DAYS_PER_YEAR = 365.0

# tastytrade's own expected-move convention: the ATM straddle blended with
# the first two OTM strangles, weighted 60/30/10 — not the plainer
# straddle*0.85 heuristic (Brenner-Subrahmanyam gives ~0.7979 as the more
# precise version of that constant, and different desks round it
# differently). Weighting in the wings is a cheap skew correction: a
# single-strike straddle only samples the smile at one point. Falls back to
# the 0.85 straddle-only heuristic when the wing strikes are not both
# priced, since that can still happen on a thin chain or a narrow fetch
# window; falls back to None only if even the straddle is unpriced.
EM_WEIGHTS = (0.6, 0.3, 0.1)  # straddle, 1st OTM strangle, 2nd OTM strangle
STRADDLE_ONLY_FACTOR = 0.85


@dataclass(frozen=True)
class Leg:
    occ: str
    streamer: str
    strike: float
    right: str  # "C" or "P"
    bid: float | None = None
    ask: float | None = None
    delta: float | None = None
    theta: float | None = None
    iv: float | None = None

    @property
    def priced(self) -> bool:
        return self.bid is not None and self.ask is not None

    @property
    def mid(self) -> float | None:
        if not self.priced:
            return None
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float | None:
        if not self.priced:
            return None
        return self.ask - self.bid


@dataclass(frozen=True)
class StrikeRow:
    """One strike's call and put together — the natural unit for a straddle
    or a strangle leg pair, as opposed to `Leg`, which is one side alone."""

    strike: float
    call: Leg | None
    put: Leg | None


def strike_ladder(legs: tuple[Leg, ...]) -> list[StrikeRow]:
    by_strike: dict[float, dict[str, Leg]] = {}
    for leg in legs:
        by_strike.setdefault(leg.strike, {})[leg.right] = leg
    return [
        StrikeRow(strike, sides.get("C"), sides.get("P"))
        for strike, sides in sorted(by_strike.items())
    ]


def _atm_index(ladder: list[StrikeRow], spot: float) -> int | None:
    if not ladder:
        return None
    return min(range(len(ladder)), key=lambda i: abs(ladder[i].strike - spot))


def _straddle_mid(row: StrikeRow) -> float | None:
    """Call mid + put mid at one strike. Both sides must be priced — half a
    straddle is not a smaller straddle, it is a different, wrong number."""
    if row.call is None or row.put is None:
        return None
    if not row.call.priced or not row.put.priced:
        return None
    return row.call.mid + row.put.mid


def _strangle_mid(ladder: list[StrikeRow], atm_idx: int, n: int) -> float | None:
    """Call n strikes above the ATM strike + put n strikes below it — the
    'nth OTM strangle' tastytrade's expected-move formula weights in."""
    up, down = atm_idx + n, atm_idx - n
    if up >= len(ladder) or down < 0:
        return None
    call, put = ladder[up].call, ladder[down].put
    if call is None or put is None or not call.priced or not put.priced:
        return None
    return call.mid + put.mid


@dataclass(frozen=True)
class Cycle:
    symbol: str
    expiration: date
    dte: int
    underlying: float | None
    legs: tuple[Leg, ...]
    fetched_at: datetime
    expirations: tuple[tuple[date, int], ...] = ()  # every cycle available

    @property
    def atm_iv(self) -> float | None:
        """Call and put IV at the nearest strike to spot, averaged — a
        single leg's IV is skew-biased (puts richer than calls, typically),
        so blending both sides of the same strike is the honest read."""
        ladder = strike_ladder(self.legs)
        idx = _atm_index(ladder, self.underlying) if self.underlying else None
        if idx is None:
            return None
        row = ladder[idx]
        ivs = [leg.iv for leg in (row.call, row.put) if leg and leg.iv is not None]
        return sum(ivs) / len(ivs) if ivs else None

    @property
    def expected_move(self) -> float | None:
        value = self._expected_move_calc()
        return None if value is None else value[0]

    @property
    def expected_move_method(self) -> str | None:
        value = self._expected_move_calc()
        return None if value is None else value[1]

    def _expected_move_calc(self) -> tuple[float, str] | None:
        if self.underlying is None:
            return None
        ladder = strike_ladder(self.legs)
        idx = _atm_index(ladder, self.underlying)
        if idx is None:
            return None
        straddle = _straddle_mid(ladder[idx])
        if straddle is None:
            return None
        w1 = _strangle_mid(ladder, idx, 1)
        w2 = _strangle_mid(ladder, idx, 2)
        if w1 is not None and w2 is not None:
            a, b, c = EM_WEIGHTS
            return (a * straddle + b * w1 + c * w2, "weighted")
        return (straddle * STRADDLE_ONLY_FACTOR, "straddle×0.85")


@dataclass(frozen=True)
class Strangle:
    put: Leg | None
    call: Leg | None
    reason: str | None = None  # set when the structure could not be completed
    target_delta: float = TARGET_DELTA

    @property
    def complete(self) -> bool:
        return self.reason is None and self.put is not None and self.call is not None

    @property
    def off_target(self) -> float | None:
        """Worst absolute miss against the requested delta. The chain only
        offers the strikes it offers, so a miss is normal on a coarse ladder —
        but it must be shown, not folded into a number labelled 16 delta."""
        if not self.complete:
            return None
        return max(
            abs(abs(self.put.delta) - self.target_delta),
            abs(abs(self.call.delta) - self.target_delta),
        )

    @property
    def credit(self) -> float | None:
        if not self.complete:
            return None
        return self.put.mid + self.call.mid

    @property
    def breakevens(self) -> tuple[float, float] | None:
        if not self.complete:
            return None
        credit = self.credit
        return (self.put.strike - credit, self.call.strike + credit)

    @property
    def worst_spread(self) -> float | None:
        if not self.complete:
            return None
        return max(self.put.spread, self.call.spread)


def pick_by_delta(
    legs: tuple[Leg, ...], right: str, target: float = TARGET_DELTA
) -> Leg | None:
    """Nearest priced leg to the target absolute delta on one side."""
    usable = [
        leg
        for leg in legs
        if leg.right == right and leg.delta is not None and leg.priced
    ]
    if not usable:
        return None
    return min(usable, key=lambda leg: abs(abs(leg.delta) - target))


def build_strangle(cycle: Cycle, target: float = TARGET_DELTA) -> Strangle:
    put = pick_by_delta(cycle.legs, "P", target)
    call = pick_by_delta(cycle.legs, "C", target)
    if put is None or call is None:
        missing = " and ".join(
            side for side, leg in (("put", put), ("call", call)) if leg is None
        )
        return Strangle(put, call, reason=f"no priced {missing} leg with greeks")
    return Strangle(put, call, target_delta=target)


def be_vs_expected_move(cycle: Cycle, strangle: Strangle) -> float | None:
    """How far the nearer breakeven sits in expected moves. Under 1.0 means
    a single standard deviation reaches it."""
    em = cycle.expected_move
    bes = strangle.breakevens
    if em is None or bes is None or cycle.underlying is None or em == 0:
        return None
    nearest = min(cycle.underlying - bes[0], bes[1] - cycle.underlying)
    return nearest / em


async def _collect(streamer, cls, want: set[str], out: dict, timeout: float) -> None:
    try:
        async with asyncio.timeout(timeout):
            while want - out.keys():
                event = await streamer.get_event(cls)
                if event.event_symbol in want:
                    out[event.event_symbol] = event
    except TimeoutError:
        pass


def _f(value) -> float | None:
    return None if value is None else float(value)


def _stride(items: list, cap: int) -> list:
    """Thin a list to at most `cap` entries, keeping the outermost one so the
    window's edge survives. Delta moves smoothly across strikes, so a strided
    sample still lands within a strike or two of the target."""
    if len(items) <= cap:
        return items
    step = -(-len(items) // cap)  # ceil
    thinned = items[::step]
    if items[-1] not in thinned:
        thinned.append(items[-1])
    return thinned


def select_strikes(
    strikes: list,
    underlying: float | None,
    dte: int,
    iv_hint: float | None = None,
) -> list:
    """Strikes spanning +/- SIGMA_SPAN standard deviations around spot, thinned
    to MAX_STRIKES_PER_SIDE a side. `iv_hint` is annualized (0-1); the caller
    usually has one free from the metrics pull."""
    if underlying is None:
        mid = len(strikes) // 2
        return strikes[max(0, mid - MAX_STRIKES_PER_SIDE) : mid + MAX_STRIKES_PER_SIDE]
    iv = iv_hint if iv_hint else FALLBACK_IV
    sigma = underlying * iv * sqrt(max(dte, 1) / DAYS_PER_YEAR)
    half = max(SIGMA_SPAN * sigma, underlying * MIN_WINDOW)
    lo, hi = underlying - half, underlying + half
    window = [s for s in strikes if lo <= float(s.strike_price) <= hi]
    below = [s for s in window if float(s.strike_price) <= underlying]
    above = [s for s in window if float(s.strike_price) > underlying]
    # Reverse `below` so striding keeps the far strike, then restore order.
    return sorted(
        _stride(below[::-1], MAX_STRIKES_PER_SIDE) + _stride(above, MAX_STRIKES_PER_SIDE),
        key=lambda s: float(s.strike_price),
    )


MONTHLY_EXPIRATION_TYPE = "Regular"


def is_monthly(expiration) -> bool:
    """True for a standard monthly (3rd-Friday) expiration. Weeklies and
    quarterlies are excluded — monthly-only for now, since liquidity and
    the rest of the pipeline haven't been verified against the thinner
    weekly chains yet."""
    return expiration.expiration_type == MONTHLY_EXPIRATION_TYPE


def choose_expiration(chain, target_dte: int):
    live = [
        e for e in chain.expirations if e.days_to_expiration >= 0 and is_monthly(e)
    ]
    if not live:
        return None
    return min(live, key=lambda e: abs(e.days_to_expiration - target_dte))


async def fetch_cycle(
    session: Session,
    symbol: str,
    target_dte: int = TARGET_DTE,
    expiration: date | None = None,
    iv_hint: float | None = None,
) -> Cycle:
    """One cycle of one symbol, quoted. `expiration` pins a specific cycle;
    otherwise the one nearest `target_dte` is chosen. `iv_hint` (annualized,
    0-1) sizes the strike window and comes free from the metrics pull."""
    chains = await NestedOptionChain.get(session, symbol)
    if not chains:
        raise ValueError(f"no option chain for {symbol}")
    chain = max(chains, key=lambda c: len(c.expirations))
    available = tuple(
        (e.expiration_date, e.days_to_expiration)
        for e in sorted(chain.expirations, key=lambda e: e.expiration_date)
        if e.days_to_expiration >= 0 and is_monthly(e)
    )
    if expiration is not None:
        exp = next(
            (
                e
                for e in chain.expirations
                if e.expiration_date == expiration and is_monthly(e)
            ),
            None,
        )
    else:
        exp = choose_expiration(chain, target_dte)
    if exp is None:
        raise ValueError(f"no usable expiration for {symbol}")

    strikes = sorted(exp.strikes, key=lambda s: float(s.strike_price))
    quotes: dict = {}
    greeks: dict = {}
    underlying: float | None = None

    async with DXLinkStreamer(session) as streamer:
        # Phase one: spot, so the strike window can be centred honestly.
        await streamer.subscribe(Quote, [symbol])
        spot_out: dict = {}
        await _collect(streamer, Quote, {symbol}, spot_out, EVENT_TIMEOUT)
        spot_quote = spot_out.get(symbol)
        if spot_quote is not None:
            bid, ask = _f(spot_quote.bid_price), _f(spot_quote.ask_price)
            if bid is not None and ask is not None:
                underlying = (bid + ask) / 2

        selected = select_strikes(
            strikes, underlying, exp.days_to_expiration, iv_hint
        )

        streamer_symbols = [s.call_streamer_symbol for s in selected] + [
            s.put_streamer_symbol for s in selected
        ]
        # Phase two: the strike window, quotes and greeks together.
        await streamer.subscribe(Quote, streamer_symbols)
        await streamer.subscribe(Greeks, streamer_symbols)
        want = set(streamer_symbols)
        await asyncio.gather(
            _collect(streamer, Quote, want, quotes, EVENT_TIMEOUT),
            _collect(streamer, Greeks, want, greeks, EVENT_TIMEOUT),
        )

    legs: list[Leg] = []
    for strike in selected:
        for right, occ, stream_sym in (
            ("C", strike.call, strike.call_streamer_symbol),
            ("P", strike.put, strike.put_streamer_symbol),
        ):
            quote = quotes.get(stream_sym)
            greek = greeks.get(stream_sym)
            legs.append(
                Leg(
                    occ=occ,
                    streamer=stream_sym,
                    strike=float(strike.strike_price),
                    right=right,
                    bid=_f(quote.bid_price) if quote else None,
                    ask=_f(quote.ask_price) if quote else None,
                    delta=_f(greek.delta) if greek else None,
                    theta=_f(greek.theta) if greek else None,
                    iv=_f(greek.volatility) if greek else None,
                )
            )

    return Cycle(
        symbol=symbol,
        expiration=exp.expiration_date,
        dte=exp.days_to_expiration,
        underlying=underlying,
        legs=tuple(legs),
        fetched_at=datetime.now(UTC),
        expirations=available,
    )
