"""Shared test setup.

`tau.broker` keeps process-global state on purpose — the resolved margin
account and the dry-run circuit breaker are both facts about the token and the
account API that do not vary by symbol, so they are learned once per run. A
test process is many runs, and one test tripping the breaker would silently
disable enrichment for every test after it.
"""

import pytest

from tau import broker as broker_mod


@pytest.fixture(autouse=True)
def _fresh_broker_state():
    broker_mod._state = broker_mod._State()
    yield
    broker_mod._state = broker_mod._State()
