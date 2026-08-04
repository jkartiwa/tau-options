# Design notes

Why `tau` is built the way it is. The [user guide](GUIDE.md) covers using it;
this is the reasoning underneath.

- [Where the name comes from](#where-the-name-comes-from)
- [Composition is right for legs and wrong for math](#composition-is-right-for-legs-and-wrong-for-math)
- [Strategies as data](#strategies-as-data)
- [Scan parameters](#scan-parameters)
- [How probability of profit is computed](#how-probability-of-profit-is-computed)
- [Known limits](#known-limits)

---

## Where the name comes from

In Black-Scholes, τ is time to expiration. It enters the option price twice —
in the drift term, and in the width term underneath it:

$$d_1 = \frac{\ln(S/K) + (r + \sigma^2/2)\tau}{\sigma\sqrt{\tau}}$$

Set the risk-free rate to zero, which is what `tau` does when it computes
probability of profit, and the two collapse into one: every τ left in the
expression is carried by σ√τ. That single quantity is the distribution. It
sets how wide the underlying is expected to range, and therefore what every
option in the chain costs.

It is also exactly what a premium seller sells. IV rank says σ is rich against
the name's own history, days to expiration is τ, and the credit is the product
of the two — so selling a strangle is selling σ√τ and waiting for τ to run
down. Screen, price, and rank are three views of that one quantity rather than
three separate features.

---

## Composition is right for legs and wrong for math

This is the claim the whole engine rests on.

An iron condor is "two verticals" in its leg list and in nothing else:

- **Margin does not compose.** Two verticals charged separately is 2× the
  width. A condor is charged 1×, because only one side can lose. An engine
  that summed its components would report roughly half the true return on
  capital for every four-legged structure.
- **Probability does not compose.** You cannot AND two spreads' probabilities.
  It is the same underlying variable in both.
- **Definitional properties are not structural.** A jade lizard is not "short
  put plus call credit spread". It is that *plus* the credit covering the call
  spread's width, so there is no upside risk at all. That is a pricing
  outcome, not a shape — on a given day the requested deltas may simply not
  produce it, and what got built is a lopsided condor.

**So the universal representation is the payoff function, not the taxonomy.**

Given a signed leg list and quoted prices, `payoff.py` derives everything
generically: breakevens, maximum profit and loss, worst loss on each side of
spot, buying power, profitable regions, probability of profit. The payoff is
piecewise-linear with kinks exactly at the strikes, so all of it is solved
analytically rather than sampled.

Nothing in the engine knows what family a structure belongs to. Adding a
structure adds no math.

What genuinely remains per-strategy is **only strike selection**, and there
composition earns its place as references *between* legs rather than as a
nesting tree. A flat list plus references expresses everything: a strangle has
no references, a vertical one, a condor two, a broken wing two with asymmetric
offsets. A tree buys nothing and forces names for intermediate objects that
have no independent meaning.

---

## Strategies as data

`src/tau/strategies/` holds one module per structure:

```python
JADE_LIZARD = Strategy(
    name="jade-lizard",
    bias=Bias.BULLISH,
    legs=[
        LegSpec("short_put",  type=P, side=SHORT, strike=Delta([0.20, 0.30])),
        LegSpec("short_call", type=C, side=SHORT, strike=Delta([0.20, 0.25])),
        LegSpec("long_call",  type=C, side=LONG,
                strike=Ref("short_call", offset=[5, 10, 15])),
    ],
    require=[
        Require("worst_loss_up", "<=", 0),          # what makes it a lizard
        Require("spread_cost", "<=", MAX_SPREAD_COST),
    ],
)
```

Note what the lizard's defining property is: a **constraint on the pricing
outcome**, not a shape. The payoff engine already exposes `worst_loss_up`, so
the property is expressible as a plain scalar comparison. That is why the
constraint vocabulary needs no expression language — no arithmetic, no
function calls, and no risk of it growing into a bad DSL.

**Any selector value may be a list, which turns a definition into a search.**
A scalar is a one-element search, so there is one code path. The twelve
combinations above are all built and priced off a single chain fetch. For a
broken wing the widths *are* the trade, and which one pays depends on today's
skew, so searching them is more honest than pinning one.

What ships is a starting set, not the feature: `strangle`, `vertical-put`,
`iron-condor`, `jade-lizard`, `broken-wing-butterfly`, `cash-secured-put`.
`tau strategies` prints whatever is currently defined. Adding one is a module
and a line in `ALL` — no engine changes, no math, and it is searched, ranked
and logged with the rest from the next run. The
[user guide](GUIDE.md#defining-your-own-structure) covers the selectors and
constraints.

### Why Python and not a config file

Definitions are Python frozen dataclasses rather than TOML or YAML, which
reverses the obvious call. The deciding question is *how a hand-author learns
the schema*, and no config format answers it without you building the answer:
a validator, hand-written error messages, and a JSON Schema so editors can
autocomplete.

Python answers it for free. Hovering a field shows its docstring, `type=`
autocompletes as an enum, and **typing the metric names as a `Literal` union
makes a typo'd `worst_loss_up` a type error before `tau` is ever run** — which
no config format catches unless you hand-maintain a validator in sync with it.
The standard argument for a config format is untrusted or non-programmer
authors; there are none here, and since definitions ship inside the package,
"editable without touching code" was never on offer anyway.

They ship in-package on purpose: this is a public repo, and what `tau` looked
for is worth being visible.

---

## Scan parameters

Constants rather than flags:

| Parameter | Value | Constant |
|---|---|---|
| Target days to expiration | 45 (`--dte` overrides) | `chain.TARGET_DTE` |
| Expirations considered | monthlies only | `chain.MONTHLY_EXPIRATION_TYPE` |
| Strike window | ±2.5σ, max 80 per side, nearest 60 contiguous | `chain.SIGMA_SPAN`, `MAX_STRIKES_PER_SIDE`, `UNSTRIDED_CORE` |
| Max spread cost | 25% of premium at stake | `strategies.defaults.MAX_SPREAD_COST` |
| Max referenced-strike miss | 25% of the requested width | `build.MAX_REF_MISS` |
| Max variants per strategy | 64, fails loudly at import | `strategy.MAX_VARIANTS` |
| Margin estimate | max(20% spot − OTM + premium, 10% strike + premium, $50) per contract | `payoff.OTM_PERCENT`, `STRIKE_PERCENT`, `MIN_PER_CONTRACT` |

**Monthlies only** means a symbol with no monthly near 45 DTE has no usable
cycle at all, rather than quietly falling back to a weekly with different
liquidity.

**The strike window is the one measured tuning decision.** It is scaled by
expected move rather than a fixed percentage, since a 16-delta wing sits near
one standard deviation on any name. Capping by strike *count* was the original
bug in this codebase: on a densely struck name the window stopped inside the
wings and a 0.38-delta leg came back labelled 16-delta.

Measured live at three budgets, six symbols concurrent: the fetch costs
1.0–1.75s whether it carries 64 legs or 322, and six in flight finish in the
time one takes — connection setup dominates, not leg count. Meanwhile the cap
decides how much ladder exists to build on. On SPY, 45 strikes a side built 33
of 56 variants; 60 built 52; 80 builds all 56. Sparse ladders are identical at
all three, because the sigma window bounds them rather than the count. So the
smaller budget was throttling the densely struck names for nothing.

`UNSTRIDED_CORE` turned out to be the binding constant, not the cap. The
contiguous region has to reach *past* the short strike: at 30 the 16-delta SPY
put sat 40 points out in the strided region, and every 5-wide condor and
vertical was correctly refused for a ladder that only looked coarse because of
the thinning.

### Two things about the return metric

Return is `max_profit / bpr`, not `credit / bpr`. On a broken wing the best
case sits at a strike well above the credit taken in, and the structure can
legitimately price as a debit, where credit-over-capital is meaningless.

And every shipped strategy constrains `spread_cost`, because a four-legger
crosses four markets and would otherwise win every ranking on fills that never
happen. See the [user guide](GUIDE.md#a-session) for what that looks like on
live data.

---

## How probability of profit is computed

This is the one place `tau` leans on Black-Scholes directly. It assumes a
driftless lognormal — no expected return, just diffusion — and asks how much
of the terminal distribution lands in the profitable range:

$$\sigma_\tau = \sigma\sqrt{\tau} \quad \text{where} \quad \tau = \text{DTE}/365$$

$$d(K) = \frac{\ln(K/S) + \sigma_\tau^{2}/2}{\sigma_\tau}$$

$$P(\text{profit}) = N\big(d(B_{\text{up}})\big) - N\big(d(B_{\text{low}})\big)$$

σ is the chain's at-the-money implied volatility, S is spot, and N is the
standard normal CDF. The `σ²/2` term is the median shift that makes the
process driftless in log terms.

`d` is evaluated once at each breakeven, $B_{\text{up}}$ and $B_{\text{low}}$.
$N(d(B_{\text{up}}))$ is the probability of finishing below the upper
breakeven and $N(d(B_{\text{low}}))$ of finishing below the lower one, so the
difference is the probability of landing between them.

That two-term form is for a structure with breakevens on both sides — a
strangle, straddle, or condor. A one-sided structure such as a cash-secured
put keeps a single term, and a broken wing with four breakevens sums two
intervals. **`tau` special-cases none of them**: it reads the profitable
regions straight off the payoff function and integrates over each.

Two deliberate choices. It uses the **breakevens**, not the strikes — the
credit pushes the breakevens further out than the strikes, so the common
`1 − Δ` shortcut understates every proposal's real odds. And it takes the
chain's own at-the-money implied volatility rather than a fixed-tenor number,
so σ and τ refer to the same expiration.

The usual caveat applies: a lognormal has thin tails and real equities gap.
Treat probability of profit as a way to compare proposals, not as a forecast.

---

## Known limits

**Buying power is an estimate** from the standard naked-margin formula, not a
broker quote — the read-only grant cannot dry-run an order. It reduces
correctly to the naked formula on a strangle and to width-minus-credit on a
condor, but every return figure divides by it, and condors and broken wings
rank highest on it. This is the most load-bearing unverified number in the
tool.

**Calendars and diagonals are not supported.** Payoff-at-expiry is intrinsic
arithmetic, and a back leg still holding extrinsic value needs a pricing model
instead. Single expiration only. Architecturally not foreclosed.

**Ex-dividend dates are not surfaced.** An ex-div inside the trade window is
real early-assignment risk on a short call, and `tau` will not warn you. The
metrics pull already carries the dividend fields; nothing reads them yet.

**No position awareness.** The read-only grant can see your positions, so the
list could flag names you are already short premium in. It doesn't.

**The scan log has no outcome side.** `tau rank --log` records which strategy
definition and variant produced each pick, keyed so that editing a strategy
does not rewrite the history of trades chosen under the old version. Nothing
reads it, and there is no fill or result recorded, so the scoreboard it exists
to feed does not exist yet.
