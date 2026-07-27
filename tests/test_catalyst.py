import json
from datetime import date

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


def test_news_query_strips_only_trailing_legal_suffixes():
    # "Trust" is part of the brand here, not a suffix to drop.
    assert news_query("FXU", "First Trust Utilities AlphaDEX Fund") == (
        "First Trust Utilities AlphaDEX Fund FXU"
    )
    assert news_query("INTC", "Intel Corp") == "Intel INTC"
    assert news_query("DOW", "Dow Inc.") == "Dow DOW"


def test_news_query_falls_back_to_the_ticker():
    assert news_query("AAPL") == "AAPL stock"
