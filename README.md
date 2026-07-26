# tau

Premium-selling options scanner: screens a universe of liquid ETFs and
single names by IV rank, liquidity, and earnings proximity, and prints
ranked short-premium candidates.

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in tastytrade OAuth credentials
```

## Usage

```bash
tau tui                      # interactive triage (recommended)
tau scan                     # ranked screen with default filters
tau scan --min-ivr 40        # tighter IV rank floor
tau scan --all               # every symbol, with exclusion reasons
tau scan --days 0            # keep symbols with upcoming earnings
```

In the TUI, one metrics pull feeds everything: `[` / `]` move the IV rank
floor, `l` and `e` cycle the liquidity and earnings filters, `s` re-sorts,
`x` shows what was excluded and why — all instantly, with no refetch.
`c` or Enter prices the highlighted name's ~45 DTE cycle and shows a
16-delta strangle with credit, breakevens, and breakeven-vs-expected-move.

Every scan is logged to a local SQLite database so picks can be evaluated
against outcomes later.
