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

In Black-Scholes, τ is time to expiration. It enters the option price in two
places — the drift term, and the width term underneath it:

$$d_1 = \frac{\ln(S/K) + (r + \sigma^2/2)\tau}{\sigma\sqrt{\tau}}$$

Set the risk-free rate to zero, which is what `tau` does when it computes
probability of profit, and the two collapse into one: every τ in the
expression is now carried by σ√τ. That single quantity is the whole
distribution. It sets how wide the underlying is expected to range, and
therefore what every option in the chain costs.

It is also exactly what a premium seller sells. IV rank says σ is rich
against the name's own history; days to expiration is τ; the credit you
collect is the product of the two. Selling a strangle is selling σ√τ and
waiting for τ to run down — the position is short volatility and long the
passage of time, which are the same trade seen from two sides.

So the screen and the pricing are one idea, not two. Screening on IV rank
finds a rich σ. Pricing the chain turns that σ into a credit at a specific
τ. Ranking asks which of those products pays best for the capital it ties
up.

## The math

Probability of profit is the one place `tau` leans on Black-Scholes
directly. It assumes a driftless lognormal — no expected return, just
diffusion — and asks how much of the terminal distribution lands between the
breakevens:

$$\sigma_\tau = \sigma\sqrt{\tau} \quad \text{where} \quad \tau = \text{DTE}/365$$

$$d(K) = \frac{\ln(K/S) + \sigma_\tau^{2}/2}{\sigma_\tau}$$

$$P(\text{profit}) = N\big(d(B_{\text{up}})\big) - N\big(d(B_{\text{low}})\big)$$

σ is the chain's at-the-money implied volatility, S is spot, and N is the
standard normal CDF. The `σ²/2` term is the median shift that makes the
process driftless in log terms.

`d` is evaluated once at each breakeven — $B_{\text{up}}$ (upper) and
$B_{\text{low}}$ (lower). $N(d(B_{\text{up}}))$ is the probability of
finishing below the upper breakeven and $N(d(B_{\text{low}}))$ the
probability of finishing below the lower one, so the difference is the
probability of landing between them, which is where a short strangle pays.

Two deliberate choices. It uses the **breakevens**, not the strikes — the
credit pushes the breakevens further out than the strikes, so the common
`1 − Δ` shortcut understates every proposal's real odds. And it takes the
chain's own at-the-money implied volatility rather than a fixed-tenor number,
so σ and τ refer to the same expiration.

The usual caveat applies: a lognormal has thin tails, and real equities gap.
Treat probability of profit as a comparison tool between proposals, not as a
forecast.

## Features

- **Screen** — pulls tastytrade market metrics for the whole universe in one
  batch, filters by IV rank floor, liquidity rating, and earnings proximity.
- **Price** — for any candidate, prices a ~45 DTE 16-delta strangle: credit,
  breakevens, estimated buying-power reduction, annualized return on
  capital, probability of profit (driftless lognormal, not the 1-delta
  shortcut), and spread cost as a share of credit.
- **Rank** — prices a whole shortlist concurrently and ranks by any of the
  above, so trades across different names and prices are actually
  comparable.
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
- **Scan log** (opt-in) — `tau scan --log` records the screen to a local
  SQLite database so picks can be compared against outcomes later. Off by
  default; `tau` writes nothing to disk unless you ask it to.

## Requirements

- Python 3.12+
- A tastytrade account with API access (personal OAuth grant, read scope is
  enough — `tau` never places orders)
- An Anthropic API key — optional. Without one, the catalyst read still
  fetches and shows headlines; only the classification is skipped.

## Setup

```bash
git clone https://github.com/jkartiwa/tau-options.git
cd tau-options
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in tastytrade OAuth credentials
```

The catalyst read needs the `anthropic` package, which is a separate extra:

```bash
pip install -e ".[dev,catalyst]"
```

Getting a tastytrade personal OAuth grant: my.tastytrade.com → Manage → API
→ OAuth Applications → create a personal grant, read scope. Put the client
secret and refresh token in `.env`. Add `ANTHROPIC_API_KEY` too if you want
the catalyst read.

## Usage

```bash
tau                           # interactive TUI (default)
tau tui                       # same, explicit
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
| `[` / `]` | move the IV rank floor down / up |
| `l` | cycle liquidity filter |
| `e` | cycle earnings filter |
| `s` | re-sort |
| `x` | toggle excluded view (shows exclusion reasons) |
| `c` / Enter | price the highlighted name's cycle, show the strangle |
| `w` | catalyst read for the highlighted name |
| `p` | price the whole current shortlist, switch to ranked view |
| `R` | force a re-price (rank view) |
| `space` | star a name (session-only) |
| `r` | refresh from the API |
| `esc` | back to the screen |
| `q` | quit |

Proposals and chains are cached per symbol, so leaving a view with `esc` and
coming back is instant — only `r`/`R` force a refetch.

**`c` — the chain.** Term structure, price position, and the 16-delta
strangle with credit, breakevens, and breakeven-vs-expected-move.

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

## How it works

- `screen.py` — pulls and filters market metrics; pure logic, separated from
  SDK parsing so it's testable without live data.
- `chain.py` — fetches one symbol's option chain and quotes over a single
  DXLink connection, selects strikes around a target delta, and builds the
  strangle. Expected move follows tastytrade's own weighted straddle/strangle
  convention rather than the textbook `S·σ·√t`.
- `history.py` — daily bars for price-position and volatility-normalized
  move context.
- `catalyst.py` — headlines plus one structured-output LLM call classifying
  why a name's vol is elevated.
- `propose.py` — turns priced candidates into ranked, comparable trades
  (return on capital, probability of profit, spread cost).
- `store.py` — opt-in SQLite scan log at `~/.local/share/tau/tau.sqlite3`
  (override with `TAU_DATA_DIR`).
- `tui/` — the Textual interactive app.

## Testing

```bash
pytest
```

Tests cover the pure filter/rank/pricing logic and TUI behavior without
hitting the live API.

## Status

Only the short strangle is implemented; iron condor and cash-secured put are
open, as is surfacing ex-dividend dates (an ex-div inside the trade window
is real early-assignment risk on the short call). Nothing reads the scan log
yet — it accumulates for a scoreboard that doesn't exist, which is why it's
opt-in.

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
