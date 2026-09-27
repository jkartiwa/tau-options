"""The tastytrade OAuth session, tau's only auth surface.

Built from TASTY_CLIENT_SECRET and TASTY_REFRESH_TOKEN. The refresh token does
not expire and the SDK mints short-lived access tokens as needed, so one cached
Session serves the process.

The grant needs trading scope because the order dry-run is a trading-scope
endpoint, but that dry-run is the only trading call tau makes. There is no
order-placement code in this package.
"""

import os
from functools import cache

from tastytrade import Session


@cache
def get_session() -> Session:
    secret = os.environ.get("TASTY_CLIENT_SECRET")
    token = os.environ.get("TASTY_REFRESH_TOKEN")
    if not (secret and token):
        raise RuntimeError("TASTY_CLIENT_SECRET / TASTY_REFRESH_TOKEN not set (.env)")
    return Session(secret, token)
