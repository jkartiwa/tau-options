"""Why is this name's vol bid?

High IV rank is never free. Something put it there, and the something decides
whether the premium is harvestable or fair payment for a coin flip: a pending
binary is a landmine, a just-resolved event is often the best sale on the
screen, and sector sympathy is the textbook mean-reversion case. That judgment
is the step a trader otherwise does by alt-tabbing to a browser for every
candidate, so it is worth one model call and a cache.

Headlines come from Google News RSS over stdlib http; the classification is a
single structured-output call. Both are on demand — nothing here runs during a
scan.
"""

import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

MODEL = "claude-sonnet-5"
NEWS_URL = "https://news.google.com/rss/search"
NEWS_WINDOW_DAYS = 14
MAX_HEADLINES = 20
HTTP_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; tau/0.1)"

# Under this many headlines there is nothing to reason about, so the model is
# never asked — an unanswerable question invites a confident wrong answer.
MIN_HEADLINES = 3

PENDING = "pending_binary"
RESOLVED = "resolved"
NO_CATALYST = "no_idiosyncratic"
UNKNOWN = "insufficient_signal"

# How each verdict bears on selling a 45-DTE strangle.
VERDICT_GLOSS = {
    PENDING: "event risk ahead — premium is payment for a binary",
    RESOLVED: "event passed — IV usually bleeds slower than the risk left",
    NO_CATALYST: "no single-name event — sector or macro vol",
    UNKNOWN: "not enough signal to say — check before selling",
}

SYSTEM = """You are an analyst supporting a systematic options premium seller. \
The trader sells roughly 45-day short strangles on liquid names screened for \
high IV rank. Elevated implied volatility always has a cause; your job is to \
classify that cause so the trader knows whether the premium is harvestable \
edge or fair compensation for a binary event.

Today's date is {today}.

Classify the volatility driver into exactly one of:

- "pending_binary": a dated or strongly expected FUTURE event with \
discontinuous outcomes lands soon — upcoming earnings, an FDA decision, a \
court ruling, in-progress M&A, a guidance event. Selling a strangle across \
this is selling event risk.
- "resolved": the catalyst ALREADY HAPPENED — earnings reported, ruling \
issued, news broke and is being digested. Implied vol typically stays \
elevated after resolution and bleeds off slower than the risk actually left, \
which is what makes these attractive sales.
- "no_idiosyncratic": you have enough information to conclude there is no \
company-specific catalyst. Vol is elevated in sympathy with the sector or the \
broad market, or from a sustained drift with no single event behind it. This \
is a positive finding, not a shrug — a diversified sector ETF usually belongs \
here, because it genuinely has no single-name binary.
- "insufficient_signal": you cannot tell. The headlines are absent, stale, or \
entirely non-substantive — listicles, price-target chatter, "should you buy" \
filler — so no conclusion about the vol driver is supportable.

The distinction between the last two matters and is easy to get wrong. \
"no_idiosyncratic" asserts something: you looked and the absence of a \
company-specific catalyst is itself the finding. "insufficient_signal" \
asserts nothing: you looked and cannot see well enough to judge. When a \
single name's headlines are all filler, that is insufficient_signal, not \
no_idiosyncratic. Never resolve doubt in the direction of reassurance — a \
false all-clear is the costliest output you can produce here.

Further rules:
- Weigh headline DATES against today's date. Earnings reported three days ago \
are resolved; earnings expected next week are pending.
- If both apply — earnings just passed, but another dated event falls within \
about 45 days — classify pending_binary. Forward risk dominates the structure.
- Analyst commentary, price targets, and ranked-list articles are not \
catalysts. Ignore them.
- Be decisive about the classification and express any doubt through \
"confidence" rather than hedging the label.
- "note" is one sentence for a professional trader: the actionable takeaway, \
not a summary of the headlines."""

SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": [PENDING, RESOLVED, NO_CATALYST, UNKNOWN],
        },
        "catalyst": {
            "type": ["string", "null"],
            "description": "The specific driver, or null if none identifiable",
        },
        "key_dates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "event": {"type": "string"},
                },
                "required": ["date", "event"],
                "additionalProperties": False,
            },
            "description": "Dated events falling within roughly 45 days",
        },
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "note": {"type": "string"},
    },
    "required": ["classification", "catalyst", "key_dates", "confidence", "note"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Headline:
    day: date | None
    title: str

    def render(self) -> str:
        stamp = self.day.isoformat() if self.day else "undated"
        return f"[{stamp}] {self.title}"


@dataclass(frozen=True)
class KeyDate:
    day: str
    event: str


@dataclass(frozen=True)
class Brief:
    """The catalyst read for one symbol."""

    symbol: str
    classification: str
    catalyst: str | None
    key_dates: tuple[KeyDate, ...]
    confidence: str
    note: str
    headlines: tuple[Headline, ...]
    fetched_at: datetime

    @property
    def gloss(self) -> str:
        return VERDICT_GLOSS.get(self.classification, "")

    @property
    def tradable(self) -> bool:
        """Whether the verdict argues for selling premium here. A pending
        binary argues against; an unreadable name is not an endorsement."""
        return self.classification in (RESOLVED, NO_CATALYST)


def _parse_day(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).date()
    except (TypeError, ValueError):
        return None


def fetch_headlines(
    query: str,
    window_days: int = NEWS_WINDOW_DAYS,
    limit: int = MAX_HEADLINES,
) -> tuple[Headline, ...]:
    """Recent headlines for a free-text query, newest first."""
    params = urllib.parse.urlencode(
        {
            "q": f"{query} when:{window_days}d",
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        }
    )
    request = urllib.request.Request(
        f"{NEWS_URL}?{params}", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        body = response.read()

    root = ElementTree.fromstring(body)
    out: list[Headline] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        out.append(Headline(day=_parse_day(item.findtext("pubDate")), title=title))
        if len(out) >= limit:
            break
    return tuple(out)


def _unreadable(symbol: str, headlines: tuple[Headline, ...], why: str) -> Brief:
    return Brief(
        symbol=symbol,
        classification=UNKNOWN,
        catalyst=None,
        key_dates=(),
        confidence="high",
        note=why,
        headlines=headlines,
        fetched_at=datetime.now(UTC),
    )


def classify(
    symbol: str,
    headlines: tuple[Headline, ...],
    today: date | None = None,
    client=None,
) -> Brief:
    """Classify the vol driver from headlines. Never guesses: too little to
    read on returns insufficient_signal without spending a model call."""
    today = today or date.today()
    if len(headlines) < MIN_HEADLINES:
        count = len(headlines)
        return _unreadable(
            symbol,
            headlines,
            f"only {count} headline{'' if count == 1 else 's'} in the window",
        )

    if client is None:
        import anthropic

        if not os.getenv("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY not set (.env)")
        client = anthropic.Anthropic()

    body = f"Symbol: {symbol}\n\nRecent headlines (newest first):\n" + "\n".join(
        f"- {h.render()}" for h in headlines
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM.format(today=today.isoformat()),
        messages=[{"role": "user", "content": body}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
    )
    if response.stop_reason == "refusal":
        return _unreadable(symbol, headlines, "model declined to classify")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        return _unreadable(symbol, headlines, "model returned no verdict")
    data = json.loads(text)
    return Brief(
        symbol=symbol,
        classification=data["classification"],
        catalyst=data["catalyst"],
        key_dates=tuple(
            KeyDate(day=k["date"], event=k["event"]) for k in data["key_dates"]
        ),
        confidence=data["confidence"],
        note=data["note"],
        headlines=headlines,
        fetched_at=datetime.now(UTC),
    )


def news_query(symbol: str, description: str | None = None) -> str:
    """A search string for a ticker. Bare three-letter tickers collide with
    ordinary words, so the company name carries the query when it is known."""
    if not description:
        return f"{symbol} stock"
    cleaned = re.sub(r"[^\w\s&-]", " ", description)
    # Only trailing legal suffixes are dropped. Stripping these words
    # anywhere would maim names that contain them ("First Trust", "Dow Inc").
    cleaned = re.sub(
        r"\s+(inc|corp|corporation|company|co|ltd|limited|plc|llc|sa|nv|ag)\s*$",
        "",
        cleaned.strip(),
        flags=re.IGNORECASE,
    )
    cleaned = " ".join(cleaned.split())
    return f"{cleaned or symbol} {symbol}"


def brief_for(
    symbol: str,
    description: str | None = None,
    today: date | None = None,
    client=None,
) -> Brief:
    """Headlines plus classification for one symbol."""
    try:
        headlines = fetch_headlines(news_query(symbol, description))
    except Exception as exc:
        return _unreadable(symbol, (), f"headline fetch failed: {exc}")
    return classify(symbol, headlines, today=today, client=client)
