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

A proposal is every structure the selected strategies could build on one
cycle. One chain fetch per symbol feeds all of them; everything after the
fetch is in-memory arithmetic, so searching six strategies costs what
searching one does.

Buying power is the broker's own figure when the account answers (an order
dry-run, which places nothing) and the formula estimate otherwise, labelled
as such either way. Any broker failure falls back to the formula.
"""

import asyncio
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import cached_property

from tastytrade import Session

from tau import broker as broker_mod
from tau import build as build_mod
from tau import chain as chain_mod
from tau.build import Structure
from tau.chain import Cycle
from tau.screen import Candidate
from tau.strategies import ALL as ALL_STRATEGIES
from tau.strategy import Strategy

# Concurrency for batch pricing. Each fetch_cycle makes two REST calls (chain
# lookup, streamer token) before opening a DXLink socket, and six pipelines
# starting at once can draw a 429. STAGGER_SECONDS spreads their starts, and
# exponential backoff in price_candidate absorbs whatever still gets limited.
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

# Per symbol, how many of the formula-ranked structures the broker prices.
# A full search is ~50 variants; pricing them all is ~5x the API load for no
# change in what the rank view shows.
BROKER_BPR_TOP = 10

# Seconds one symbol's broker pull gets, so an unresponsive broker never
# stalls the pipeline. Expiring it counts as a broker failure (see
# `broker.cancel_for_budget`), so a slow broker trips the breaker instead of
# costing the same stall on every symbol in the pass.
BROKER_BPR_BUDGET = 30.0


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

    @cached_property
    def best(self) -> Structure | None:
        """The trade to do on this name, chosen in two stages.

        Each strategy picks its own winner by its own `rank` metric — that is
        what the field is for, and a strategy may legitimately want its widest
        variant rather than its highest-returning one. Those winners then
        compete on one common metric, because a lizard and a condor cannot be
        compared on a yardstick only one of them declared.

        The winners can carry figures from two margin models (the broker
        prices a bounded shortlist); `comparable_on` keeps the final
        comparison to one of them.

        Cached: every figure the rank table shows delegates here, and the
        instance is frozen, so the answer cannot change.
        """
        by_strategy: dict[str, list[Structure]] = {}
        for structure in self.structures:
            by_strategy.setdefault(structure.strategy.name, []).append(structure)
        winners = [
            winner
            for group in by_strategy.values()
            if (winner := build_mod.best(group)) is not None
        ]
        comparable = [w for w in winners if w.metric(CROSS_STRATEGY_METRIC) is not None]
        if not comparable:
            return None
        pool = build_mod.comparable_on(comparable, CROSS_STRATEGY_METRIC)
        return max(pool, key=lambda s: s.metric(CROSS_STRATEGY_METRIC))

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
            "no strategy enabled"
            if not kept
            else _no_structure_reason(self.cycle, kept)
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


def _unbuilt_reasons(structures: tuple[Structure, ...]) -> str:
    """The distinct reasons variants never got as far as being priced, capped
    the way the constraint tally is — two is enough to tell a chain too thin
    to quote from a ladder too coarse to land on, and the whole list would not
    fit the row this prints in."""
    reasons = {s.reason for s in structures if not s.complete and s.reason}
    return "; ".join(sorted(reasons)[:2])


def _no_structure_reason(cycle: Cycle | None, structures: tuple[Structure, ...]) -> str:
    """Why a priced cycle yielded no trade.

    A missing underlying quote is checked first: without spot every risk
    metric fails closed, and the tally would read a dropped feed as a market
    condition. Constraint failures are tallied per metric so the dominant one
    is visible, and unbuilt variants are reported alongside priced ones.
    """
    if cycle is not None and cycle.underlying is None:
        return "no underlying quote"
    if not structures:
        return "no strategy produced a variant"
    built = [s for s in structures if s.complete]
    if not built:
        return _unbuilt_reasons(structures) or "no variant could be built"
    counts = Counter(f.require.metric for s in built for f in s.failures)
    tally = ", ".join(f"{metric} ({n})" for metric, n in counts.most_common())
    reason = f"all {len(built)} priced variants failed a constraint: {tally}"
    unbuilt = len(structures) - len(built)
    if unbuilt:
        reason += (
            f"; {unbuilt} of {len(structures)} never priced: "
            f"{_unbuilt_reasons(structures)}"
        )
    return reason


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
        candidate, cycle, structures, error=_no_structure_reason(cycle, structures)
    )


async def enrich_with_broker_bpr(
    session: Session | None,
    proposal: Proposal,
    top_n: int = BROKER_BPR_TOP,
    budget: float = BROKER_BPR_BUDGET,
) -> Proposal:
    """Attach the broker's dry-run buying-power figure to the proposal's
    top `top_n` tradable structures, ranked by the formula.

    All or nothing: if any shortlisted structure goes unpriced (error,
    missing session, or `budget` seconds running out), the proposal comes
    back unchanged. A partial pull would pick the winner from whichever
    requests happened to return, mixing two margin models in one comparison.
    """
    if session is None or proposal.error is not None or not proposal.structures:
        return proposal
    if broker_mod.dry_runs_disabled():
        return proposal
    try:
        account = await broker_mod.margin_account(session)
    except Exception:
        return proposal
    if account is None:
        return proposal
    shortlist = [s for s in proposal.variants() if s.ok][:top_n]
    if not shortlist:
        return proposal

    priced: dict[int, Structure] = {}

    async def one(structure: Structure) -> None:
        # `broker_bpr_for` gates itself: a rank pass runs several of these
        # batches at once, so the cap on dry-run POSTs in flight has to be
        # shared across them rather than reset per batch.
        try:
            value = await broker_mod.broker_bpr_for(session, account, structure)
        except Exception:
            return
        if value is not None:
            priced[id(structure)] = replace(structure, broker_bpr=value)

    tasks = [asyncio.ensure_future(one(s)) for s in shortlist]
    try:
        _, pending = await asyncio.wait(tasks, timeout=budget)
    except BaseException:
        # Somebody else's cancellation (Ctrl-C, a TUI tearing down its
        # worker). These tasks are ours to clean up, but the broker did not
        # fail — cancel them plainly so `broker_bpr_for` re-raises rather
        # than counting it, and let the cancellation through.
        for task in tasks:
            if not task.done():
                task.cancel()
        raise
    # Out of budget: the broker did not answer in time. `cancel_for_budget`
    # counts that as a failure, which a plain cancel would not, since
    # `CancelledError` is not an `Exception`.
    for task in pending:
        broker_mod.cancel_for_budget(task)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    if len(priced) != len(shortlist):
        return proposal
    structures = tuple(priced.get(id(s), s) for s in proposal.structures)
    return replace(proposal, structures=structures)


async def price_candidate(
    session: Session,
    candidate: Candidate,
    strategies: tuple[Strategy, ...] = ALL_STRATEGIES,
    target_dte: int = chain_mod.TARGET_DTE,
) -> Proposal:
    """Fetch one symbol's cycle, search it, and attach broker buying power.
    Rate limits are retried with backoff; any other error comes back as a
    Proposal carrying it."""
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
    return await enrich_with_broker_bpr(
        session, propose_on(candidate, cycle, strategies)
    )


async def price_many(
    session: Session,
    candidates: list[Candidate],
    strategies: tuple[Strategy, ...] = ALL_STRATEGIES,
    target_dte: int = chain_mod.TARGET_DTE,
    max_concurrent: int = MAX_CONCURRENT,
    on_done: Callable[[Proposal], None] | None = None,
) -> list[Proposal]:
    """Price a whole shortlist concurrently. One symbol failing never fails
    the batch — it comes back as a Proposal carrying its error."""
    sem = asyncio.Semaphore(max_concurrent)

    async def one(candidate: Candidate, start_delay: float) -> Proposal:
        await asyncio.sleep(start_delay)
        async with sem:
            proposal = await price_candidate(session, candidate, strategies, target_dte)
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


def broker_priced_pass(proposals: list[Proposal]) -> bool:
    """Whether every priced proposal in a pass carries a broker figure.

    The question an ordering has to ask before it picks a yardstick. Answered
    across the whole pass rather than per row, because the ordering is what
    the reader compares and one row measured on the other model corrupts the
    comparison rather than just itself.
    """
    priced = [p for p in proposals if p.best is not None]
    return bool(priced) and all(p.best.bpr_source == "broker" for p in priced)


def ordering_value(proposal: Proposal, key: str, on_broker: bool) -> float | None:
    """The figure `proposal` sorts on, given what the rest of its pass got.

    A whole pass priced by the broker orders on the broker's numbers. If any
    symbol lacks them, the whole list orders on the formula: the two models
    can differ by tens of percent per name, so a mixed sort would favour
    whichever names the more generous model measured. Each row still
    displays and labels its own figure.
    """
    best = proposal.best
    if best is None:
        return None
    return best.metric(key) if on_broker else best.on_formula.metric(key)


def rank_proposals(
    proposals: list[Proposal], key: str = CROSS_STRATEGY_METRIC
) -> list[Proposal]:
    """Priced proposals first, ordered by the chosen metric descending;
    unpriced ones keep their place at the back rather than vanishing."""
    on_broker = broker_priced_pass(proposals)

    def sort_key(p: Proposal) -> tuple[bool, float, str]:
        value = ordering_value(p, key, on_broker)
        return (not p.ok, -(value or 0), p.symbol)

    return sorted(proposals, key=sort_key)
