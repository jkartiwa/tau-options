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
- **Price** — for any candidate, prices a ~45 DTE 16-delta strangle: credit,
  breakevens, estimated buying-power reduction, annualized return on
  capital, probability of profit (driftless lognormal, not the 1-delta
  shortcut), and spread cost as a share of credit.
- **Rank** — prices a whole shortlist concurrently and ranks by any of the
  above, so trades across different names and prices are actually
  comparable. Return on capital prices the trade held to expiration; theta
  over buying power prices what it earns per day, which is the comparison
  that survives closing early.
- **Price context** — how far a name's recent move sits outside its own
  normal (z-score against a baseline that excludes the move itself), and
  where it sits in its 52-week range.
- **Account awareness** — reads current positions and balances (still read
  scope; `tau` cannot place an order) and uses them to answer the two
  questions a screen alone cannot: whether a candidate would *add to* short
  premium you already carry, and what share of net liq the estimated buying
  power represents. Degrades silently: a grant without account access loses
  the position columns and nothing else.
- **Catalyst read** — pulls recent headlines for the name and, if an
  Anthropic API key is configured, asks a model to classify why the vol is
  bid: a pending binary event, an already-resolved one, no identifiable
  single-name catalyst, or insufficient signal to say. Biased toward saying
  "not sure" over a false all-clear. Triage, not clearance — there's no
  measured accuracy behind it, so check the name before selling. Without a
  key the headlines are shown unclassified.
- **Scan log** (opt-in) — `tau scan --log` records the screen to a local
  SQLite database (`~/.local/share/tau/tau.sqlite3`, override with
  `TAU_DATA_DIR`) so picks can be compared against outcomes later. Off by
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
# TAU_ACCOUNT=5WX00000   # which account to read positions from
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
tau tui --delta 0.20 --dte 30 # start on a different wing and tenor
tau scan                      # ranked screen, text output, default filters
tau scan --min-ivr 40         # tighter IV rank floor
tau scan --all                # every symbol, with exclusion reasons
tau scan --days 0             # keep symbols with upcoming earnings
tau scan --universe PATH      # custom universe file, one symbol per line
```

`tau scan` flags: `--min-ivr` (default 30), `--min-liquidity` (1-4 scale,
default 3), `--days` (earnings exclusion window, default 45; 0 disables),
`--top N`, `--all`, `--universe PATH`, `--log`.

### TUI

One metrics pull feeds everything — filtering, sorting, and pricing all
happen in memory with no refetch until you ask for one.

| Key | Action |
|---|---|
| `?` | help — what every column means and which way is good |
| `[` / `]` | move the IV rank floor down / up |
| `l` | cycle liquidity filter |
| `e` | cycle earnings filter |
| `/` | filter by symbol; `esc` clears it |
| `s` | re-sort |
| `x` | toggle excluded view (shows exclusion reasons) |
| `c` / Enter | price the highlighted name's cycle, show the strangle |
| `<` / `>` | previous / next monthly expiration on a loaded chain |
| `d` / `D` | cycle target delta / target DTE |
| `w` | catalyst read for the highlighted name |
| `p` | price the whole current shortlist, switch to ranked view |
| `R` | force a re-price (rank view) |
| `space` | star a name (session-only) |
| `r` | refresh from the API |
| `esc` | back to the screen |
| `q` | quit |

`esc` unwinds one layer at a time — an open search first, then the rank
view — so cancelling a filter never also discards a priced shortlist.

Proposals and chains are cached per symbol, so leaving a view with `esc` and
coming back is instant — only `r`/`R` force a refetch.

**`c` — the chain.** Term structure, price position, and the 16-delta
strangle with credit, breakevens, breakeven-vs-expected-move, decay per day
against the capital it consumes, and the time the quotes behind those numbers
were taken. `<` and `>` walk the same read along the monthly expirations.

![Detail pane with a priced chain](docs/img/detail.svg)

**`w` — why vol is bid.** Price context plus the catalyst read. Here SMH
classifies as `no_idiosyncratic`: a diversified sector ETF repricing with the
chip complex, no single-name binary to sell into.

![Catalyst read](docs/img/catalyst.svg)

**`p` — the rank view.** The whole shortlist priced concurrently and sorted
by annualized return on capital, so names at different prices and
expirations are comparable.

![Ranked proposals](docs/img/rank.svg)

**`x` — what was excluded, and why.** Every symbol the screen dropped, with
the reason, so a filter that's too tight is visible rather than silent.

![Excluded view](docs/img/excluded.svg)

## Structures and parameters

`tau` is a scanner, and the structure is the last step of it. Screening,
pricing, and ranking are all structure-agnostic — return on capital,
probability of profit, and spread cost as a share of credit are defined for
anything that collects a credit against a margin requirement. Adding a
structure means writing two things: a builder that picks its legs off the
chain, and a margin model.

Today one structure is implemented: the **short strangle** — one short call
and one short put, same expiration, naked, undefined risk on both sides.

The two parameters that define the structure — the wing and the tenor — are
adjustable at runtime. `d` and `D` cycle them in the TUI, and `tau tui
--delta 0.20 --dte 30` sets where they start:

| Parameter | Default | Change it with |
|---|---|---|
| Target delta per side | 0.16 | `d`, `--delta` (0.10 / 0.16 / 0.20 / 0.25 / 0.30) |
| Target days to expiration | 45 | `D`, `--dte` (21 / 30 / 45 / 60 / 90) |
| Expiration priced | nearest monthly to target | `<` / `>` on a loaded chain |

Moving the wing costs nothing: the cached cycle already holds the whole
strike window, so the shortlist re-prices at the new delta with no API call.
Moving the tenor is a different chain, so held quotes are dropped rather than
relabelled, and the next `p` or `c` refetches.

The rest is still fixed in code — change them by editing the constant:

| Parameter | Value | Constant |
|---|---|---|
| Expirations considered | monthlies only | `chain.MONTHLY_EXPIRATION_TYPE` |
| Strike window | ±2.5σ, max 26 per side | `chain.SIGMA_SPAN` |
| Margin estimate | max(20% spot − OTM + premium, 10% strike + premium, $50) per contract | `propose.OTM_PERCENT`, `STRIKE_PERCENT`, `MIN_PER_CONTRACT` |
| Sizing ceiling shown against net liq | 5% | `tui.detail.MAX_ALLOCATION` |

Monthlies-only means a symbol with no monthly near 45 DTE has no usable
cycle at all, rather than quietly falling back to a weekly with different
liquidity.

**Next up:** iron condor and cash-secured put, then verticals. Each needs a
little more than a builder — defined-risk structures want a credit-to-width
measure alongside return on capital, and single-sided ones reduce the
probability calculation below from two terms to one. The ranking layer
itself doesn't change.

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
put keeps a single term.

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
