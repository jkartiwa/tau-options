"""Shared test setup.

`tau.broker` keeps process-global state on purpose — the resolved margin
account and the dry-run circuit breaker are both facts about the token and the
account API that do not vary by symbol, so they are learned once per run. A
test process is many runs, and one test tripping the breaker would silently
disable enrichment for every test after it.
"""

import pytest


@pytest.fixture(autouse=True)
def _fresh_broker_state():
    from tau import broker as broker_mod

    def reset():
        broker_mod._margin_account = False
        broker_mod._consecutive_failures = 0
        broker_mod._tripped = False

    reset()
    yield
    reset()
