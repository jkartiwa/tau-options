# tau

**An options scanner for premium sellers.** Finds where volatility is
expensive, prices every option structure you have defined against the chain,
and ranks what comes out. Terminal only, read-only, cannot place orders.

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

Needs Python 3.12+ and a tastytrade account (read scope).

```bash
git clone https://github.com/jkartiwa/tau-options.git
cd tau-options
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[catalyst]"
cp .env.example .env          # tastytrade client secret + refresh token
tau
```

[Setup](docs/SETUP.md) if that fails. [User guide](docs/GUIDE.md) for the
commands, the keys, what the columns mean, and how to define your own
structures.

## Limits

Buying power is an estimate from a margin formula, not a broker quote.
Probability of profit assumes a driftless lognormal. Quotes are mid-based. The
catalyst read is a language model's opinion of recent headlines with no
measured accuracy.

A personal research tool, not financial advice. Selling options carries
uncapped risk. Verify every number against your broker before trading.

## License

MIT — see [LICENSE](LICENSE).
