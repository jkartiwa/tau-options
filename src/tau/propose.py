"""Turning candidates into comparable trades.

A screen that ranks by IV rank answers "where is vol expensive". A premium
seller needs "which of these pays best for the capital and risk it ties up",
and those are different orderings. Credit alone cannot answer it — a $19
credit on a $560 underlying and a $0.93 credit on a $42 one are not
comparable numbers until both are divided by the capital they consume.

So every proposal carries normalized figures: return on capital, the same
annualized, probability of profit computed from the actual breakevens, and
the bid-ask cost as a share of the credit. Those are comparable across the
whole universe, which is what makes ranking meaningful.

Buying power is an *estimate* from the standard naked-option margin formula,
not a broker quote — the read-only grant cannot dry-run an order. It is
labelled as an estimate everywhere it surfaces.
"""

import asyncio
from dataclasses import dataclass

from tastytrade import Session

from tau import chain as chain_mod
from tau.chain import Cycle, Strangle
from tau.payoff import (
    CONTRACT_MULTIPLIER,
    DAYS_PER_YEAR,
    naked_side_requirement,
    pop_between,
)
from tau.screen import Candidate

# Concurrency for batch pricing: each symbol's fetch_cycle makes two REST
# calls (chain lookup, streamer token) before it ever opens a DXLink socket,
# so MAX_CONCURRENT candidates in flight means up to 2x that many REST
# requests landing in the same instant. 6 concurrent pipelines was enough to
# 429 the REST API live; STAGGER_SECONDS spreads their starts so the burst
# never fully lands at once, and exponential backoff below absorbs whatever
# still slips through, so the cap can go back to its original size.
MAX_CONCURRENT = 6
STAGGER_SECONDS = 0.25
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BASE_BACKOFF = 1.0


def _is_rate_limited(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "too many requests" in text


def _clean_error(exc: Exception) -> str:
    """The SDK dumps the raw response body into the exception message when
    it can't parse an error as JSON (tastytrade/utils.py's validate_response)
    — a 429 from a rate-limiting proxy comes back as an HTML page, and that
    HTML would otherwise leak straight into the rank view's detail pane."""
    if _is_rate_limited(exc):
        return "rate limited (429) — too many concurrent requests"
    text = str(exc).strip()
    if text.startswith("<"):
        return f"{type(exc).__name__}: unreadable error response"
    return text


# The margin formula and the lognormal probability both live in payoff.py now,
# where the generic engine needs them, and are re-exported here so this module
# keeps its vocabulary. One copy of the margin arithmetic, not two.


def strangle_bpr(spot: float, strangle: Strangle) -> float | None:
    """Estimated buying power reduction for one short strangle, in dollars.
    Only one side can be assigned, so the charge is the larger requirement
    plus the premium collected on the other side."""
    if not strangle.complete or spot <= 0:
        return None
    put_req = naked_side_requirement(spot, strangle.put.strike, strangle.put.mid)
    call_req = naked_side_requirement(spot, strangle.call.strike, strangle.call.mid)
    if put_req >= call_req:
        return put_req + strangle.call.mid * CONTRACT_MULTIPLIER
    return call_req + strangle.put.mid * CONTRACT_MULTIPLIER


@dataclass(frozen=True)
class Proposal:
    candidate: Candidate
    cycle: Cycle | None = None
    strangle: Strangle | None = None
    error: str | None = None

    @property
    def symbol(self) -> str:
        return self.candidate.symbol

    @property
    def ok(self) -> bool:
        return (
            self.error is None
            and self.cycle is not None
            and self.strangle is not None
            and self.strangle.complete
        )

    @property
    def credit(self) -> float | None:
        return self.strangle.credit if self.ok else None

    @property
    def bpr(self) -> float | None:
        if not self.ok or self.cycle.underlying is None:
            return None
        return strangle_bpr(self.cycle.underlying, self.strangle)

    @property
    def roc(self) -> float | None:
        """Credit as a fraction of estimated buying power."""
        bpr, credit = self.bpr, self.credit
        if not bpr or credit is None:
            return None
        return (credit * CONTRACT_MULTIPLIER) / bpr

    @property
    def annualized_roc(self) -> float | None:
        """Return on capital scaled to a year. This is the number that makes
        a 40-day trade comparable to a 60-day one, not a promise of repeating
        it eight times."""
        roc = self.roc
        if roc is None or not self.cycle or self.cycle.dte <= 0:
            return None
        return roc * DAYS_PER_YEAR / self.cycle.dte

    @property
    def pop(self) -> float | None:
        if not self.ok or self.cycle.underlying is None:
            return None
        iv = self.cycle.atm_iv
        bes = self.strangle.breakevens
        if iv is None or bes is None:
            return None
        return pop_between(self.cycle.underlying, bes[0], bes[1], iv, self.cycle.dte)

    @property
    def spread_cost(self) -> float | None:
        """Round-trip bid-ask on both legs as a share of the credit. Crossing
        two wide markets can cost more than the edge being sold; the symbol's
        liquidity rating cannot see this because it is not strike-specific."""
        if not self.ok:
            return None
        credit = self.strangle.credit
        if not credit:
            return None
        total = self.strangle.put.spread + self.strangle.call.spread
        return total / credit

    @property
    def be_over_em(self) -> float | None:
        if not self.ok:
            return None
        return chain_mod.be_vs_expected_move(self.cycle, self.strangle)


async def price_candidate(
    session: Session,
    candidate: Candidate,
    target_dte: int = chain_mod.TARGET_DTE,
    target_delta: float = chain_mod.TARGET_DELTA,
) -> Proposal:
    hint = candidate.iv30 / 100 if candidate.iv30 else None
    attempt = 0
    while True:
        try:
            cycle = await chain_mod.fetch_cycle(
                session, candidate.symbol, target_dte=target_dte, iv_hint=hint
            )
            break
        except Exception as exc:
            if _is_rate_limited(exc) and attempt < RATE_LIMIT_RETRIES:
                attempt += 1
                await asyncio.sleep(RATE_LIMIT_BASE_BACKOFF * 2 ** (attempt - 1))
                continue
            return Proposal(candidate, error=_clean_error(exc))
    strangle = chain_mod.build_strangle(cycle, target_delta)
    return Proposal(candidate, cycle, strangle, error=strangle.reason)


async def price_many(
    session: Session,
    candidates: list[Candidate],
    target_dte: int = chain_mod.TARGET_DTE,
    target_delta: float = chain_mod.TARGET_DELTA,
    max_concurrent: int = MAX_CONCURRENT,
    on_done=None,
) -> list[Proposal]:
    """Price a whole shortlist concurrently. One symbol failing never fails
    the batch — it comes back as a Proposal carrying its error."""
    sem = asyncio.Semaphore(max_concurrent)

    async def one(candidate: Candidate, start_delay: float) -> Proposal:
        await asyncio.sleep(start_delay)
        async with sem:
            proposal = await price_candidate(
                session, candidate, target_dte, target_delta
            )
        if on_done is not None:
            on_done(proposal)
        return proposal

    return list(
        await asyncio.gather(
            *(
                one(c, (i % max_concurrent) * STAGGER_SECONDS)
                for i, c in enumerate(candidates)
            )
        )
    )


def rank_proposals(proposals: list[Proposal], key: str = "annualized_roc"):
    """Priced proposals first, ordered by the chosen metric descending;
    unpriced ones keep their place at the back rather than vanishing."""
    def sort_key(p: Proposal):
        value = getattr(p, key, None)
        return (not p.ok, -(value or 0), p.symbol)

    return sorted(proposals, key=sort_key)
