import json
import sys
from datetime import date
from types import ModuleType

import pytest

from tau.catalyst import (
    NO_CATALYST,
    PENDING,
    RESOLVED,
    UNKNOWN,
    Headline,
    classify,
    news_query,
)


class FakeResponse:
    def __init__(self, payload, stop_reason="end_turn"):
        self.stop_reason = stop_reason

        class Block:
            type = "text"
            text = json.dumps(payload)

        self.content = [Block()] if payload is not None else []


class FakeClient:
    """Stands in for the Anthropic client; records what it was asked."""

    def __init__(self, payload, stop_reason="end_turn"):
        self._response = FakeResponse(payload, stop_reason)
        self.calls = []

    class _Messages:
        def __init__(self, outer):
            self._outer = outer

        def create(self, **kwargs):
            self._outer.calls.append(kwargs)
            return self._outer._response

    @property
    def messages(self):
        return FakeClient._Messages(self)


VERDICT = {
    "classification": RESOLVED,
    "catalyst": "Q2 earnings reported 2026-07-23",
    "key_dates": [],
    "confidence": "high",
    "note": "Event passed; IV should bleed.",
}


def headlines(n=5):
    return tuple(
        Headline(day=date(2026, 7, 20), title=f"Story {i}") for i in range(n)
    )


def test_classify_parses_a_verdict():
    client = FakeClient(VERDICT)
    brief = classify("INTC", headlines(), today=date(2026, 7, 26), client=client)
    assert brief.classification == RESOLVED
    assert brief.catalyst.startswith("Q2 earnings")
    assert brief.tradable
    assert brief.gloss


def test_todays_date_reaches_the_prompt():
    """Pending-vs-resolved is a comparison against today; without the date in
    the prompt the model cannot order events at all."""
    client = FakeClient(VERDICT)
    classify("INTC", headlines(), today=date(2026, 7, 26), client=client)
    assert "2026-07-26" in client.calls[0]["system"]


def test_key_dates_are_carried_through():
    payload = dict(
        VERDICT,
        classification=PENDING,
        key_dates=[{"date": "2026-08-14", "event": "FDA PDUFA"}],
    )
    brief = classify("OMGA", headlines(), client=FakeClient(payload))
    assert [(k.day, k.event) for k in brief.key_dates] == [
        ("2026-08-14", "FDA PDUFA")
    ]
    assert not brief.tradable


def test_too_few_headlines_never_reaches_the_model():
    """An unanswerable question invites a confident wrong answer, so it is
    not asked."""
    client = FakeClient(VERDICT)
    brief = classify("ZZZZ", headlines(1), client=client)
    assert brief.classification == UNKNOWN
    assert not client.calls
    assert not brief.tradable


def test_a_refusal_becomes_insufficient_signal_not_an_all_clear():
    client = FakeClient(VERDICT, stop_reason="refusal")
    brief = classify("X", headlines(), client=client)
    assert brief.classification == UNKNOWN
    assert not brief.tradable


def test_no_api_key_returns_the_headlines_instead_of_raising(monkeypatch):
    """Classification is the optional half. Without a key the headlines still
    come back, so the feature degrades instead of failing."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    hs = headlines()
    brief = classify("X", hs)
    assert brief.classification == UNKNOWN
    assert not brief.tradable
    assert brief.headlines == hs


def test_empty_content_becomes_insufficient_signal():
    brief = classify("X", headlines(), client=FakeClient(None))
    assert brief.classification == UNKNOWN
    assert not brief.tradable


@pytest.mark.parametrize(
    "verdict, tradable",
    [(RESOLVED, True), (NO_CATALYST, True), (PENDING, False), (UNKNOWN, False)],
)
def test_only_a_positive_read_counts_as_tradable(verdict, tradable):
    brief = classify(
        "X", headlines(), client=FakeClient(dict(VERDICT, classification=verdict))
    )
    assert brief.tradable is tradable


class ApiError(Exception):
    """Shaped like anthropic.APIError: carries the SDK's `type` and
    `status_code` so the reason string can be checked against them."""

    def __init__(self, message, type=None, status_code=None):
        super().__init__(message)
        self.type = type
        self.status_code = status_code


@pytest.fixture
def anthropic_installed(monkeypatch):
    """The optional package is not a test dependency, so stand in a module
    exposing the one class classify() catches."""
    module = ModuleType("anthropic")
    module.APIError = ApiError
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return module


class RaisingClient:
    """A client whose model call blows up."""

    def __init__(self, exc):
        self._exc = exc

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        raise self._exc


class RawTextClient:
    """Returns whatever text it was given, schema or no schema."""

    def __init__(self, text):
        self._text = text

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        block = type("Block", (), {"type": "text", "text": self._text})()
        return type("Response", (), {"stop_reason": "end_turn", "content": [block]})()


def assert_degraded(brief, hs):
    """A failed classification hands back the headlines and endorses nothing."""
    assert brief.classification == UNKNOWN
    assert brief.headlines == hs
    assert not brief.tradable


@pytest.mark.parametrize(
    "exc, expected",
    [
        (ApiError("rate limited", type="rate_limit_error", status_code=429), "rate"),
        (ApiError("credit balance is too low", type="billing_error"), "credit"),
        (ApiError("overloaded", type="overloaded_error", status_code=529), "failed"),
    ],
)
def test_a_failed_model_call_still_returns_the_headlines(
    anthropic_installed, exc, expected
):
    """The headlines are already fetched and cost nothing; a quota or rate
    limit reads differently to someone deciding whether to retry, so it is
    named separately from a generic failure."""
    hs = headlines()
    brief = classify("X", hs, client=RaisingClient(exc))
    assert_degraded(brief, hs)
    assert expected in brief.note


def test_a_connection_failure_returns_the_headlines():
    """Not every transport error arrives as an SDK exception."""
    hs = headlines()
    brief = classify("X", hs, client=RaisingClient(ConnectionError("no route")))
    assert_degraded(brief, hs)
    assert "failed" in brief.note


def test_a_timeout_returns_the_headlines():
    hs = headlines()
    brief = classify("X", hs, client=RaisingClient(TimeoutError("timed out")))
    assert_degraded(brief, hs)


@pytest.mark.parametrize(
    "text",
    [
        "not json at all",
        json.dumps({"classification": RESOLVED}),  # right type, missing fields
        json.dumps([1, 2, 3]),  # right syntax, wrong shape entirely
    ],
)
def test_an_unreadable_payload_returns_the_headlines(text):
    hs = headlines()
    brief = classify("X", hs, client=RawTextClient(text))
    assert_degraded(brief, hs)
    assert "unreadable" in brief.note


def test_a_programming_error_is_not_reported_as_headlines_only():
    """Degrading is for failures of the call, not for bugs in this module —
    swallowing those would hide a refactor that broke the request."""
    with pytest.raises(AttributeError):
        classify("X", headlines(), client=RaisingClient(AttributeError("typo")))


def test_news_query_strips_only_trailing_legal_suffixes():
    # "Trust" is part of the brand here, not a suffix to drop.
    assert news_query("FXU", "First Trust Utilities AlphaDEX Fund") == (
        "First Trust Utilities AlphaDEX Fund FXU"
    )
    assert news_query("INTC", "Intel Corp") == "Intel INTC"
    assert news_query("DOW", "Dow Inc.") == "Dow DOW"


def test_news_query_falls_back_to_the_ticker():
    assert news_query("AAPL") == "AAPL stock"
