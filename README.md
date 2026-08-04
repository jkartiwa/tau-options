# tau

An options scanner for premium selling, built around user-defined structures.

Terminal only. Read scope only, so it cannot place orders.

![The screen](docs/img/screen.svg)

## Features

- **Screen** — one batch pull of tastytrade market metrics for the whole
  universe, filtered by IV rank, liquidity rating and earnings proximity.
  Thresholds re-filter in memory, so moving one costs no API call.
- **User-defined structures** — strategies are data, not hardcoded logic. A
  payoff engine derives breakevens, margin, probability of profit and return
  from the leg list, so adding a structure needs no new math. Six ship as
  examples: strangle, put vertical, iron condor, jade lizard, broken wing
  butterfly, cash-secured put.
- **Search, not a single trade** — any parameter in a definition can be a
  list, so one structure becomes every combination of deltas and widths, all
  priced off the same chain fetch.
- **Comparable ranking** — annualized return on capital, probability of profit
  from the real breakevens, and bid-ask cost as a share of premium, so trades
  on different names and prices can be ordered against each other.
- **Visible rejections** — structures that fail a constraint stay on screen
  with the rule they broke, instead of disappearing from the list.
- **Catalyst read** — recent headlines classified into pending event, resolved
  event, no single-name catalyst, or not enough signal, so a high IV rank can
  be told apart from a coin flip. Optional, needs an Anthropic API key.
- **Price context** — where a name sits in its 52-week range, and how far its
  recent move sits outside its own normal volatility.
- **Scan log** — optional SQLite record of each scan and the structures it
  picked, including which definition and variant produced them.

![Every variant considered on one name](docs/img/variants.svg)

## Install

Needs Python 3.12+ and a tastytrade account.

```bash
git clone https://github.com/jkartiwa/tau-options.git
cd tau-options
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[catalyst]"     # drop [catalyst] to skip the headline read
```

Create an OAuth personal grant at [my.tastytrade.com](https://my.tastytrade.com)
under Manage → API, read scope only. Then copy `.env.example` to `.env`, fill in
the client secret and refresh token, and check it works:

```bash
tau scan --top 5
```

See [docs/SETUP.md](docs/SETUP.md) if that fails.

## Commands

```bash
tau                        # interactive TUI
tau scan                   # the vol screen, text output
tau strategies             # the structures currently defined
tau rank --top 8           # best structure per name
tau variants SPY           # one name's whole search, rejections included
```

## Docs

- [User guide](docs/GUIDE.md) — commands, keys, what the columns mean, defining
  structures, the scan log
- [Setup](docs/SETUP.md) — credentials and troubleshooting

## Limits

- Buying power is an estimate from a standard margin formula, not a broker
  quote. Every return figure divides by it.
- Probability of profit assumes a driftless lognormal. Real markets gap.
- Quotes are mid-based and can be stale outside market hours.
- The catalyst read is a language model's opinion of recent headlines, with no
  measured accuracy.
- No ex-dividend warnings, no position awareness, monthly expirations only, no
  calendars or diagonals.

A personal research tool, not financial advice. Selling options carries uncapped
risk. Verify every number against your broker before trading.

## License

MIT — see [LICENSE](LICENSE).
