"""Resolving a strategy against a real chain, and evaluating what comes out.

Three things happen here, in order. Selectors resolve to actual quoted
contracts, which is the only genuinely per-strategy step left once the payoff
engine exists. Every combination of selector values is enumerated, since any
selector may carry a list. And each built variant is priced through the payoff
engine and checked against its constraints.

Two rules carry over from the single-structure version and matter more now
that structures have four legs:

A variant missing any leg's quote or greeks is invalid with its reason kept,
never a partial credit — half a structure's credit is a wrong number, not an
imprecise one.

A strike that misses what was asked for by more than its tolerance is
refused, never folded into the label. That was the original bug in this
codebase, where a 0.38-delta leg came back labelled 16-delta, and a referenced
wing landing on the wrong strike is the same failure wearing a different hat:
the spread is narrower than the name says and the margin figure is wrong with
it. Both selectors are gated the same way — see `MAX_DELTA_MISS` and
`MAX_REF_MISS` — so a variant that survives to be priced is one whose label
describes the contracts it holds. Fewer rows on a thin day is the price, and
it is the right one: a row is a comparison, and a mislabelled row is a
comparison against a trade that was never on offer.
"""

from dataclasses import dataclass, replace
from math import inf

from tau.chain import DELTA_TOLERANCE, Cycle, Leg
from tau.payoff import (
    DAYS_PER_YEAR,
    OptionType,
    PayoffLeg,
    bpr,
    breakevens,
    max_loss,
    max_profit,
    net_premium,
    pop_over_intervals,
    profitable_intervals,
    worst_loss_down,
    worst_loss_up,
)
from tau.strategy import Atm, Delta, LegSpec, Moneyness, Ref, Require, Strategy

# How far a referenced wing may land from the strike it asked for, as a
# fraction of the requested offset, before the variant is thrown out. A 10-wide
# spread that resolves to 7 wide is a different trade, and its margin and
# maximum loss are different numbers — better to have no row than a wrong one.
MAX_REF_MISS = 0.25

# How far a delta-selected leg may land from the delta it asked for before the
# variant is thrown out. The same rule as MAX_REF_MISS above, applied to the
# other selector: a variant labelled 16Δ holding a 29.5-delta contract is a
# different trade with a different probability of profit, and the label is the
# thing a trader reads. On a partially quoted chain this is the routine
# outcome rather than the rare one — the far wings are exactly the contracts
# with no resting market, so they are the ones that never quote, and the
# nearest surviving delta can be most of the way to the money.
#
# Gated here rather than in each strategy's `require` tuple on purpose. It
# holds for every strategy including one with no delta leg at all, whose
# `worst_off_target` is None and which a constraint would therefore auto-fail;
# and it refuses before pricing, so no downstream number is ever derived from
# a contract the label misdescribes.
MAX_DELTA_MISS = DELTA_TOLERANCE


@dataclass(frozen=True)
class BuiltLeg:
    """One resolved leg: what was asked for, what the chain actually had."""

    spec: LegSpec
    leg: Leg
    off_target: float | None = None  # delta miss, for delta-selected legs
    strike_miss: float | None = None  # dollars from the requested strike

    @property
    def payoff_leg(self) -> PayoffLeg:
        return PayoffLeg(
            type=self.spec.type,
            side=self.spec.side,
            strike=self.leg.strike,
            mid=self.leg.mid,
            qty=self.spec.qty,
        )


@dataclass(frozen=True)
class ConstraintResult:
    require: Require
    passed: bool
    actual: float | None

    @property
    def reason(self) -> str:
        return self.require.describe(self.actual)


@dataclass(frozen=True)
class Structure:
    """A priced variant of a strategy on one cycle.

    Carries its failures rather than disappearing when it breaks a constraint:
    "no lizard on MU today, worst_loss_up 340 > 0" is information, a missing
    row is not.
    """

    strategy: Strategy
    variant: str
    cycle: Cycle
    legs: tuple[BuiltLeg, ...] = ()
    reason: str | None = None
    failures: tuple[ConstraintResult, ...] = ()
    # The broker's dry-run buying-power figure, when one was obtained. `None`
    # means `bpr` falls back to the in-house formula estimate. Filled by
    # `propose.enrich_with_broker_bpr`, never by the builder.
    broker_bpr: float | None = None

    @property
    def symbol(self) -> str:
        return self.cycle.symbol

    @property
    def label(self) -> str:
        return f"{self.strategy.name} · {self.variant}"

    @property
    def complete(self) -> bool:
        """Built and priced. Says nothing about whether it passed."""
        return self.reason is None and bool(self.legs)

    @property
    def ok(self) -> bool:
        return self.complete and not self.failures

    @property
    def payoff_legs(self) -> tuple[PayoffLeg, ...]:
        return tuple(built.payoff_leg for built in self.legs)

    # --- metrics; every name here appears in the constraint vocabulary ---

    @property
    def net_premium(self) -> float | None:
        return net_premium(self.payoff_legs) if self.complete else None

    @property
    def credit(self) -> float | None:
        """Per share, and only when the structure actually pays. A debit
        structure has no credit, and rendering a negative one as a credit is
        how a broken wing gets read backwards."""
        premium = self.net_premium
        return premium if premium is not None and premium > 0 else None

    @property
    def max_profit(self) -> float | None:
        return max_profit(self.payoff_legs) if self.complete else None

    @property
    def max_loss(self) -> float | None:
        return max_loss(self.payoff_legs) if self.complete else None

    @property
    def worst_loss_up(self) -> float | None:
        if not self.complete or self.cycle.underlying is None:
            return None
        return worst_loss_up(self.payoff_legs, self.cycle.underlying)

    @property
    def worst_loss_down(self) -> float | None:
        if not self.complete or self.cycle.underlying is None:
            return None
        return worst_loss_down(self.payoff_legs, self.cycle.underlying)

    @property
    def bpr(self) -> float | None:
        """Buying power reduction in dollars: the broker's dry-run figure
        when one was obtained, otherwise the formula estimate."""
        if self.broker_bpr is not None:
            return self.broker_bpr
        if not self.complete or self.cycle.underlying is None:
            return None
        return bpr(self.payoff_legs, self.cycle.underlying)

    @property
    def bpr_source(self) -> str:
        """`"broker"` when `bpr` came from the dry-run calculation,
        `"estimate"` when it is the formula. The two are different numbers
        from different models, and the display says which one it is."""
        return "broker" if self.broker_bpr is not None else "estimate"

    @property
    def roc(self) -> float | None:
        """Max profit over capital tied up. Not credit over capital: on a
        broken wing the best case sits at a strike well above the credit, and
        on a debit structure there is no credit to divide."""
        capital, profit = self.bpr, self.max_profit
        if not capital or profit is None or profit == inf:
            return None
        return profit / capital

    @property
    def annualized_roc(self) -> float | None:
        """The number that makes a 40-day trade comparable to a 60-day one,
        not a promise of repeating it eight times."""
        roc = self.roc
        if roc is None or self.cycle.dte <= 0:
            return None
        return roc * DAYS_PER_YEAR / self.cycle.dte

    @property
    def breakevens(self) -> list[float]:
        return breakevens(self.payoff_legs) if self.complete else []

    @property
    def breakeven_low(self) -> float | None:
        bes = self.breakevens
        return bes[0] if bes else None

    @property
    def breakeven_high(self) -> float | None:
        bes = self.breakevens
        return bes[-1] if bes else None

    @property
    def pop(self) -> float | None:
        """Probability of finishing profitable, with each breakeven priced
        under the vol local to it — puts below, calls above — and `atm_iv` as
        the per-boundary fallback. See `pop_over_intervals` for what that
        approximation is and is not."""
        if not self.complete or self.cycle.underlying is None:
            return None
        iv = self.cycle.atm_iv
        if iv is None:
            return None
        return pop_over_intervals(
            profitable_intervals(self.payoff_legs),
            self.cycle.underlying,
            iv,
            self.cycle.dte,
            iv_at=self.cycle.iv_at,
        )

    @property
    def spread_cost(self) -> float | None:
        """Cost of crossing every leg, as a share of the premium at stake.
        A four-legger crosses four markets, and ranked on return alone it
        would win on fills that never happen."""
        if not self.complete:
            return None
        premium = self.net_premium
        if not premium:
            return None
        total = sum(built.leg.spread * built.spec.qty for built in self.legs)
        return total / abs(premium)

    @property
    def be_over_em(self) -> float | None:
        """How far the nearest breakeven sits in expected moves. Under 1.0
        means a single standard deviation reaches it."""
        em = self.cycle.expected_move
        spot = self.cycle.underlying
        bes = self.breakevens
        if not em or spot is None or not bes:
            return None
        return min(abs(spot - be) for be in bes) / em

    @property
    def worst_off_target(self) -> float | None:
        """Worst delta miss across the delta-selected legs. Referenced legs
        are measured in dollars instead and reported by `worst_strike_miss`."""
        misses = [b.off_target for b in self.legs if b.off_target is not None]
        return max(misses) if misses else None

    @property
    def worst_strike_miss(self) -> float | None:
        misses = [b.strike_miss for b in self.legs if b.strike_miss is not None]
        return max(misses) if misses else None

    @property
    def dte(self) -> int:
        return self.cycle.dte

    @property
    def leg_count(self) -> int:
        return sum(built.spec.qty for built in self.legs)

    def metric(self, name: str) -> float | None:
        value = getattr(self, name, None)
        return None if value is None else float(value)


def _usable(cycle: Cycle, option_type: OptionType) -> list[Leg]:
    """Legs of one type that can carry a structure: quoted and with greeks.
    Anything else is dropped rather than defaulted."""
    return sorted(
        (
            leg
            for leg in cycle.legs
            if leg.type is option_type and leg.priced and leg.delta is not None
        ),
        key=lambda leg: leg.strike,
    )


def _nearest_by_strike(legs: list[Leg], target: float) -> Leg:
    return min(legs, key=lambda leg: abs(leg.strike - target))


def _resolve_leg(
    spec: LegSpec, cycle: Cycle, placed: dict[str, BuiltLeg]
) -> BuiltLeg | str:
    """One resolved leg, or a string saying why it could not be."""
    legs = _usable(cycle, spec.type)
    if not legs:
        side = "call" if spec.type is OptionType.CALL else "put"
        return f"no priced {side} leg with greeks"

    selector = spec.strike
    if isinstance(selector, Delta):
        target = abs(float(selector.value))
        chosen = min(legs, key=lambda leg: abs(abs(leg.delta) - target))
        miss = abs(abs(chosen.delta) - target)
        # Rounded before comparing: the miss is a difference of two floats, so
        # a ladder sitting exactly on the tolerance lands either side of it
        # depending on which operands produced it. A 0.15-delta contract
        # against a 0.20 target is exactly on it and computes to
        # 0.05000000000000002, which would otherwise be refused.
        if round(miss, 6) > MAX_DELTA_MISS:
            return (
                f"{spec.id}: asked {target * 100:g}Δ, nearest quoted is "
                f"{abs(chosen.delta) * 100:.1f}Δ — no strike near that delta"
            )
        return BuiltLeg(spec, chosen, off_target=miss)

    if isinstance(selector, (Atm, Moneyness)):
        if cycle.underlying is None:
            return "no underlying price to place the strike against"
        fraction = 0.0 if isinstance(selector, Atm) else float(selector.value)
        target = cycle.underlying * (1 + fraction)
        chosen = _nearest_by_strike(legs, target)
        return BuiltLeg(spec, chosen, strike_miss=abs(chosen.strike - target))

    reference = placed.get(selector.leg)
    if reference is None:
        return f"leg {selector.leg!r} was not built"

    if selector.strikes is not None:
        anchor = min(
            range(len(legs)), key=lambda i: abs(legs[i].strike - reference.leg.strike)
        )
        index = anchor + int(selector.strikes)
        if not 0 <= index < len(legs):
            return (
                f"{spec.id}: {int(selector.strikes):+d} strikes from "
                f"{reference.leg.strike:g} runs off the ladder"
            )
        chosen = legs[index]
        return BuiltLeg(spec, chosen, strike_miss=0.0)

    offset = float(selector.offset)
    target = reference.leg.strike + offset
    chosen = _nearest_by_strike(legs, target)
    achieved = chosen.strike - reference.leg.strike
    miss = abs(chosen.strike - target)
    if offset and abs(achieved - offset) > MAX_REF_MISS * abs(offset):
        return (
            f"{spec.id}: asked {offset:+g} from {reference.leg.strike:g}, "
            f"nearest strike is {achieved:+g} — ladder too coarse"
        )
    if chosen.strike == reference.leg.strike:
        return f"{spec.id}: resolved onto {selector.leg!r}, zero width"
    return BuiltLeg(spec, chosen, strike_miss=miss)


def _check(structure: Structure) -> tuple[ConstraintResult, ...]:
    results = []
    for rule in structure.strategy.require:
        actual = structure.metric(rule.metric)
        limit = (
            structure.metric(rule.value)
            if isinstance(rule.value, str)
            else float(rule.value)
        )
        passed = actual is not None and limit is not None and _compare(actual, rule.op, limit)
        if not passed:
            results.append(ConstraintResult(rule, False, actual))
    return tuple(results)


def _compare(actual: float, op: str, limit: float) -> bool:
    if op == "<":
        return actual < limit
    if op == "<=":
        return actual <= limit
    if op == ">":
        return actual > limit
    if op == ">=":
        return actual >= limit
    return actual == limit


def build(
    strategy: Strategy, variant: str, specs: tuple[LegSpec, ...], cycle: Cycle
) -> Structure:
    """Resolve one variant's leg specs against a cycle and price it."""
    placed: dict[str, BuiltLeg] = {}
    for spec in specs:
        resolved = _resolve_leg(spec, cycle, placed)
        if isinstance(resolved, str):
            return Structure(strategy, variant, cycle, reason=resolved)
        placed[spec.id] = resolved

    strikes_used = [(b.leg.strike, b.spec.type) for b in placed.values()]
    if len(set(strikes_used)) != len(strikes_used):
        return Structure(
            strategy, variant, cycle, reason="two legs resolved to the same contract"
        )

    structure = Structure(strategy, variant, cycle, legs=tuple(placed.values()))
    return replace(structure, failures=_check(structure))


def _asked_miss(structure: Structure) -> tuple[float, float]:
    """How far a variant's label sits from the contracts it actually got.

    Two dimensions, ordered rather than summed: a delta miss is in delta and a
    strike miss is in dollars, and there is no exchange rate between them.
    Delta leads because it is what the label is named for on every shipped
    strategy, and because `MAX_REF_MISS` already holds the strike miss
    proportional to the width that was asked for. `None` means the variant has
    no leg selected that way, which is no miss at all rather than a large one.
    """
    return (structure.worst_off_target or 0.0, structure.worst_strike_miss or 0.0)


def _contracts(structure: Structure) -> tuple:
    return tuple(
        (b.leg.occ, b.spec.side, b.spec.qty) for b in structure.legs
    )


def evaluate(strategy: Strategy, cycle: Cycle) -> list[Structure]:
    """Every variant of one strategy on one cycle, priced and checked.
    Failures are kept, with their reasons.

    Variants that resolve to the same contracts are collapsed to one, keeping
    whichever asked for closest to what it got — measured on the delta it
    asked for as well as the strike, since a coarse ladder collapses a delta
    ladder onto one contract exactly as readily as it collapses two widths. A
    coarse ladder maps several requested offsets onto a single strike, and
    showing a 25-wide wing and a 20-wide wing as two rows when they are the
    same three contracts is the label-that-lies problem again — two rows, one
    trade, and one of the labels wrong.
    """
    seen: dict[tuple, int] = {}
    out: list[Structure] = []
    for variant, specs in strategy.variants():
        structure = build(strategy, variant, specs, cycle)
        if not structure.complete:
            out.append(structure)
            continue
        key = _contracts(structure)
        if key not in seen:
            seen[key] = len(out)
            out.append(structure)
            continue
        kept = out[seen[key]]
        if _asked_miss(structure) < _asked_miss(kept):
            out[seen[key]] = structure
    return out


def evaluate_all(strategies, cycle: Cycle) -> list[Structure]:
    """Every variant of every strategy over a single cycle.

    One chain fetch per symbol, all strategies evaluated over it — everything
    after the fetch is in-memory arithmetic, so variant enumeration is free by
    the same argument. Do not let this become a scan per strategy.
    """
    return [s for strategy in strategies for s in evaluate(strategy, cycle)]


# Metrics computed from the buying-power figure. Two structures whose `bpr`
# came from different margin models are not comparable on these: the broker's
# portfolio margin ran 30% above the naked-margin formula on MU and 8% below
# it on AAPL, which is enough to hand the win to whichever structure happened
# to be measured on the more generous model. Metrics that never read `bpr`
# are unaffected and compare across sources as they always did.
MODEL_SENSITIVE_METRICS = frozenset({"bpr", "roc", "annualized_roc"})


def comparable_on(structures: list[Structure], key: str) -> list[Structure]:
    """`structures` narrowed to one margin model when `key` depends on which
    model produced it.

    The broker-priced structures when there are any, everything otherwise —
    so a formula estimate can never beat a broker figure on a comparison that
    reads buying power, and a run with no broker figures at all ranks exactly
    as it did before the dry-run existed.
    """
    if key not in MODEL_SENSITIVE_METRICS:
        return structures
    priced = [s for s in structures if s.bpr_source == "broker"]
    return priced or structures


def best(structures: list[Structure]) -> Structure | None:
    """The highest-ranked passing variant, by each structure's own rank
    metric. Structures that failed a constraint never win."""
    passing = [s for s in structures if s.ok and s.metric(s.strategy.rank) is not None]
    if not passing:
        return None
    pool = comparable_on(passing, passing[0].strategy.rank)
    return max(pool, key=lambda s: s.metric(s.strategy.rank))


def rank(structures: list[Structure], key: str = "annualized_roc") -> list[Structure]:
    """Passing variants first, ordered by the chosen metric descending;
    failures and unbuildable variants keep their place at the back rather
    than vanishing."""

    def sort_key(structure: Structure):
        value = structure.metric(key) if structure.complete else None
        return (not structure.ok, -(value or 0), structure.symbol, structure.variant)

    return sorted(structures, key=sort_key)
