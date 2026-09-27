# Setup

## Requirements

- Python 3.12+
- A tastytrade account with API access, with **trade scope** on the personal
  grant. The buying-power figure comes from the order dry-run calculation,
  which is a trading-scope endpoint. tau only calls that calculation; the
  package has no code that places, modifies, or cancels an order.
- An Anthropic API key, optional. Without one the catalyst read still fetches
  and shows headlines, and skips the classification.

## 1. Install

```bash
git clone https://github.com/jkartiwa/tau-options.git
cd tau-options
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e "."
```

Two optional extras:

```bash
pip install -e ".[dev]"            # pytest, to run the test suite
pip install -e ".[catalyst]"       # anthropic, for the catalyst classification
pip install -e ".[dev,catalyst]"   # both
```

## 2. Get tastytrade credentials

`tau` authenticates with an OAuth2 **personal grant** — a self-issued
credential tied to your own account, not an app other people log into.

1. Log in at [my.tastytrade.com](https://my.tastytrade.com).
2. Go to **Manage → API** and open **OAuth Applications**.
3. Create a **personal grant**. Give it a name (`tau` works).
4. Under scopes, select **trade** (or your broker's equivalent of
   read+trade). With a read-only grant every buying-power figure falls
   back to the formula estimate.
5. Save. You'll be shown a **client ID**, a **client secret**, and a
   **refresh token**.

- **Copy the client secret and refresh token immediately.** They are shown
  once. If you lose them, delete the grant and create a new one.
- **The client ID is not used.** `tau` needs only the secret and the refresh
  token. The refresh token does not expire; the SDK exchanges it for a
  short-lived access token on each run.

## 3. Configure the environment

```bash
cp .env.example .env
```

```ini
# .env
TASTY_CLIENT_SECRET=your-client-secret
TASTY_REFRESH_TOKEN=your-refresh-token

# Optional: enables the catalyst classification (headlines show without it).
# ANTHROPIC_API_KEY=sk-ant-...

# Optional overrides
# TAU_DATA_DIR=~/.local/share/tau
# TAU_UNIVERSE=/path/to/universe.txt
```

`.env` is gitignored. Never commit it.

Shell variables take precedence: `tau` loads `.env` but never overrides a
variable already set. If a value does not take effect, check for a stale
export:

```bash
echo $TASTY_CLIENT_SECRET     # empty is what you want if you rely on .env
```

You can skip `.env` and export the variables instead, for example on a
shared machine:

```bash
export TASTY_CLIENT_SECRET=...
export TASTY_REFRESH_TOKEN=...
```

## 4. Verify

```bash
tau scan --top 5
```

A table of five symbols means the credentials work. An error mentioning
`TASTY_CLIENT_SECRET / TASTY_REFRESH_TOKEN not set` means neither `.env` nor
the shell supplied them.

Market metrics are precomputed server-side, so this works outside market
hours. Chain pricing also works after hours, but the quotes are wide, so more
structures fail their spread-cost check than would during the session.

## 5. Run the tests

```bash
pip install -e ".[dev]"
pytest
```

None of the tests touch the live API.
