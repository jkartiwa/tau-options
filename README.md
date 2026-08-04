# tau

**An options scanner where the strategies are data.** Describe a structure as
a list of legs and a few constraints on how it has to price; `tau` searches it
across a universe of names, ranks it against every other structure you've
defined, and shows you what it rejected as well as what it liked.

Built for premium selling, and it finds where volatility is expensive and
tells you *why* before you sell it. Terminal only. Read-only: it cannot place
a trade even if it tried.

![The screen](docs/img/screen.svg)

## Why bother

**Describe a structure, get a search.** This is a complete strategy — a
shipped one, minus its imports:

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

Two deltas times two widths is four variants, because **any value may be a
list** — a definition is a search space, not a single trade. `tau` prices all
four on every name that passes the screen, keeps the best, and keeps the rest
to show you.

Now notice what is *not* in that file. No breakevens, no margin formula, no
probability, no return calculation, no mention of what a vertical is. All of
it is derived from the leg list by a payoff engine that does not know one
structure from another, which is why **adding a structure means writing no
math**. Six ship as starting points; the interesting ones are the ones you
write.

**A high IV rank tells you almost nothing on its own.** A name is "high IVR"
either because it panicked and will mean-revert, or because it is genuinely
repricing for an earnings date you are about to sell into. `tau` separates
those with a realized-vol comparison and a headline read of what is actually
pending — because the structure search is only worth running on names where
the premium is worth collecting.

**It shows you what it threw away.** The part most scanners skip:

![Every variant considered on one name](docs/img/variants.svg)

Look at the four greyed rows. They show **979% to 1808% annualized** — several
times better than anything that passed. They were rejected because crossing
their legs costs 68% to 224% of the premium you would collect. They are not
trades; they are an artifact of dividing a real profit by a small margin
number. Ranked on return alone they would have been your top four ideas of the
day.

Constraints are part of the definition, so every rejection carries the rule it
broke. A missing row teaches you nothing.

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

Then the commands worth knowing:

```bash
tau                                 # the interactive TUI — start here
tau strategies                      # what is currently defined, and what it searches
tau rank --top 8                    # best structure per name, as text
tau variants SPY                    # one name's whole search, rejections included
```

To add your own: drop a module in `src/tau/strategies/`, add it to `ALL`, and
it is searched, priced, ranked and logged alongside everything else from the
next run. Nothing else has to change — the
[guide](docs/GUIDE.md#defining-your-own-structure) walks through the selectors
and constraints.

Market metrics are precomputed server-side, so the screen works outside
market hours. Chain quotes after hours are wide, which makes more structures
fail their spread-cost check than would during the session.

## Where next

| | |
|---|---|
| **[User guide](docs/GUIDE.md)** | How to define your own structure, how a session goes, what every number means and where it misleads |
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
