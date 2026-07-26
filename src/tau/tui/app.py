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

from tau import chain as chain_mod
from tau import propose as propose_mod
from tau import screen
from tau.propose import Proposal
from tau.screen import Candidate
from tau.session import get_session
from tau.tui.detail import DetailPane

ChainLoader = Callable[[Candidate], Awaitable[chain_mod.Cycle]]
Loader = Callable[[], Awaitable[list[Candidate]]]
ProposalLoader = Callable[[list[Candidate], Callable[[Proposal], None]], Awaitable[None]]

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

RANK_COLUMNS = ("", "SYM", "DTE", "CREDIT", "BPR~", "ROC%", "ANN%", "POP%", "SPRD%", "BE/EM")
# (label, Proposal attribute, higher-is-better) — build_rank_rows() looks up
# the attribute per candidate's cached Proposal; "symbol" is handled as a
# literal case rather than a Proposal attribute.
RANK_SORTS = (
    ("ANN%", "annualized_roc", True),
    ("ROC%", "roc", True),
    ("POP%", "pop", True),
    ("SPRD%", "spread_cost", False),
    ("SYM", "symbol", False),
)


async def fetch_candidates() -> list[Candidate]:
    metrics = await screen.fetch_metrics(get_session(), _universe())
    today = date.today()
    return [screen.parse(m, today) for m in metrics]


async def fetch_cycle_for(candidate: Candidate) -> chain_mod.Cycle:
    """The metrics pull already knows this name's 30-day IV, so the strike
    window is sized without paying for a probe."""
    hint = candidate.iv30 / 100 if candidate.iv30 else None
    return await chain_mod.fetch_cycle(get_session(), candidate.symbol, iv_hint=hint)


async def price_shortlist(
    candidates: list[Candidate], on_done: Callable[[Proposal], None]
) -> None:
    await propose_mod.price_many(get_session(), candidates, on_done=on_done)


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
    #table { width: 1fr; height: 1fr; }
    #detail {
        width: 46; height: 1fr; padding: 0 1;
        border-left: solid $panel; overflow-y: auto;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        # Enter also works, via the table's RowSelected; it can't be bound
        # here because the focused table consumes it before the app sees it,
        # which would leave the action undiscoverable in the footer.
        Binding("c", "load_chain", "chain"),
        Binding("r", "refresh", "refresh"),
        Binding("s", "sort", "sort"),
        Binding("x", "toggle_excluded", "excluded"),
        Binding("space", "star", "star"),
        Binding("left_square_bracket", "ivr_down", "IVR-"),
        Binding("right_square_bracket", "ivr_up", "IVR+"),
        Binding("l", "cycle_liquidity", "liq"),
        Binding("e", "cycle_earnings", "ern"),
        Binding("p", "rank_shortlist", "rank"),
        Binding("R", "reprice", "re-price", show=False),
        Binding("escape", "back_to_screen", "screen", show=False),
    ]

    min_ivr: reactive[float] = reactive(30.0)
    min_liquidity: reactive[int] = reactive(3)
    earnings_days: reactive[int] = reactive(45)
    sort_index: reactive[int] = reactive(0)
    show_excluded: reactive[bool] = reactive(False)
    mode: reactive[str] = reactive("screen")  # "screen" | "rank"
    rank_sort_index: reactive[int] = reactive(0)

    def __init__(
        self,
        loader: Loader | None = None,
        chain_loader: ChainLoader | None = None,
        proposal_loader: ProposalLoader | None = None,
    ) -> None:
        super().__init__()
        self._loader: Loader = loader or fetch_candidates
        self._chain_loader: ChainLoader = chain_loader or fetch_cycle_for
        self._proposal_loader: ProposalLoader = proposal_loader or price_shortlist
        self._raw: list[Candidate] = []
        self._rows: list[Candidate] = []
        self._starred: set[str] = set()
        self._fetched_at: datetime | None = None
        self._status = "loading…"
        # Cycles are kept per symbol so returning to a name is instant; the
        # timestamp travels with them so a stale quote can't pass as live.
        self._cycles: dict[str, chain_mod.Cycle] = {}
        self._detail_status = ""
        # Proposals persist across a rank-mode exit/re-entry so toggling back
        # and forth never re-fetches; only `R` forces a re-price.
        self._proposals: dict[str, Proposal] = {}
        self._passing: list[Candidate] = []  # this screen's pass set, unsorted by rank
        self._rank_rows: list[Candidate] = []
        self._pricing = False
        self._columns_mode = "screen"  # tracks which header row the table wears

    def compose(self) -> ComposeResult:
        yield Static("", id="meta")
        with Horizontal():
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            yield DetailPane("", id="detail")
        yield Static("", id="filters")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns(*COLUMNS)
        self._columns_mode = "screen"
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
        # A refetch invalidates prior quotes/greeks — stale numbers presented
        # as fresh would be worse than an empty rank view.
        self._proposals.clear()
        self._cycles.clear()
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
        self._passing = [c for c in scored if c.passed]
        rows = scored if self.show_excluded else self._passing
        _, key = SORTS[self.sort_index]
        self._rows = sorted(rows, key=key)
        self._passed = len(self._passing)
        self.build_rank_rows()
        self.render_current_table()
        self.refresh_meta()

    def build_rank_rows(self) -> None:
        """Sort the current pass set by the active rank metric. A candidate
        with no proposal yet (or one that failed to price) sorts last rather
        than vanishing — the rank view is the same shortlist, just ordered
        differently once numbers exist."""
        label, attr, desc = RANK_SORTS[self.rank_sort_index]

        def keyfn(c: Candidate):
            if attr == "symbol":
                return (False, c.symbol)
            p = self._proposals.get(c.symbol)
            if p is None or not p.ok:
                return (True, 0.0)
            value = getattr(p, attr)
            if value is None:
                return (True, 0.0)
            return (False, -value if desc else value)

        self._rank_rows = sorted(self._passing, key=keyfn)

    def render_current_table(self) -> None:
        if self.mode == "rank":
            self.render_rank_table()
        else:
            self.render_screen_table()

    def render_screen_table(self) -> None:
        table = self.query_one("#table", DataTable)
        cursor = table.cursor_row
        if self._columns_mode != "screen":
            table.clear(columns=True)
            table.add_columns(*COLUMNS)
            self._columns_mode = "screen"
        else:
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
        self.render_detail()

    def render_rank_table(self) -> None:
        table = self.query_one("#table", DataTable)
        cursor = table.cursor_row
        if self._columns_mode != "rank":
            table.clear(columns=True)
            table.add_columns(*RANK_COLUMNS)
            self._columns_mode = "rank"
        else:
            table.clear()
        for c in self._rank_rows:
            p = self._proposals.get(c.symbol)
            star = c.symbol in self._starred
            if p is None:
                marker = "★" if star else "…"
                table.add_row(marker, c.symbol, *(["—"] * 8), key=c.symbol)
                continue
            if not p.ok:
                marker = "★" if star else "✗"
                table.add_row(marker, c.symbol, *(["—"] * 8), key=c.symbol)
                continue
            marker = "★" if star else "·"
            table.add_row(
                marker,
                c.symbol,
                f"{p.cycle.dte}d",
                _fmt(p.credit),
                _fmt(p.bpr, ",.0f"),
                _fmt(p.roc * 100 if p.roc is not None else None, ".1f"),
                _fmt(p.annualized_roc * 100 if p.annualized_roc is not None else None, ".0f"),
                _fmt(p.pop * 100 if p.pop is not None else None, ".0f"),
                _fmt(p.spread_cost * 100 if p.spread_cost is not None else None, ".0f"),
                _fmt(p.be_over_em, ".2f"),
                key=c.symbol,
            )
        if self._rank_rows:
            table.move_cursor(row=min(cursor, len(self._rank_rows) - 1))
        self.render_detail()

    def render_detail(self) -> None:
        c = self.selected
        cycle = self._cycles.get(c.symbol) if c else None
        self.query_one("#detail", DetailPane).show(
            c, cycle, status=self._detail_status
        )

    def on_data_table_row_highlighted(self, _: DataTable.RowHighlighted) -> None:
        self._detail_status = ""
        self.render_detail()

    def on_data_table_row_selected(self, _: DataTable.RowSelected) -> None:
        # Enter reaches the focused table as a row selection, never the
        # app-level binding, so the chain load hangs off this instead.
        self.action_load_chain()

    @work(exclusive=True)
    async def load_chain(self, candidate: Candidate) -> None:
        self._detail_status = f"loading {candidate.symbol} chain…"
        self.render_detail()
        try:
            cycle = await self._chain_loader(candidate)
            self._cycles[candidate.symbol] = cycle
            self._detail_status = ""
        except Exception as exc:
            self._detail_status = f"chain failed: {exc}"
        # The cursor may have moved on; only repaint what is selected now.
        self.render_detail()

    def action_load_chain(self) -> None:
        c = self.selected
        if c is not None:
            self.load_chain(c)

    @work(exclusive=True)
    async def price_shortlist_worker(self, candidates: list[Candidate]) -> None:
        self._pricing = True
        self.refresh_meta()

        def on_done(p: Proposal) -> None:
            self._proposals[p.symbol] = p
            if p.ok:
                # Reuses the same cache 'c' reads from, so a name priced
                # here needs no separate chain fetch if you inspect it.
                self._cycles[p.symbol] = p.cycle
            self.build_rank_rows()
            self.render_current_table()
            self.refresh_meta()

        try:
            await self._proposal_loader(candidates, on_done)
        except Exception as exc:
            self._status = f"pricing failed: {exc}"
        self._pricing = False
        self.refresh_meta()

    def action_rank_shortlist(self) -> None:
        self.mode = "rank"
        self.build_rank_rows()
        self.render_current_table()
        self.refresh_meta()
        unpriced = [c for c in self._passing if c.symbol not in self._proposals]
        if unpriced and not self._pricing:
            self.price_shortlist_worker(unpriced)

    def action_reprice(self) -> None:
        self.mode = "rank"
        for c in self._passing:
            self._proposals.pop(c.symbol, None)
        self.build_rank_rows()
        self.render_current_table()
        if not self._pricing:
            self.price_shortlist_worker(list(self._passing))

    def action_back_to_screen(self) -> None:
        if self.mode == "rank":
            self.mode = "screen"
            self.render_current_table()
            self.refresh_meta()

    # ---- chrome ----

    def refresh_meta(self) -> None:
        fetched = (
            self._fetched_at.astimezone().strftime("%H:%M")
            if self._fetched_at
            else "—"
        )
        passed = getattr(self, "_passed", 0)
        if self.mode == "rank":
            priced = sum(1 for c in self._passing if c.symbol in self._proposals)
            bits = [f"tau · rank view · {priced}/{len(self._passing)} priced"]
            if self._pricing:
                bits.append("pricing…")
            bits += [f"★ {len(self._starred)}", f"fetched {fetched}"]
        else:
            bits = [
                f"tau · {passed}/{len(self._raw)} pass",
                f"showing {len(self._rows)}"
                + (" (incl. excluded)" if self.show_excluded else ""),
                f"★ {len(self._starred)}",
                f"fetched {fetched}",
            ]
        if self._status:
            bits.append(self._status)
        self.query_one("#meta", Static).update("  ·  ".join(bits))

        if self.mode == "rank":
            label = RANK_SORTS[self.rank_sort_index][0]
            self.query_one("#filters", Static).update(
                f"sort {label}  ·  R force re-price all  ·  esc back to screen"
            )
        else:
            self.query_one("#filters", Static).update(
                f"IVR ≥ {self.min_ivr:.0f}   liquidity ≥ {self.min_liquidity}   "
                f"earnings > {self.earnings_days}d   sort {SORTS[self.sort_index][0]}"
            )

    # ---- actions ----

    def action_refresh(self) -> None:
        self.load()

    def action_sort(self) -> None:
        if self.mode == "rank":
            self.rank_sort_index = (self.rank_sort_index + 1) % len(RANK_SORTS)
            self.build_rank_rows()
            self.render_current_table()
            self.refresh_meta()
        else:
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
        self.render_current_table()
        self.refresh_meta()

    @property
    def current_rows(self) -> list[Candidate]:
        return self._rank_rows if self.mode == "rank" else self._rows

    @property
    def selected(self) -> Candidate | None:
        table = self.query_one("#table", DataTable)
        rows = self.current_rows
        if not rows or table.cursor_row < 0:
            return None
        return rows[min(table.cursor_row, len(rows) - 1)]


def run() -> None:
    TauApp().run()
