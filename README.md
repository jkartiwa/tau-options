# tau

**An options scanner for premium sellers.** Finds where volatility is
expensive, tells you *why* it is expensive, then searches six option
structures across the whole chain and ranks what it finds — showing you what
it rejected as well as what it liked.

Terminal only. Read-only: it cannot place a trade even if it tried.

![The screen](docs/img/screen.svg)

## Why bother

**A high IV rank tells you almost nothing on its own.** A name is "high IVR"
either because it panicked and will mean-revert, or because it is genuinely
repricing for an earnings date you are about to sell into. `tau` screens for
the first and flags the second, using both a realized-vol comparison and a
headline read of what is actually pending.

**One chain fetch, every structure.** Strangles, put verticals, iron condors,
jade lizards, broken wing butterflies, cash-secured puts — 56 variants of them
priced on the same data, ranked on one comparable number. The fetch is the
only slow part, so searching six structures costs what searching one did.

**It shows you what it threw away.** This is the part most scanners skip:

![Every variant considered on one name](docs/img/variants.svg)

Look at the four greyed rows. They show **979% to 1808% annualized** — several
times better than anything that passed. They were rejected because crossing
their legs costs 68% to 224% of the premium you would collect. They are not
trades; they are an artifact of dividing a real profit by a small margin
number. Ranked on return alone they would have been your top four ideas of the
day.

Every rejection stays on screen with the reason it failed. A missing row
teaches you nothing.

## Try it

Needs Python 3.12+ and a tastytrade account. Read scope is enough.

```bash
git clone https://github.com/jkartiwa/tau-options.git
cd tau-options
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[catalyst]"        # drop [catalyst] to skip the headline read
```

Create an OAuth **personal grant** at [my.tastytrade.com](https://my.tastytrade.com)
under **Manage → API**, read scope only, then:

```bash
cp .env.example .env                # fill in the secret and refresh token
tau scan --top 5                    # a table of five names means it works
```

Full credential walkthrough, including the two things that trip people up:
**[docs/SETUP.md](docs/SETUP.md)**.

Then the three commands worth knowing:

```bash
tau                                 # the interactive TUI — start here
tau rank --top 8                    # best structure per name, as text
tau variants SPY                    # one name's whole search, rejections included
```

Market metrics are precomputed server-side, so the screen works outside
market hours. Chain quotes after hours are wide, which makes more structures
fail their spread-cost check than would during the session.

## Where next

| | |
|---|---|
| **[User guide](docs/GUIDE.md)** | How a session goes, what every number means and where it misleads, how to define your own structure |
| **[Design notes](docs/DESIGN.md)** | Why the payoff function is the right abstraction, how probability of profit is computed, where the name comes from |
| **[Setup](docs/SETUP.md)** | Credentials, environment, troubleshooting |

## What to distrust

A personal research tool, not financial advice. Selling options carries
uncapped risk; a short strangle can lose far more than the credit received.

Four numbers specifically:

- **Buying power is a formula, not a broker quote.** The read-only grant
  cannot dry-run an order. Every return figure divides by this, and condors
  and broken wings rank highest on it. Check it against your broker before
  trading a new structure family.
- **Probability of profit assumes a driftless lognormal.** Real equities gap,
  and short premium is exactly the position that gets hurt when they do.
- **Quotes are mid-based** and can be stale, especially outside market hours.
- **The catalyst read is a language model's opinion** of recent headlines,
  with no measured accuracy. It can and will miss events.

Verify every number against your broker before placing a trade. Provided as
is, without warranty.

## License

MIT — see [LICENSE](LICENSE).
