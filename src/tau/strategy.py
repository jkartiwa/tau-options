"""What a strategy *is*, as data.

The payoff engine made every derived number generic, so the only thing left
that differs between a strangle and a broken wing butterfly is which strikes
to pick. That is what a `Strategy` describes: a flat list of leg specs, with
references between them, plus scalar constraints on the pricing outcome.

A flat list plus references expresses every structure worth trading — a
strangle has no references, a vertical one, a condor two, a broken wing two
with asymmetric offsets. A nesting tree buys nothing and forces names for
intermediate objects that have no independent meaning.

Definitions are Python rather than a config format, and the schema *is* the
dataclass: hovering a field shows its docstring, `type=` autocompletes as an
enum, and a typo'd metric name in a `Require` is caught by the type checker
as a bad `Literal` before tau ever runs. `__post_init__` covers what types
cannot express — unique leg ids, references resolving backwards, the variant
cap. `asdict()` still yields a serializable identity for the scan log.

Any selector value may be a list, which makes it a search rather than a
point: a scalar is simply a one-element search, so there is one code path.
For a broken wing the widths *are* the trade, and which width pays depends on
today's skew.
"""

from dataclasses import dataclass
from enum import StrEnum
from itertools import product
from typing import Literal, get_args

from tau.payoff import OptionType, Side

# Every metric a constraint may name, and the only names `rank` accepts. This
# is a Literal rather than a plain str so a typo is a type error at author
# time, which is the main thing a config format could not have given us.
Metric = Literal[
    "credit",
    "net_premium",
    "max_profit",
    "max_loss",
    "worst_loss_up",
    "worst_loss_down",
    "bpr",
    "roc",
    "annualized_roc",
    "pop",
    "spread_cost",
    "dte",
    "breakeven_low",
    "breakeven_high",
    "be_over_em",
    "worst_off_target",
    "leg_count",
]

METRICS: frozenset[str] = frozenset(get_args(Metric))

Op = Literal["<", "<=", ">", ">=", "=="]

OPS: frozenset[str] = frozenset(get_args(Op))

# A three-selector strategy with careless lists multiplies fast. The cap fails
# loudly at load time rather than silently truncating the search.
MAX_VARIANTS = 64


class Bias(StrEnum):
    """Directional lean of a structure. Displayed beside the name's own move
    read, never filtered on — the judgement stays with the trader."""

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


def _as_list(value):
    return list(value) if isinstance(value, (list, tuple)) else [value]


@dataclass(frozen=True)
class Delta:
    """Nearest available strike by absolute delta."""

    value: float | list[float]

    def variants(self) -> list["Delta"]:
        return [Delta(v) for v in _as_list(self.value)]

    def label(self) -> str:
        return f"{abs(float(self.value)) * 100:g}Δ"

    def describe(self) -> str:
        return "/".join(f"{abs(float(v)) * 100:g}Δ" for v in _as_list(self.value))


@dataclass(frozen=True)
class Moneyness:
    """Fraction away from spot: -0.05 is 5% below, +0.05 is 5% above."""

    value: float | list[float]

    def variants(self) -> list["Moneyness"]:
        return [Moneyness(v) for v in _as_list(self.value)]

    def label(self) -> str:
        return f"{float(self.value) * 100:+g}%"

    def describe(self) -> str:
        return "/".join(f"{float(v) * 100:+g}%" for v in _as_list(self.value))


@dataclass(frozen=True)
class Atm:
    """Nearest strike to spot."""

    def variants(self) -> list["Atm"]:
        return [self]

    def label(self) -> str:
        return "ATM"

    def describe(self) -> str:
        return "ATM"


@dataclass(frozen=True)
class Ref:
    """A strike placed relative to another leg's.

    `offset` moves in dollars, `strikes` moves in ladder positions, and on an
    irregular ladder those are different things — both are needed. Exactly one
    must be given. Positive is up the ladder, so a wing below a short put is
    a negative offset.
    """

    leg: str
    offset: float | list[float] | None = None
    strikes: int | list[int] | None = None

    def variants(self) -> list["Ref"]:
        if self.offset is not None:
            return [Ref(self.leg, offset=v) for v in _as_list(self.offset)]
        return [Ref(self.leg, strikes=v) for v in _as_list(self.strikes)]

    def label(self) -> str:
        if self.offset is not None:
            return f"{float(self.offset):+g}"
        return f"{int(self.strikes):+d}k"

    def describe(self) -> str:
        if self.offset is not None:
            moves = "/".join(f"{float(v):+g}" for v in _as_list(self.offset))
        else:
            moves = "/".join(f"{int(v):+d} strikes" for v in _as_list(self.strikes))
        return f"{moves} from {self.leg}"


Selector = Delta | Moneyness | Atm | Ref


@dataclass(frozen=True)
class LegSpec:
    """A spec for *choosing* a contract.

    Deliberately not named `Leg`: `chain.Leg` already exists and means a
    quoted contract. Two different concepts one word apart is how the
    0.38-delta-labelled-16-delta class of confusion starts.
    """

    id: str
    type: OptionType
    side: Side
    strike: Selector
    qty: int = 1

    def variants(self) -> list["LegSpec"]:
        return [
            LegSpec(self.id, self.type, self.side, selector, self.qty)
            for selector in self.strike.variants()
        ]


@dataclass(frozen=True)
class Require:
    """`metric op value`, where value is a number or another metric name.

    No arithmetic and no function calls: the payoff engine already exposes
    everything a structure's defining property needs, including
    `worst_loss_up`, so the vocabulary never has to grow into a bad DSL.
    """

    metric: Metric
    op: Op
    value: float | Metric

    def describe(self, actual: float | None) -> str:
        shown = "unavailable" if actual is None else f"{actual:g}"
        return f"{self.metric} {shown} not {self.op} {self.value}"


@dataclass(frozen=True)
class Strategy:
    """One structure tau knows how to look for.

    There is deliberately no expiration or target-DTE field. A scan makes one
    chain fetch per symbol and every strategy is evaluated over that single
    cycle, so the tenor is a property of the scan rather than of the strategy;
    a per-strategy target would either multiply the fetches or be quietly
    ignored, and quietly ignored is the worse of the two.
    """

    name: str
    bias: Bias
    legs: tuple[LegSpec, ...] = ()
    require: tuple[Require, ...] = ()
    rank: Metric = "annualized_roc"

    def __post_init__(self) -> None:
        object.__setattr__(self, "legs", tuple(self.legs))
        object.__setattr__(self, "require", tuple(self.require))
        self._validate()

    def _validate(self) -> None:
        where = f"strategy {self.name!r}"
        if not self.legs:
            raise ValueError(f"{where}: no legs")
        if self.rank not in METRICS:
            raise ValueError(f"{where}: unknown rank metric {self.rank!r}")
        seen: set[str] = set()
        for spec in self.legs:
            if spec.id in seen:
                raise ValueError(f"{where}: duplicate leg id {spec.id!r}")
            if spec.qty < 1:
                raise ValueError(f"{where}: leg {spec.id!r} has qty < 1")
            selector = spec.strike
            if isinstance(selector, Ref):
                if (selector.offset is None) == (selector.strikes is None):
                    raise ValueError(
                        f"{where}: leg {spec.id!r} must give exactly one of "
                        "offset or strikes"
                    )
                if selector.leg not in seen:
                    raise ValueError(
                        f"{where}: leg {spec.id!r} references {selector.leg!r}, "
                        "which is not declared before it"
                    )
            seen.add(spec.id)
        for rule in self.require:
            if rule.metric not in METRICS:
                raise ValueError(f"{where}: unknown metric {rule.metric!r}")
            if rule.op not in OPS:
                raise ValueError(f"{where}: unknown operator {rule.op!r}")
            if isinstance(rule.value, str) and rule.value not in METRICS:
                raise ValueError(f"{where}: unknown metric {rule.value!r}")
        if self.variant_count > MAX_VARIANTS:
            raise ValueError(
                f"{where}: {self.variant_count} variants exceeds the "
                f"{MAX_VARIANTS} cap"
            )

    @property
    def variant_count(self) -> int:
        total = 1
        for spec in self.legs:
            total *= len(spec.strike.variants())
        return total

    def variants(self) -> list[tuple[str, tuple[LegSpec, ...]]]:
        """Every combination of selector values, each labelled by its shape —
        `20Δ/25Δ+10`. A strategy with no lists yields exactly one."""
        out = []
        for combo in product(*(spec.variants() for spec in self.legs)):
            out.append((_label(combo), combo))
        return out


def _label(specs: tuple[LegSpec, ...]) -> str:
    parts: list[str] = []
    for spec in specs:
        text = spec.strike.label()
        if isinstance(spec.strike, Ref) and parts:
            parts[-1] += text
        else:
            parts.append(text)
    return "/".join(parts)
