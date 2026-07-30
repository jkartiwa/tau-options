"""Tastytrade OAuth session — the only auth surface. Personal-grant scheme:
TASTY_CLIENT_SECRET + TASTY_REFRESH_TOKEN (read scope), the refresh token
never expires, and the SDK mints the short-lived access tokens per request,
so one cached Session serves the process."""

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
