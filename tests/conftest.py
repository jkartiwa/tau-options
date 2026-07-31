"""Test-wide guarantee that nothing reaches the network.

The TUI takes every data source as an injected loader precisely so tests can
drive it offline, but the account read is different in kind: it is an
enrichment the app starts on its own at mount, so a test that says nothing
about it would still call the real one — and on a developer machine with
credentials exported, that means live REST traffic during a unit test.

The default is therefore neutralised here for every test at once, rather than
left to each one to remember. A test that wants a book passes its own loader,
which overrides this.
"""

import pytest


@pytest.fixture(autouse=True)
def no_account_access(monkeypatch):
    async def unavailable():
        raise RuntimeError("account access disabled in tests")

    monkeypatch.setattr("tau.tui.app.fetch_book", unavailable)
