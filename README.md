# tau

A premium-selling options scanner for the tastytrade API. Screens a universe
of liquid equity/ETF names by IV rank, liquidity, and earnings proximity;
prices short-premium candidates into comparable, ranked trades; and gives
each one a plain-language read on *why* its volatility is elevated before
you sell it. CLI-first, no web UI — a text screen for quick checks and a
Textual TUI for interactive triage.

![The screen](docs/img/screen.svg)

## Why

High IV rank alone doesn't tell you whether premium is free money or fair
payment for a coin flip — a name can be "high IVR" because it's a panic
spike about to mean-revert, or because it's genuinely repricing for a
pending binary event (earnings, an FDA date, litigation). `tau` screens for
the former and flags the latter, then prices what's left into apples-to-apples
trades (annualized return on capital, probability of profit, spread cost as
a share of credit) instead of leaving you to compare raw IV numbers across
different names, strikes, and expirations.

## Where the name comes from

In Black-Scholes, τ is time to expiration. It enters the option price twice —
in the drift term, and in the width term underneath it:

$$d_1 = \frac{\ln(S/K) + (r + \sigma^2/2)\tau}{\sigma\sqrt{\tau}}$$

Set the risk-free rate to zero, which is what `tau` does when it computes
probability of profit, and the two collapse into one: every τ left in the
expression is carried by σ√τ. That single quantity is the distribution. It
sets how wide the underlying is expected to range, and therefore what every
option in the chain costs.

It is also exactly what a premium seller sells. IV rank says σ is rich
against the name's own history, days to expiration is τ, and the credit is
the product of the two — so selling a strangle is selling σ√τ and waiting
for τ to run down. Screen, price, and rank are three views of that one
quantity rather than three separate features.

## Features

- **Screen** — pulls tastytrade market metrics for the whole universe in one
  batch, filters by IV rank floor, liquidity rating, and earnings proximity.
- **Price** — for any candidate, searches every shipped structure over one
  ~45 DTE chain fetch — strangles, verticals, iron condors, jade lizards,
  broken wing butterflies, cash-secured puts — and reports credit,
  breakevens, estimated buying-power reduction, annualized return on
  capital, probability of profit (driftless lognormal, not the 1-delta
  shortcut), and spread cost as a share of the premium at stake. The fetch is
  the only expensive part, so searching six structures costs what searching
  one did.
- **Rank** — prices a whole shortlist concurrently and ranks by any of the
  above, so trades across different names and prices are actually
  comparable. The best structure per name is shown by default; drilling into
  a name lists every variant considered, including the ones that were
  rejected and why.
- **Scan log** — records which strategy definition and which variant produced
  each pick, so an accumulating corpus can eventually answer "how did
  16-delta strangles do against 30-delta jade lizards".
- **Price context** — how far a name's recent move sits outside its own
  normal (z-score against a baseline that excludes the move itself), and
  where it sits in its 52-week range.
- **Catalyst read** — pulls recent headlines for the name and, if an
  Anthropic API key is configured, asks a model to classify why the vol is
  bid: a pending binary event, an already-resolved one, no identifiable
  single-name catalyst, or insufficient signal to say. Biased toward saying
  "not sure" over a false all-clear. Triage, not clearance — there's no
  measured accuracy behind it, so check the name before selling. Without a
  key the headlines are shown unclassified.
- **Opt-in storage** — `--log` writes to a local SQLite database
  (`~/.local/share/tau/tau.sqlite3`, override with `TAU_DATA_DIR`). Off by
  default; `tau` writes nothing to disk unless you ask it to.

## Requirements

- Python 3.12+
- A tastytrade account with API access (personal OAuth grant, read scope is
  enough — `tau` never places orders)
- An Anthropic API key — optional. Without one, the catalyst read still
  fetches and shows headlines; only the classification is skipped.

## Setup

### 1. Install

```bash
git clone https://github.com/jkartiwa/tau-options.git
cd tau-options
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e "."
```

Two optional extras:

```bash
pip install -e ".[dev]"            # pytest, to run the test suite
pip install -e ".[catalyst]"       # anthropic, for the catalyst classification
pip install -e ".[dev,catalyst]"   # both
```

### 2. Get tastytrade credentials

`tau` authenticates with an OAuth2 **personal grant** — a self-issued
credential tied to your own account, not an app other people log into.
**Read scope is enough**, and read scope means `tau` cannot place, modify, or
cancel an order even if it tried.

1. Log in at [my.tastytrade.com](https://my.tastytrade.com).
2. Go to **Manage → API** and open **OAuth Applications**.
3. Create a **personal grant**. Give it a name (`tau` works).
4. Under scopes, select **read** only. Don't grant trade scope.
5. Save. You'll be shown a **client ID**, a **client secret**, and a
   **refresh token**.

Two things worth knowing here:

- **Copy the client secret and refresh token immediately.** They are shown
  once. If you lose them, delete the grant and create a new one.
- **The client ID is not used.** `tau` needs only the secret and the refresh
  token. The refresh token doesn't expire, and the SDK exchanges it for a
  short-lived access token on each run.

### 3. Configure the environment

Copy the template and fill in the two values:

```bash
cp .env.example .env
```

```ini
# .env
TASTY_CLIENT_SECRET=your-client-secret
TASTY_REFRESH_TOKEN=your-refresh-token

# Optional: enables the catalyst classification. Without it, `w` still
# fetches and shows headlines, it just won't classify them.
ANTHROPIC_API_KEY=sk-ant-...

# Optional overrides
# TAU_DATA_DIR=~/.local/share/tau
# TAU_UNIVERSE=/path/to/universe.txt
```

`.env` is gitignored. Never commit it.

Real environment variables take precedence — `tau` loads `.env` but will not
override a variable already set in your shell. If a value looks like it isn't
taking effect, check for a stale export:

```bash
echo $TASTY_CLIENT_SECRET     # empty is what you want if you rely on .env
```

You can skip `.env` entirely and export the variables instead, which is the
better option on a shared machine or in CI:

```bash
export TASTY_CLIENT_SECRET=...
export TASTY_REFRESH_TOKEN=...
```

### 4. Verify

```bash
tau scan --top 5
```

A table of five symbols means the credentials work. An error mentioning
`TASTY_CLIENT_SECRET / TASTY_REFRESH_TOKEN not set` means neither `.env` nor
the shell supplied them. Market metrics are precomputed server-side, so this
works outside market hours.

## Usage

```bash
tau                           # interactive TUI (default)
tau tui                       # same, explicit
tau scan                      # ranked screen, text output, default filters
tau scan --min-ivr 40         # tighter IV rank floor
tau scan --all                # every symbol, with exclusion reasons
tau scan --days 0             # keep symbols with upcoming earnings
tau scan --universe PATH      # custom universe file, one symbol per line

tau strategies                # the structures tau searches for
tau rank --top 8              # price the shortlist, best structure per name
tau rank --strategy iron-condor --strategy vertical-put
tau variants SPY              # one name's whole search, rejections included
```

`tau scan` flags: `--min-ivr` (default 30), `--min-liquidity` (1-4 scale,
default 3), `--days` (earnings exclusion window, default 45; 0 disables),
`--top N`, `--all`, `--universe PATH`, `--log`.

`tau rank` takes the same screen filters plus `--strategy NAME` (repeatable,
defaults to all), `--dte` (default 45), `--top N` (default 15 — each row
costs a chain fetch), and `--log`.

### TUI

One metrics pull feeds everything — filtering, sorting, and pricing all
happen in memory with no refetch until you ask for one.

| Key | Action |
|---|---|
| `[` / `]` | move the IV rank floor down / up |
| `l` | cycle liquidity filter |
| `e` | cycle earnings filter |
| `s` | re-sort |
| `x` | toggle excluded view (shows exclusion reasons) |
| `c` / Enter | price the highlighted name's cycle, show the best structure |
| `w` | catalyst read for the highlighted name |
| `p` | price the whole current shortlist, switch to ranked view |
| `v` / Enter | every variant considered on the highlighted name |
| `R` | force a re-price (rank view) |
| `space` | star a name (session-only) |
| `r` | refresh from the API |
| `esc` | back one view |
| `q` | quit |

Proposals are cached per symbol, so leaving a view with `esc` and coming back
is instant — only `r`/`R` force a refetch. A name inspected with `c` is
already fully searched, so ranking it afterwards costs no second fetch.

**`c` — the chain.** Term structure, price position, and the best structure
found on the cycle: its legs, credit, breakevens, and
breakeven-vs-expected-move.

![Detail pane with a priced chain](docs/img/detail.svg)

**`w` — why vol is bid.** Price context plus the catalyst read. Here SMH
classifies as `no_idiosyncratic`: a diversified sector ETF repricing with the
chip complex, no single-name binary to sell into.

![Catalyst read](docs/img/catalyst.svg)

**`p` — the rank view.** The whole shortlist priced concurrently and sorted
by annualized return on capital, so names at different prices and
expirations are comparable.

![Ranked proposals](docs/img/rank.svg)

**`v` / Enter — the whole search.** Every variant of every strategy on that
name, ranked, with the rejected ones greyed and carrying the constraint they
broke. "No jade lizard here today, because the credit doesn't cover the call
spread" is information; a missing row is not. Here 16 of 44 variants passed —
every broken wing died on spread cost, since a three-legged structure on a
$91 name crosses more market than the premium is worth.

![Every variant considered on one name](docs/img/variants.svg)

**`x` — what was excluded, and why.** Every symbol the screen dropped, with
the reason, so a filter that's too tight is visible rather than silent.

![Excluded view](docs/img/excluded.svg)

## Structures and parameters

A structure is data, not code. `src/tau/strategies/` holds one module per
strategy — a flat list of legs, references between them, and constraints on
what the pricing has to come out to:

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

Any selector value may be a list, which turns a definition into a search: a
scalar is simply a one-element search, so there is one code path. The twelve
combinations above are all built and priced off the same chain fetch.

Six ship: `strangle`, `vertical-put`, `iron-condor`, `jade-lizard`,
`broken-wing-butterfly`, `cash-secured-put`. `tau strategies` prints them.

**The central claim is that composition is the right model for legs and the
wrong model for math.** An iron condor is "two verticals" in its leg list and
in nothing else. Margin does not compose — two verticals charged separately
is 2× the width, a condor is charged 1× because only one side can lose — so
an engine that summed components would report roughly half the true return on
every four-legger. Probabilities do not compose either; it is the same
underlying variable in both spreads.

So the universal representation is the **payoff function**. Given a signed leg
list and quoted prices, breakevens, extrema, buying power, and probability of
profit are all derived generically in `payoff.py`. The payoff is
piecewise-linear with kinks exactly at the strikes, so everything is solved
analytically rather than sampled. Nothing in the engine knows what family a
structure belongs to.

That is also why a jade lizard's defining property is a constraint rather
than a shape. It is not "short put plus call credit spread" — it is that
*plus* the credit covering the call spread's width, so there is no upside
risk at all. That is a pricing outcome, and on a given day the requested
deltas may simply not produce it. `Require("worst_loss_up", "<=", 0)` says so
directly, which is why the constraint vocabulary needs no expression
language.

Scan-level parameters are constants rather than flags:

| Parameter | Value | Constant |
|---|---|---|
| Target days to expiration | 45 (`--dte` overrides) | `chain.TARGET_DTE` |
| Expirations considered | monthlies only | `chain.MONTHLY_EXPIRATION_TYPE` |
| Strike window | ±2.5σ, max 80 per side, nearest 60 contiguous | `chain.SIGMA_SPAN`, `MAX_STRIKES_PER_SIDE`, `UNSTRIDED_CORE` |
| Max spread cost | 25% of premium at stake | `strategies.defaults.MAX_SPREAD_COST` |
| Max variants per strategy | 64, fails loudly at import | `strategy.MAX_VARIANTS` |
| Margin estimate | max(20% spot − OTM + premium, 10% strike + premium, $50) per contract | `payoff.OTM_PERCENT`, `STRIKE_PERCENT`, `MIN_PER_CONTRACT` |

Monthlies-only means a symbol with no monthly near 45 DTE has no usable
cycle at all, rather than quietly falling back to a weekly with different
liquidity.

**Two things worth knowing about the numbers.** Return is `max_profit / bpr`,
not `credit / bpr`: on a broken wing the best case sits at a strike well above
the credit taken in, and the structure can legitimately price as a debit.
And every shipped strategy constrains `spread_cost`, because a four-legger
crosses four markets and would otherwise win every ranking on fills that
never happen.

**Buying power is an estimate**, not a broker quote — the read-only grant
cannot dry-run an order. It reduces correctly to the naked formula on a
strangle and to width-minus-credit on a condor, but check it against your
broker's own figure the first time you trade a new structure family.

**Calendars and diagonals are not supported.** Payoff-at-expiry is intrinsic
arithmetic, and a back leg still holding extrinsic value needs a pricing
model instead. Single expiration only.

**Not surfaced:** ex-dividend dates. An ex-div inside the trade window is
real early-assignment risk on the short call, and `tau` will not warn you.
Check it yourself.

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

`d` is evaluated once at each breakeven, $B_{\text{up}}$ and
$B_{\text{low}}$. $N(d(B_{\text{up}}))$ is the probability of finishing
below the upper breakeven and $N(d(B_{\text{low}}))$ of finishing below the
lower one, so the difference is the probability of landing between them.
That two-term form is for a structure with breakevens on both sides — a
strangle, straddle, or condor. A one-sided structure such as a cash-secured
put keeps a single term, and a broken wing with four breakevens sums two
intervals. `tau` does not special-case any of them: it reads the profitable
regions straight off the payoff function and integrates over each.

Two deliberate choices. It uses the **breakevens**, not the strikes — the
credit pushes the breakevens further out than the strikes, so the common
`1 − Δ` shortcut understates every proposal's real odds. And it takes the
chain's own at-the-money implied volatility rather than a fixed-tenor number,
so σ and τ refer to the same expiration.

The usual caveat applies: a lognormal has thin tails and real equities gap.
Treat probability of profit as a way to compare proposals, not as a forecast.

## Testing

```bash
pytest
```

Tests cover the pure filter/rank/pricing logic and TUI behavior without
hitting the live API.

## Disclaimer

This is a personal research tool, not financial advice, and nothing it
outputs is a recommendation to trade. Selling options carries unlimited risk;
short strangles can lose far more than the credit received.

Specific things not to trust blindly: buying-power reduction is an estimate
from a standard margin formula, not a broker quote, and your actual
requirement will differ. Probability of profit assumes a driftless lognormal,
which real markets are not. Quotes are mid-based and can be stale, especially
outside market hours. The catalyst read is a language model's opinion of
recent headlines with no measured accuracy — it can and will miss events.

Verify every number against your broker before placing a trade. Provided as
is, without warranty — see [LICENSE](LICENSE).

## License

MIT — see [LICENSE](LICENSE).
