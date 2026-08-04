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

A proposal is now every structure the selected strategies could build on one
cycle, not a single hardcoded strangle. One chain fetch per symbol feeds all
of them — everything after the fetch is in-memory arithmetic, so searching six
strategies costs what searching one did.

Buying power is an *estimate* from the standard naked-option margin formula,
not a broker quote — the read-only grant cannot dry-run an order. It is
labelled as an estimate everywhere it surfaces.
"""

import asyncio
from dataclasses import dataclass

from tastytrade import Session

from tau import build as build_mod
from tau import chain as chain_mod
from tau.build import Structure
from tau.chain import Cycle
from tau.payoff import (  # noqa: F401 — re-exported; one copy of this arithmetic
    CONTRACT_MULTIPLIER,
    DAYS_PER_YEAR,
    naked_side_requirement,
    pop_between,
)
from tau.screen import Candidate
from tau.strategies import ALL as ALL_STRATEGIES
from tau.strategy import Strategy

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

# The yardstick for comparing structures of different families against each
# other. A strategy's own `rank` decides which of *its* variants is the one
# worth showing; picking between a jade lizard and an iron condor needs a
# single metric both are measured on, and return per day of capital tied up is
# the one this tool is built around.
CROSS_STRATEGY_METRIC = "annualized_roc"


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


@dataclass(frozen=True)
class Proposal:
    """One symbol's chain, and every structure the selected strategies found
    on it. `best` is the trade to do; `structures` is the whole search, kept
    so the drill-in can show what was rejected and why."""

    candidate: Candidate
    cycle: Cycle | None = None
    structures: tuple[Structure, ...] = ()
    error: str | None = None

    @property
    def symbol(self) -> str:
        return self.candidate.symbol

    @property
    def best(self) -> Structure | None:
        """The trade to do on this name, chosen in two stages.

        Each strategy picks its own winner by its own `rank` metric — that is
        what the field is for, and a strategy may legitimately want its widest
        variant rather than its highest-returning one. Those winners then
        compete on one common metric, because a lizard and a condor cannot be
        compared on a yardstick only one of them declared.
        """
        by_strategy: dict[str, list[Structure]] = {}
        for structure in self.structures:
            by_strategy.setdefault(structure.strategy.name, []).append(structure)
        winners = [
            winner
            for group in by_strategy.values()
            if (winner := build_mod.best(group)) is not None
        ]
        comparable = [
            w for w in winners if w.metric(CROSS_STRATEGY_METRIC) is not None
        ]
        if not comparable:
            return None
        return max(comparable, key=lambda s: s.metric(CROSS_STRATEGY_METRIC))

    @property
    def ok(self) -> bool:
        return self.error is None and self.best is not None

    @property
    def label(self) -> str | None:
        best = self.best
        return None if best is None else best.label

    @property
    def bias(self) -> str | None:
        best = self.best
        return None if best is None else str(best.strategy.bias)

    def variants(self, key: str = CROSS_STRATEGY_METRIC) -> list[Structure]:
        """Everything considered on this name, ranked — passing variants
        first, then constraint failures, then what could not be built."""
        return build_mod.rank(list(self.structures), key)

    def only(self, names: set[str] | None) -> "Proposal":
        """This proposal narrowed to a subset of strategies.

        Turning a strategy off is a view over what was already found, not a
        reason to fetch anything: the chain was searched once and every
        structure it produced is still here. Turning it back on costs nothing
        either.
        """
        if names is None or self.cycle is None:
            return self
        kept = tuple(s for s in self.structures if s.strategy.name in names)
        if len(kept) == len(self.structures):
            return self
        narrowed = Proposal(self.candidate, self.cycle, kept)
        if narrowed.best is not None:
            return narrowed
        reason = (
            "no strategy enabled" if not kept else _no_structure_reason(kept)
        )
        return Proposal(self.candidate, self.cycle, kept, error=reason)

    # Normalized figures, delegated to the winning structure. `rank_proposals`
    # and the rank table read these and never touch the structure itself.

    def _of_best(self, name: str) -> float | None:
        best = self.best
        return None if best is None else getattr(best, name)

    @property
    def credit(self) -> float | None:
        return self._of_best("credit")

    @property
    def bpr(self) -> float | None:
        return self._of_best("bpr")

    @property
    def roc(self) -> float | None:
        return self._of_best("roc")

    @property
    def annualized_roc(self) -> float | None:
        return self._of_best("annualized_roc")

    @property
    def pop(self) -> float | None:
        return self._of_best("pop")

    @property
    def spread_cost(self) -> float | None:
        return self._of_best("spread_cost")

    @property
    def be_over_em(self) -> float | None:
        return self._of_best("be_over_em")

    @property
    def max_profit(self) -> float | None:
        return self._of_best("max_profit")


def _no_structure_reason(structures: tuple[Structure, ...]) -> str:
    """Why a priced cycle yielded no trade. A cycle that built nothing failed
    for a different reason than one where every variant broke a constraint,
    and the second is the interesting case — it is a market condition, not a
    data problem.

    Summarized by which constraint bit and how often, not by quoting the first
    couple of failure messages: a name where nine variants died on spread cost
    and twelve on probability reads as a pure probability problem if the list
    is simply truncated.
    """
    if not structures:
        return "no strategy produced a variant"
    built = [s for s in structures if s.complete]
    if not built:
        reasons = {s.reason for s in structures if s.reason}
        return "; ".join(sorted(reasons)[:2]) or "no variant could be built"
    counts: dict[str, int] = {}
    for structure in built:
        for failure in structure.failures:
            counts[failure.require.metric] = counts.get(failure.require.metric, 0) + 1
    tally = ", ".join(
        f"{metric} ({n})"
        for metric, n in sorted(counts.items(), key=lambda kv: -kv[1])
    )
    return f"all {len(built)} priced variants failed a constraint: {tally}"


def propose_on(
    candidate: Candidate,
    cycle: Cycle,
    strategies: tuple[Strategy, ...] = ALL_STRATEGIES,
) -> Proposal:
    """Every structure the selected strategies find on an already-fetched
    cycle.

    Pure and in-memory: the network cost of a proposal is entirely the chain
    fetch, so a cycle loaded to inspect one name yields the same proposal the
    rank view builds, and searching six strategies costs what searching one
    did.
    """
    structures = tuple(build_mod.evaluate_all(strategies, cycle))
    proposal = Proposal(candidate, cycle, structures)
    if proposal.best is not None:
        return proposal
    return Proposal(
        candidate, cycle, structures, error=_no_structure_reason(structures)
    )


async def price_candidate(
    session: Session,
    candidate: Candidate,
    strategies: tuple[Strategy, ...] = ALL_STRATEGIES,
    target_dte: int = chain_mod.TARGET_DTE,
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
    return propose_on(candidate, cycle, strategies)


async def price_many(
    session: Session,
    candidates: list[Candidate],
    strategies: tuple[Strategy, ...] = ALL_STRATEGIES,
    target_dte: int = chain_mod.TARGET_DTE,
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
                session, candidate, strategies, target_dte
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


def rank_proposals(proposals: list[Proposal], key: str = CROSS_STRATEGY_METRIC):
    """Priced proposals first, ordered by the chosen metric descending;
    unpriced ones keep their place at the back rather than vanishing."""
    def sort_key(p: Proposal):
        value = getattr(p, key, None)
        return (not p.ok, -(value or 0), p.symbol)

    return sorted(proposals, key=sort_key)
