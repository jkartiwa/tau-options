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
tau scan                     # ranked screen with default filters
tau scan --min-ivr 40        # tighter IV rank floor
tau scan --all               # every symbol, with exclusion reasons
tau scan --days 0            # keep symbols with upcoming earnings
```

Every scan is logged to a local SQLite database so picks can be evaluated
against outcomes later.
