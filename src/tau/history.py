"""Daily price history, and the two questions a premium seller asks of it.

The screen answers "is vol rich". It cannot answer "is this name in the middle
of a repricing", and IV rank alone can't tell a panic spike that mean-reverts
from a regime change that keeps going. Both look like high IVR. This module
supplies the price-side evidence: how far the recent move sits outside the
name's own normal, and where price sits in its yearly range.

Bars come from the dxfeed candle stream on the session already in hand, so
this costs a websocket round trip and no new dependency.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import log, sqrt
from statistics import StatisticsError, stdev

from tastytrade import DXLinkStreamer, Session
from tastytrade.dxfeed import Candle

LOOKBACK_DAYS = 420  # calendar days; ~an extra month over 52 weeks of trading
MOVE_WINDOW = 20  # trading days in the "recent move" window
BASELINE_DAYS = 120  # trading days of prior vol the move is measured against
CANDLE_TIMEOUT = 20.0
TRADING_DAYS_PER_YEAR = 252

# Beyond this many standard deviations the recent move stops looking like
# noise around a level and starts looking like a repricing.
STRETCHED_Z = 2.0


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class History:
    """Daily bars for one symbol, newest last."""

    symbol: str
    bars: tuple[Bar, ...]
    fetched_at: datetime

    @property
    def last(self) -> float | None:
        return self.bars[-1].close if self.bars else None

    @property
    def year(self) -> tuple[Bar, ...]:
        """The trailing 52 weeks of bars."""
        if not self.bars:
            return ()
        cutoff = self.bars[-1].day - timedelta(days=365)
        return tuple(b for b in self.bars if b.day >= cutoff)

    @property
    def high_52w(self) -> float | None:
        year = self.year
        return max(b.high for b in year) if year else None

    @property
    def low_52w(self) -> float | None:
        year = self.year
        return min(b.low for b in year) if year else None

    @property
    def range_position(self) -> float | None:
        """Where the last close sits in the 52-week range, 0 (low) to 1 (high).
        A strangle near either edge is not the symmetric bet it looks like."""
        last, hi, lo = self.last, self.high_52w, self.low_52w
        if last is None or hi is None or lo is None or hi <= lo:
            return None
        return (last - lo) / (hi - lo)

    @property
    def move(self) -> float | None:
        """Return over the last MOVE_WINDOW trading days."""
        if len(self.bars) < MOVE_WINDOW + 1:
            return None
        prior = self.bars[-(MOVE_WINDOW + 1)].close
        last = self.bars[-1].close
        if prior <= 0:
            return None
        return last / prior - 1

    @property
    def baseline_vol(self) -> float | None:
        """Daily return stdev over the window *preceding* the recent move.

        Deliberately excludes the move itself. A violent repricing inflates
        trailing volatility, and measuring the move against a denominator it
        just widened is how a three-sigma break gets reported as one sigma."""
        window = self.bars[-(MOVE_WINDOW + BASELINE_DAYS + 1) : -MOVE_WINDOW]
        if len(window) < 30:
            return None
        rets = [
            log(b.close / a.close)
            for a, b in zip(window, window[1:])
            if a.close > 0 and b.close > 0
        ]
        try:
            sd = stdev(rets)
        except StatisticsError:
            return None
        return sd or None

    @property
    def move_z(self) -> float | None:
        """The recent move in standard deviations of its own prior noise.

        Signed: negative is a selloff. Scaling daily vol by sqrt(time) assumes
        independent returns, which trending markets violate — so read this as
        a flag for a second look, not a probability."""
        move, sd = self.move, self.baseline_vol
        if move is None or sd is None:
            return None
        return move / (sd * sqrt(MOVE_WINDOW))

    @property
    def baseline_vol_annual(self) -> float | None:
        """Baseline daily vol annualized, to sit next to IV30 and HV30."""
        sd = self.baseline_vol
        return None if sd is None else sd * sqrt(TRADING_DAYS_PER_YEAR) * 100

    @property
    def stretched(self) -> bool:
        z = self.move_z
        return z is not None and abs(z) >= STRETCHED_Z


def _bars_from(events) -> tuple[Bar, ...]:
    """Candles arrive as an unordered snapshot with removals mixed in, so they
    are keyed by day and sorted rather than trusted in arrival order."""
    by_day: dict[date, Bar] = {}
    for e in events:
        if e.remove or not e.time:
            continue
        close = float(e.close)
        if close <= 0:  # a bar with no trade carries zeroed prices
            continue
        day = datetime.fromtimestamp(e.time / 1000, UTC).date()
        by_day[day] = Bar(
            day=day,
            open=float(e.open),
            high=float(e.high),
            low=float(e.low),
            close=close,
        )
    return tuple(by_day[d] for d in sorted(by_day))


async def fetch_history(
    session: Session,
    symbol: str,
    lookback_days: int = LOOKBACK_DAYS,
) -> History:
    """Daily bars for one symbol.

    Candles are an indexed event delivered as a snapshot, not one event per
    subscribed symbol, so this drains until the feed flags the end rather than
    waiting for a known set to fill the way the chain fetch does."""
    start = datetime.now(UTC) - timedelta(days=lookback_days)
    events: list[Candle] = []
    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe_candle([symbol], "1d", start_time=start)
        try:
            async with asyncio.timeout(CANDLE_TIMEOUT):
                while True:
                    event = await streamer.get_event(Candle)
                    events.append(event)
                    if event.snapshot_end:
                        break
        except TimeoutError:
            pass  # partial history still answers the range question
    return History(
        symbol=symbol,
        bars=_bars_from(events),
        fetched_at=datetime.now(UTC),
    )
