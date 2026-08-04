# User guide

This covers the commands, the keyboard shortcuts, what the numbers mean, and
how to define your own structure. If you have not set up credentials yet, start
with [SETUP.md](SETUP.md).

- [Commands](#commands)
- [TUI keys](#tui-keys)
- [The screen](#the-screen)
- [A priced structure](#a-priced-structure)
- [Rejections](#rejections)
- [Defining your own structure](#defining-your-own-structure)
- [Reference](#reference)
- [The scan log](#the-scan-log)

## Commands

```bash
tau                           # interactive TUI
tau tui                       # same, explicit

tau scan                      # the vol screen, text output
tau strategies                # the structures currently defined
tau rank --top 8              # best structure per name
tau variants SPY              # one name's whole search, rejections included
```

`tau scan` — `--min-ivr` (default 30), `--min-liquidity` (1–4, default 3),
`--days` (earnings exclusion window, default 45; 0 disables), `--top N`,
`--all` (every symbol with exclusion reasons), `--universe PATH`, `--log`.

`tau rank` — the same screen filters, plus `--strategy NAME` (repeatable,
defaults to all), `--dte` (default 45), `--top N` (default 15, since each row
costs a chain fetch), `--log`.

`tau variants SYMBOL` — `--strategy NAME` (repeatable), `--dte`, `--sort METRIC`.

An unknown `--strategy` is an error rather than an empty result.

## TUI keys

One metrics pull feeds every view. Filtering and sorting happen in memory.

| Key | Action |
|---|---|
| `[` / `]` | move the IV rank floor down / up |
| `l` | cycle the liquidity filter |
| `e` | cycle the earnings filter |
| `s` | re-sort |
| `x` | toggle the excluded view, with reasons |
| `c` / Enter | price the highlighted name, show the best structure |
| `w` | price context and catalyst read for the highlighted name |
| `p` | price the whole shortlist, switch to the rank view |
| `v` / Enter | every variant considered on the highlighted name |
| `R` | force a re-price (rank view) |
| `space` | star a name (session only) |
| `r` | refresh from the API |
| `esc` | back one view |
| `q` | quit |

Results are cached per symbol, so leaving a view and coming back is instant.
Only `r` and `R` go back to the API. A name you looked at with `c` has already
been searched in full, so ranking it later costs nothing extra.

## The screen

![The screen](img/screen.svg)

| Column | Meaning |
|---|---|
| `IVR` | IV rank, 0–100, against the name's own trailing year |
| `IVP` | IV percentile, the share of days spent below today's vol |
| `IV/HV` | Implied over realized. Below 1.00 you are selling vol below what the name has delivered, and the column turns yellow |
| `IV30` | 30-day implied vol, percent. A fixed tenor, not the one you are trading |
| `HV30` | 30-day realized vol, percent |
| `LIQ` | tastytrade liquidity rating, 4 best. Symbol-level, not strike-specific |
| `BETA` | Beta to the broad market |
| `ERN` | Days to the next expected report |

`x` shows what was dropped and why, which is worth checking when the pass list
looks thin.

![Excluded](img/excluded.svg)

`w` adds price context and a catalyst read: where the name sits in its 52-week
range, how far its recent move sits outside its own normal, and a
classification of why vol is bid — `pending_binary`, `resolved`,
`no_idiosyncratic` or `insufficient_signal`.

![Why vol is bid](img/catalyst.svg)

Be careful with the last two. `no_idiosyncratic` means the model looked and
found nothing specific to this name. `insufficient_signal` means it could not
tell either way, and it leans toward saying that rather than giving a false
all-clear. Either way this is a starting point for your own checking, not a
verdict. It needs an Anthropic API key; without one you still get the headlines,
just unclassified.

## A priced structure

![The chain](img/detail.svg)

The header gives expiration, days to expiration, spot, at-the-money implied vol
and the expected move.

At-the-money IV is compared against `metrics @exp`, the term IV at the same
expiration, rather than IV30. IV30 is pinned to 30 days while the cycle usually
is not, and on a sloped term structure that mismatch alone shows a large gap.

Expected move follows tastytrade's convention: the ATM straddle blended with
the first and second OTM strangles, weighted 60/30/10. It falls back to
`straddle × 0.85` when the wing strikes are not both priced, and says so.

| Figure | Meaning |
|---|---|
| `credit` | Net premium per share. A debit structure shows `debit` in yellow instead |
| `BE` | Every breakeven. A broken wing has four; a cash-secured put has one |
| `max profit` | Best case in dollars per contract. `∞` when the upside is open |
| `BPR~` | Estimated buying power in dollars. A formula, not a broker quote |
| `ANN` | Annualized return on capital, which makes a 40-day trade comparable to a 60-day one |
| `POP` | Probability of profit at expiry under a driftless lognormal |
| `spread` | Cost of crossing every leg, as a share of the premium at stake |
| `BE/EM` | Nearest breakeven measured in expected moves. Under 1.00 turns yellow |

Return is `max_profit / bpr`, not `credit / bpr`. On a broken wing the best case
sits at a strike above the credit, and the structure can price as a debit.

## Rejections

![Every variant considered](img/variants.svg)

The greyed rows are the ones that failed. `WHY NOT` names the constraint they
broke, and the detail pane shows the numbers behind whichever row you have
highlighted.

| Reason | Meaning |
|---|---|
| `spread_cost` | Crossing the legs costs more than 25% of the premium at stake |
| `pop` | Probability of profit under 50%. Only on broken wings |
| `worst_loss_up` | Credit does not cover the call spread's width. Only on jade lizards |
| `not built` | No legs to price, with the reason in the detail pane |

There are two reasons a variant cannot be built at all.

**ladder too coarse** — a referenced wing landed more than 25% away from the
width it asked for. A 10-wide spread that resolves to 7 wide has different
margin and a different maximum loss, so tau refuses it rather than returning it
under the requested label. On a name with 10-point strikes, every 5-wide
request is correctly refused.

**two legs resolved to the same contract** — the requested strikes collapsed
onto one strike. Variants resolving to identical contracts are also merged into
one row, keeping whichever asked closest to what it got.

In the screenshot above, the rejected broken wings show far higher annualized
returns than anything that passed, and cost 68% to 224% of the premium to
cross. That is what the `spread_cost` constraint is for.

## Defining your own structure

Each structure lives in its own module under `src/tau/strategies/`, and you
register it by adding it to `ALL` in `__init__.py`. Every definition is
validated on import, so a broken one fails immediately instead of halfway
through a scan.

```python
from tau.payoff import OptionType, Side
from tau.strategy import Bias, Delta, LegSpec, Ref, Require, Strategy
from tau.strategies.defaults import MAX_SPREAD_COST

C, P = OptionType.CALL, OptionType.PUT
LONG, SHORT = Side.LONG, Side.SHORT

MY_STRUCTURE = Strategy(
    name="my-structure",
    bias=Bias.BULLISH,
    legs=[
        LegSpec("short_put", type=P, side=SHORT, strike=Delta([0.20, 0.30])),
        LegSpec("long_put",  type=P, side=LONG,
                strike=Ref("short_put", offset=[-5, -10])),
    ],
    require=[Require("spread_cost", "<=", MAX_SPREAD_COST)],
)
```

### Selectors

| Selector | Meaning |
|---|---|
| `Delta(0.16)` | Nearest strike by absolute delta |
| `Moneyness(-0.05)` | 5% below spot |
| `Atm()` | Nearest strike to spot |
| `Ref("short_put", offset=-5)` | Five dollars down the ladder from another leg |
| `Ref("short_put", strikes=-2)` | Two ladder positions down |

`offset` and `strikes` differ on an irregular ladder, which is why both exist.
References resolve in declaration order; a forward reference is a load-time
error.

Any value may be a list, which makes the definition a search. The example above
is four variants: two deltas by two widths. The cap is 64 variants per
strategy, and exceeding it fails at import.

### Constraints

`Require(metric, op, value)`, where `op` is one of `<`, `<=`, `>`, `>=`, `==`
and `value` is a number or another metric name.

Available metrics: `credit`, `net_premium`, `max_profit`, `max_loss`,
`worst_loss_up`, `worst_loss_down`, `bpr`, `roc`, `annualized_roc`, `pop`,
`spread_cost`, `dte`, `breakeven_low`, `breakeven_high`, `be_over_em`,
`worst_off_target`, `leg_count`.

Constrain the pricing outcome rather than the shape where you can. A jade
lizard is defined by its credit covering the call spread's width, which is
`Require("worst_loss_up", "<=", 0)`. On a given day the requested deltas may
not produce it.

Ship a `spread_cost` constraint on everything. A four-legger crosses four
markets and will otherwise win the ranking on fills that never happen.

### Checking it

```bash
tau strategies              # does it parse, and how many variants?
tau variants SPY            # a densely struck name
tau variants MU             # a coarse ladder
pytest
```

Most definition bugs only show up on one of the two.

Watch out for overlapping offset ladders, which let the search build something
other than the structure you named. A broken wing with `near [5, 10]` and
`far [-10, -15]` can come out as a balanced 10/10 fly. Keep the two ranges from
overlapping.

## Reference

Scan parameters are constants rather than flags:

| Parameter | Value | Constant |
|---|---|---|
| Target days to expiration | 45 (`--dte` overrides) | `chain.TARGET_DTE` |
| Expirations | monthlies only | `chain.MONTHLY_EXPIRATION_TYPE` |
| Strike window | ±2.5σ, max 80 per side, nearest 60 contiguous | `chain.SIGMA_SPAN`, `MAX_STRIKES_PER_SIDE`, `UNSTRIDED_CORE` |
| Max spread cost | 25% of premium at stake | `strategies.defaults.MAX_SPREAD_COST` |
| Max referenced-strike miss | 25% of the requested width | `build.MAX_REF_MISS` |
| Max variants per strategy | 64 | `strategy.MAX_VARIANTS` |
| Margin estimate | max(20% spot − OTM + premium, 10% strike + premium, $50) per contract | `payoff.OTM_PERCENT`, `STRIKE_PERCENT`, `MIN_PER_CONTRACT` |

Because only monthlies are considered, a symbol with no monthly expiration near
the target DTE simply has no usable cycle. It will not quietly fall back to a
weekly.

Probability of profit assumes a driftless lognormal:

$$\sigma_\tau = \sigma\sqrt{\tau}, \quad \tau = \text{DTE}/365$$

$$d(K) = \frac{\ln(K/S) + \sigma_\tau^{2}/2}{\sigma_\tau}$$

$$P(\text{profit}) = \sum_{\text{profitable } (a,b)} N\big(d(b)\big) - N\big(d(a)\big)$$

σ is the chain's at-the-money implied volatility, S is spot, N is the standard
normal CDF. The intervals come from the payoff function, so one, two or four
breakevens are handled the same way.

It uses the breakevens rather than the strikes. Credit pushes the breakevens
past the strikes, so the common `1 − Δ` shortcut understates the real odds.

## The scan log

Logging is off by default.

```bash
tau scan --log              # the screen only
tau rank --top 8 --log      # the screen plus the trades
```

SQLite at `~/.local/share/tau/tau.sqlite3`, or wherever `TAU_DATA_DIR` points.

`tau rank --log` writes a `pick` row per symbol: the strategy, the variant,
every leg with its OCC symbol and delta, and the figures. Definitions are
stored once each in `strategy_def`, keyed by a digest, so editing a strategy
does not rewrite the history of picks made under the old version. A symbol that
priced nothing still gets a row with its reason.

```sql
SELECT d.name, k.variant, count(*), round(avg(k.annualized_roc), 2)
FROM pick k JOIN strategy_def d ON d.id = k.strategy_def_id
GROUP BY d.digest, k.variant ORDER BY 3 DESC;
```

Nothing reads this yet and there is no outcome side, so you would have to
record fills and results yourself.
