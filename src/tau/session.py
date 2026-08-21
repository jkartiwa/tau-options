"""Tastytrade OAuth session — the only auth surface. Personal-grant scheme:
TASTY_CLIENT_SECRET + TASTY_REFRESH_TOKEN, the refresh token never expires,
and the SDK mints the short-lived access tokens per request, so one cached
Session serves the process.

The grant carries trading scope — the order dry-run calculation is a
*calculation* but a trading-scope call — yet tau only ever calls that
dry-run endpoint. There is no order-placement code in this package, and the
broker module draws the line where the code does.
"""

import os

from tastytrade import Session

_session: Session | None = None


def get_session() -> Session:
    global _session
    if _session is None:
        secret = os.environ.get("TASTY_CLIENT_SECRET")
        token = os.environ.get("TASTY_REFRESH_TOKEN")
        if not (secret and token):
            raise RuntimeError(
                "TASTY_CLIENT_SECRET / TASTY_REFRESH_TOKEN not set (.env)"
            )
        _session = Session(secret, token)
    return _session
