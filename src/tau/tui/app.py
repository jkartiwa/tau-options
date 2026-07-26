"""The scan TUI — a triage loop over the screen.

One metrics pull feeds every view: raw `Candidate`s are held unfiltered, so
moving a threshold re-filters and re-ranks in memory with no API call. That
is the whole reason this exists rather than re-running `tau scan` with new
flags.

The app takes its data through a `loader` callable so tests (and a future
cached mode) can drive it without the network.
"""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, date, datetime

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Static

from tau import screen
from tau.screen import Candidate
from tau.session import get_session

Loader = Callable[[], Awaitable[list[Candidate]]]

# Cycled by keypress rather than typed — a scanner's thresholds are coarse.
IVR_STEP = 5.0
LIQUIDITY_CYCLE = (0, 1, 2, 3, 4)
EARNINGS_CYCLE = (0, 14, 21, 45, 60)
SORTS = (
    ("IVR", lambda c: (c.ivr is None, -(c.ivr or 0))),
    ("IV/HV", lambda c: (c.iv_hv is None, -(c.iv_hv or 0))),
    ("LIQ", lambda c: (c.liquidity is None, -(c.liquidity or 0))),
    ("SYM", lambda c: (False, c.symbol)),
)

COLUMNS = ("", "SYM", "IVR", "IVP", "IV/HV", "IV30", "HV30", "LIQ", "BETA", "ERN", "WHY")


async def fetch_candidates() -> list[Candidate]:
    metrics = await screen.fetch_metrics(get_session(), _universe())
    today = date.today()
    return [screen.parse(m, today) for m in metrics]


def _universe() -> list[str]:
    from tau import universe

    return universe.load_universe(None)


def _fmt(value, spec: str = ".1f") -> str:
    return "—" if value is None else format(value, spec)


class TauApp(App):
    """Master list of screen candidates with live re-filtering."""

    TITLE = "tau"

    CSS = """
    #meta { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    #filters { height: 1; padding: 0 1; color: $text-muted; }
    DataTable { height: 1fr; }
    .warn { color: $warning; }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("r", "refresh", "refresh"),
        Binding("s", "sort", "sort"),
        Binding("x", "toggle_excluded", "excluded"),
        Binding("space", "star", "star"),
        Binding("left_square_bracket", "ivr_down", "IVR-"),
        Binding("right_square_bracket", "ivr_up", "IVR+"),
        Binding("l", "cycle_liquidity", "liq"),
        Binding("e", "cycle_earnings", "ern"),
    ]

    min_ivr: reactive[float] = reactive(30.0)
    min_liquidity: reactive[int] = reactive(3)
    earnings_days: reactive[int] = reactive(45)
    sort_index: reactive[int] = reactive(0)
    show_excluded: reactive[bool] = reactive(False)

    def __init__(self, loader: Loader | None = None) -> None:
        super().__init__()
        self._loader: Loader = loader or fetch_candidates
        self._raw: list[Candidate] = []
        self._rows: list[Candidate] = []
        self._starred: set[str] = set()
        self._fetched_at: datetime | None = None
        self._status = "loading…"

    def compose(self) -> ComposeResult:
        yield Static("", id="meta")
        with Horizontal():
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="filters")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns(*COLUMNS)
        self.refresh_meta()
        self.load()

    # ---- data ----

    @work(exclusive=True)
    async def load(self) -> None:
        self._status = "loading…"
        self.refresh_meta()
        try:
            self._raw = await self._loader()
            self._fetched_at = datetime.now(UTC)
            self._status = ""
        except Exception as exc:  # surfaced, never silently empty
            self._status = f"load failed: {exc}"
        self.rebuild()

    def rebuild(self) -> None:
        """Re-filter and re-rank from the held raw pull. No network."""
        today = date.today()
        scored = [
            screen.apply_filters(
                c,
                min_ivr=self.min_ivr,
                min_liquidity=self.min_liquidity,
                earnings_days=self.earnings_days,
                today=today,
            )
            for c in self._raw
        ]
        rows = scored if self.show_excluded else [c for c in scored if c.passed]
        _, key = SORTS[self.sort_index]
        self._rows = sorted(rows, key=key)
        self._passed = sum(1 for c in scored if c.passed)
        self.render_rows()
        self.refresh_meta()

    def render_rows(self) -> None:
        table = self.query_one("#table", DataTable)
        cursor = table.cursor_row
        table.clear()
        today = date.today()
        for c in self._rows:
            dte = c.days_to_earnings(today)
            table.add_row(
                "★" if c.symbol in self._starred else ("·" if c.passed else "✗"),
                c.symbol,
                _fmt(c.ivr, ".0f"),
                _fmt(c.ivp, ".0f"),
                _fmt(c.iv_hv, ".2f"),
                _fmt(c.iv30),
                _fmt(c.hv30),
                "—" if c.liquidity is None else str(c.liquidity),
                _fmt(c.beta, ".2f"),
                "—" if dte is None else f"{dte}d",
                "; ".join(c.excluded),
                key=c.symbol,
            )
        if self._rows:
            table.move_cursor(row=min(cursor, len(self._rows) - 1))

    # ---- chrome ----

    def refresh_meta(self) -> None:
        fetched = (
            self._fetched_at.astimezone().strftime("%H:%M")
            if self._fetched_at
            else "—"
        )
        shown = len(self._rows)
        passed = getattr(self, "_passed", 0)
        bits = [
            f"tau · {passed}/{len(self._raw)} pass",
            f"showing {shown}" + (" (incl. excluded)" if self.show_excluded else ""),
            f"★ {len(self._starred)}",
            f"fetched {fetched}",
        ]
        if self._status:
            bits.append(self._status)
        self.query_one("#meta", Static).update("  ·  ".join(bits))
        self.query_one("#filters", Static).update(
            f"IVR ≥ {self.min_ivr:.0f}   liquidity ≥ {self.min_liquidity}   "
            f"earnings > {self.earnings_days}d   sort {SORTS[self.sort_index][0]}"
        )

    # ---- actions ----

    def action_refresh(self) -> None:
        self.load()

    def action_sort(self) -> None:
        self.sort_index = (self.sort_index + 1) % len(SORTS)
        self.rebuild()

    def action_toggle_excluded(self) -> None:
        self.show_excluded = not self.show_excluded
        self.rebuild()

    def action_ivr_up(self) -> None:
        self.min_ivr = min(100.0, self.min_ivr + IVR_STEP)
        self.rebuild()

    def action_ivr_down(self) -> None:
        self.min_ivr = max(0.0, self.min_ivr - IVR_STEP)
        self.rebuild()

    def action_cycle_liquidity(self) -> None:
        i = LIQUIDITY_CYCLE.index(self.min_liquidity)
        self.min_liquidity = LIQUIDITY_CYCLE[(i + 1) % len(LIQUIDITY_CYCLE)]
        self.rebuild()

    def action_cycle_earnings(self) -> None:
        i = EARNINGS_CYCLE.index(self.earnings_days)
        self.earnings_days = EARNINGS_CYCLE[(i + 1) % len(EARNINGS_CYCLE)]
        self.rebuild()

    def action_star(self) -> None:
        c = self.selected
        if c is None:
            return
        self._starred.symmetric_difference_update({c.symbol})
        self.render_rows()
        self.refresh_meta()

    @property
    def selected(self) -> Candidate | None:
        table = self.query_one("#table", DataTable)
        if not self._rows or table.cursor_row < 0:
            return None
        return self._rows[min(table.cursor_row, len(self._rows) - 1)]


def run() -> None:
    TauApp().run()
