# tau

**An options scanner for premium sellers.** It finds where volatility is
expensive, prices every option structure you have defined against the chain,
and ranks what comes out. It runs in the terminal and only has read access to
your account, so it cannot place orders.

![The screen](docs/img/screen.svg)

- **Screen** ~190 liquid names by IV rank, liquidity and earnings proximity
- **Define your own structures** in a few lines; the payoff engine does the math
- **Search, don't pin** — every delta and width combination off one chain fetch
- **Rank comparably** by annualized return, probability of profit, spread cost
- **See the rejects**, each with the constraint it broke
- **Know why vol is bid** before you sell it
- **Log every pick**, including which definition produced it

### Best structure per name

![Rank view](docs/img/rank.svg)

### Everything it considered, rejects included

![Every variant on one name](docs/img/variants.svg)

### Why volatility is bid

![Catalyst read](docs/img/catalyst.svg)

## Quick start

You will need Python 3.12 or newer, and a tastytrade account with read scope.

```bash
git clone https://github.com/jkartiwa/tau-options.git
cd tau-options
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[catalyst]"
cp .env.example .env          # tastytrade client secret + refresh token
tau
```

## Documentation

- **[Setup](docs/SETUP.md)** — getting the tastytrade credentials, configuring
  the environment, and what to check when the quick start above does not work.
- **[User guide](docs/GUIDE.md)** — the commands and their flags, the keyboard
  shortcuts, what every column and figure means, how to define your own
  structures, and the scan log.

## Limits

Buying power is an estimate from a margin formula, not a broker quote.
Probability of profit assumes a driftless lognormal. Quotes are mid-based. The
catalyst read is a language model's opinion of recent headlines with no
measured accuracy.

This is a personal research tool and not financial advice. Selling options
carries uncapped risk, so verify every number against your broker before you
trade.

## License

MIT — see [LICENSE](LICENSE).
