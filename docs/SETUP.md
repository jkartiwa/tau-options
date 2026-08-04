# Setup

The [README](../README.md) has the three-line version. This is the full one,
including the two things that reliably trip people up.

## Requirements

- Python 3.12+
- A tastytrade account with API access. **Read scope is enough** — `tau`
  cannot place, modify, or cancel an order even if it tried.
- An Anthropic API key, optional. Without one the catalyst read still fetches
  and shows headlines; only the classification is skipped.

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
4. Under scopes, select **read** only. Don't grant trade scope.
5. Save. You'll be shown a **client ID**, a **client secret**, and a
   **refresh token**.

Two things worth knowing here:

- **Copy the client secret and refresh token immediately.** They are shown
  once. If you lose them, delete the grant and create a new one.
- **The client ID is not used.** `tau` needs only the secret and the refresh
  token. The refresh token doesn't expire, and the SDK exchanges it for a
  short-lived access token on each run.

## 3. Configure the environment

```bash
cp .env.example .env
```

```ini
# .env
TASTY_CLIENT_SECRET=your-client-secret
TASTY_REFRESH_TOKEN=your-refresh-token

# Optional: enables the catalyst classification. Without it, `w` still
# fetches and shows headlines, it just won't classify them.
ANTHROPIC_API_KEY=sk-ant-...

# Optional overrides
# TAU_DATA_DIR=~/.local/share/tau
# TAU_UNIVERSE=/path/to/universe.txt
```

`.env` is gitignored. Never commit it.

**Real environment variables take precedence.** `tau` loads `.env` but will
not override a variable already set in your shell. If a value looks like it
isn't taking effect, check for a stale export — this is the most common
configuration problem by a wide margin:

```bash
echo $TASTY_CLIENT_SECRET     # empty is what you want if you rely on .env
```

You can skip `.env` entirely and export the variables instead, which is the
better option on a shared machine or in CI:

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
hours. Chain pricing (`tau rank`, `tau variants`, the TUI's `c` and `p`) also
works after hours, but the quotes are wide, so more structures fail their
spread-cost check than would during the session. That is the constraint
working, not a fault — just don't read the pass rate as representative.

## 5. Run the tests

```bash
pip install -e ".[dev]"
pytest
```

The suite covers the payoff arithmetic, strategy validation, strike
resolution, filtering, ranking, the scan log, and TUI behavior. Nothing in it
touches the live API.
