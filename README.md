# tau

A premium-selling options scanner for the tastytrade API. Screens a universe
of liquid equity/ETF names by IV rank, liquidity, and earnings proximity;
prices short-premium candidates into comparable, ranked trades; and gives
each one a plain-language read on *why* its volatility is elevated before
you sell it. CLI-first, no web UI — a text screen for quick checks and a
Textual TUI for interactive triage.

## Why

High IV rank alone doesn't tell you whether premium is free money or fair
payment for a coin flip — a name can be "high IVR" because it's a panic
spike about to mean-revert, or because it's genuinely repricing for a
pending binary event (earnings, an FDA date, litigation). tau screens for
the former and flags the latter, then prices what's left into apples-to-apples
trades (annualized return on capital, probability of profit, spread cost as
a share of credit) instead of leaving you to compare raw IV numbers across
different names, strikes, and expirations.

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
- **Catalyst read** — pulls recent headlines and asks an LLM to classify why
  the vol is bid: a pending binary event, an already-resolved one, no
  identifiable single-name catalyst, or insufficient signal to say. Biased
  toward saying "not sure" over a false all-clear.
- **Scan log** — every scan writes to a local SQLite database so picks can
  be evaluated against outcomes later.

## Requirements

- Python 3.12+
- A tastytrade account with API access (personal OAuth grant, read scope is
  enough — tau never places orders)
- An Anthropic API key, only if you want the catalyst read (`w` in the TUI)

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in tastytrade OAuth credentials
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
`--top N`, `--all`, `--universe PATH`, `--no-log`.

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
- `store.py` — SQLite scan log at `~/.local/share/tau/tau.sqlite3` (override
  with `TAU_DATA_DIR`).
- `tui/` — the Textual interactive app.

## Testing

```bash
pytest
```

Tests cover the pure filter/rank/pricing logic and TUI behavior without
hitting the live API.

## Status

Only the short strangle is implemented. Iron condor and cash-secured put,
ex-dividend surfacing, and a persistent scoreboard over the scan-log corpus
are still open.

## License

MIT — see [LICENSE](LICENSE).
