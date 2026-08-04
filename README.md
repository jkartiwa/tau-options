# tau

An options scanner for premium selling, built around user-defined structures.

It screens a universe of liquid names by IV rank, liquidity and earnings
proximity, then prices whatever option structures you have defined against each
candidate's chain and ranks the results. Structures are Python definitions
rather than hardcoded logic: a payoff engine derives breakevens, margin,
probability of profit and return from the leg list, so adding one requires no
new math.

Terminal only. Read scope only, so it cannot place orders.

![The screen](docs/img/screen.svg)

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

## Defining a structure

One module per structure in `src/tau/strategies/`, registered in `ALL`:

```python
VERTICAL_PUT = Strategy(
    name="vertical-put",
    bias=Bias.BULLISH,
    legs=[
        LegSpec("short_put", type=P, side=SHORT, strike=Delta([0.20, 0.30])),
        LegSpec("long_put",  type=P, side=LONG,
                strike=Ref("short_put", offset=[-5, -10])),
    ],
    require=[Require("spread_cost", "<=", MAX_SPREAD_COST)],
)
```

Any value can be a list, so this is four variants rather than one trade. tau
prices all of them, keeps the best, and shows the rest with the reason each was
rejected.

Six structures ship as examples. See
[docs/GUIDE.md](docs/GUIDE.md#defining-your-own-structure) for the available
selectors and constraints.

![Every variant considered on one name](docs/img/variants.svg)

## Docs

- [User guide](docs/GUIDE.md) — commands, keys, what the columns mean, defining
  structures, the scan log
- [Setup](docs/SETUP.md) — credentials and troubleshooting

## Limits

- **Buying power is an estimate** from a standard margin formula, not a broker
  quote. Every return figure divides by it.
- **Probability of profit assumes a driftless lognormal.** Real markets gap.
- **Quotes are mid-based** and can be stale outside market hours.
- **The catalyst read is a language model's opinion** of recent headlines, with
  no measured accuracy.
- No ex-dividend warnings, no position awareness, monthly expirations only, no
  calendars or diagonals.

A personal research tool, not financial advice. Selling options carries uncapped
risk. Verify every number against your broker before trading.

## License

MIT — see [LICENSE](LICENSE).
