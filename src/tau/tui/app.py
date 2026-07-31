"""The scan TUI — a triage loop over the screen.

One metrics pull feeds every view: raw `Candidate`s are held unfiltered, so
moving a threshold re-filters and re-ranks in memory with no API call. That
is the whole reason this exists rather than re-running `tau scan` with new
flags.

The app takes its data through a `loader` callable so tests (and a future
cached mode) can drive it without the network.
"""

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime

from tastytrade.instruments import Equity
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Input, Static

from tau import catalyst as catalyst_mod
from tau import chain as chain_mod
from tau import history as history_mod
from tau import portfolio as portfolio_mod
from tau import propose as propose_mod
from tau import screen
from tau.propose import Proposal
from tau.screen import Candidate
from tau.session import get_session
from tau.tui.detail import DetailPane
from tau.tui.help import HelpScreen

ChainLoader = Callable[..., Awaitable[chain_mod.Cycle]]
Loader = Callable[[], Awaitable[list[Candidate]]]
ProposalLoader = Callable[..., Awaitable[None]]
HistoryLoader = Callable[[Candidate], Awaitable[history_mod.History]]
BriefLoader = Callable[[Candidate], Awaitable[catalyst_mod.Brief]]
BookLoader = Callable[[], Awaitable[portfolio_mod.Book]]

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

RANK_COLUMNS = (
    "", "SYM", "DTE", "CREDIT", "BPR~", "ROC%", "ANN%",
    "θ/DAY", "θ/BPR", "POP%", "SPRD%", "BE/EM",
)
# %NL is a property of the proposed trade — what it would cost this account —
# so it belongs with the other per-trade numbers, and only exists once the
# account is known.
RANK_NL_AFTER = RANK_COLUMNS.index("ROC%")

# The positions page answers one question — is it time to take this off — so
# it carries the two figures the usual management rules are written in, P/L
# against the credit received and days left, and little else.
POSITION_COLUMNS = (
    "SYM", "STRUCTURE", "DTE", "CREDIT", "NOW", "P/L", "P/L%", "BPR", "HELD",
)
# (label, Proposal attribute, higher-is-better) — build_rank_rows() looks up
# the attribute per candidate's cached Proposal; "symbol" is handled as a
# literal case rather than a Proposal attribute.
RANK_SORTS = (
    ("ANN%", "annualized_roc", True),
    ("θ/BPR", "theta_yield", True),
    ("ROC%", "roc", True),
    ("POP%", "pop", True),
    ("SPRD%", "spread_cost", False),
    ("SYM", "symbol", False),
)


def _insert(columns: tuple[str, ...], at: int, label: str) -> tuple[str, ...]:
    return columns[:at] + (label,) + columns[at:]


async def fetch_candidates() -> list[Candidate]:
    metrics = await screen.fetch_metrics(get_session(), _universe())
    today = date.today()
    return [screen.parse(m, today) for m in metrics]


async def fetch_cycle_for(
    candidate: Candidate,
    expiration: date | None = None,
    target_dte: int = chain_mod.TARGET_DTE,
) -> chain_mod.Cycle:
    """The metrics pull already knows this name's 30-day IV, so the strike
    window is sized without paying for a probe. `expiration` pins a specific
    cycle, which is what walking the term structure with `<`/`>` does."""
    hint = candidate.iv30 / 100 if candidate.iv30 else None
    return await chain_mod.fetch_cycle(
        get_session(),
        candidate.symbol,
        target_dte=target_dte,
        expiration=expiration,
        iv_hint=hint,
    )


async def price_shortlist(
    candidates: list[Candidate],
    on_done: Callable[[Proposal], None],
    target_dte: int = chain_mod.TARGET_DTE,
    target_delta: float = chain_mod.TARGET_DELTA,
) -> None:
    await propose_mod.price_many(
        get_session(),
        candidates,
        target_dte=target_dte,
        target_delta=target_delta,
        on_done=on_done,
    )


async def fetch_book() -> portfolio_mod.Book:
    return await portfolio_mod.fetch_book(
        get_session(), os.environ.get("TAU_ACCOUNT")
    )


async def fetch_history_for(candidate: Candidate) -> history_mod.History:
    return await history_mod.fetch_history(get_session(), candidate.symbol)


async def fetch_brief_for(candidate: Candidate) -> catalyst_mod.Brief:
    """The company name makes a far better news query than a bare ticker,
    which collides with ordinary words, but it is not worth failing over."""
    description: str | None = None
    try:
        equity = await Equity.get(get_session(), candidate.symbol)
        description = equity.description
    except Exception:
        pass
    # Headline fetch and the model call are both blocking; keep them off the
    # event loop or the TUI freezes for the duration.
    return await asyncio.to_thread(
        catalyst_mod.brief_for, candidate.symbol, description
    )


def _universe() -> list[str]:
    from tau import universe

    return universe.load_universe(None)


def _fmt(value, spec: str = ".1f") -> str:
    return "—" if value is None else format(value, spec)


def _colour_pnl(value, spec: str) -> str:
    """Green up, red down. The one place in the app colour carries data
    rather than warning — a position's P/L is the only number here that is
    already an outcome rather than an estimate of one."""
    if value is None:
        return "—"
    text = format(value, spec)
    if value > 0:
        return f"[green]+{text}[/green]"
    if value < 0:
        return f"[red]{text}[/red]"
    return text


class TauApp(App):
    """Master list of screen candidates with live re-filtering."""

    TITLE = "tau"
    # Without this the hidden search box wins the initial focus — it is the
    # only Input in the tree — and every key goes into it instead of the
    # table.
    AUTO_FOCUS = "#table"

    CSS = """
    #meta { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    #filters { height: 1; padding: 0 1; color: $text-muted; }
    #table { width: 1fr; height: 1fr; }
    #detail {
        width: 46; height: 1fr; padding: 0 1;
        border-left: solid $panel; overflow-y: auto;
    }
    /* Borderless so the search bar costs one row, not three — it sits over
       the filter line and the table must not jump when it opens. */
    #search {
        height: 1; padding: 0 1; border: none; background: $panel;
        display: none;
    }
    #search.open { display: block; }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("question_mark", "help", "help"),
        # Enter also works, via the table's RowSelected; it can't be bound
        # here because the focused table consumes it before the app sees it,
        # which would leave the action undiscoverable in the footer.
        Binding("c", "load_chain", "chain"),
        Binding("w", "load_why", "why"),
        Binding("slash", "search", "jump"),
        Binding("r", "refresh", "refresh"),
        Binding("s", "sort", "sort"),
        Binding("x", "toggle_excluded", "excluded"),
        Binding("space", "star", "star"),
        Binding("left_square_bracket", "ivr_down", "IVR-"),
        Binding("right_square_bracket", "ivr_up", "IVR+"),
        Binding("l", "cycle_liquidity", "liq"),
        Binding("e", "cycle_earnings", "ern"),
        Binding("p", "positions", "positions"),
        Binding("P", "rank_shortlist", "rank"),
        Binding("R", "reprice", "re-price", show=False),
        # Both spellings: the shifted form is what the pane labels, the bare
        # one is what a hand reaches for.
        Binding("less_than_sign", "prev_expiration", "exp-", show=False),
        Binding("greater_than_sign", "next_expiration", "exp+", show=False),
        Binding("comma", "prev_expiration", "exp-", show=False),
        Binding("full_stop", "next_expiration", "exp+", show=False),
        Binding("escape", "back_to_screen", "screen", show=False),
    ]

    min_ivr: reactive[float] = reactive(30.0)
    min_liquidity: reactive[int] = reactive(3)
    earnings_days: reactive[int] = reactive(45)
    sort_index: reactive[int] = reactive(0)
    show_excluded: reactive[bool] = reactive(False)
    # "screen" and "rank" are two column sets over the same candidates;
    # "positions" is a different row set entirely — open trades, not screened
    # names — which is why it is a page rather than another sort.
    mode: reactive[str] = reactive("screen")  # "screen" | "rank" | "positions"
    rank_sort_index: reactive[int] = reactive(0)

    def __init__(
        self,
        loader: Loader | None = None,
        chain_loader: ChainLoader | None = None,
        proposal_loader: ProposalLoader | None = None,
        history_loader: HistoryLoader | None = None,
        brief_loader: BriefLoader | None = None,
        book_loader: BookLoader | None = None,
    ) -> None:
        super().__init__()
        self._loader: Loader = loader or fetch_candidates
        self._chain_loader: ChainLoader = chain_loader or fetch_cycle_for
        self._proposal_loader: ProposalLoader = proposal_loader or price_shortlist
        self._history_loader: HistoryLoader = history_loader or fetch_history_for
        self._brief_loader: BriefLoader = brief_loader or fetch_brief_for
        self._book_loader: BookLoader | None = book_loader or fetch_book
        # The account, if it can be read. None means "not known", which is
        # deliberately distinct from "flat" — the position columns stay
        # hidden rather than showing dashes that read as no exposure.
        self._book: portfolio_mod.Book | None = None
        self._book_error = ""
        # `/` jumps rather than filters, so the only state it needs is where
        # the cursor was when the search opened — to put it back on cancel.
        self._jump_origin = 0
        self._jump_miss = ""
        self._trades: list[portfolio_mod.Trade] = []
        self._all_scored: list[Candidate] = []
        self._raw: list[Candidate] = []
        self._rows: list[Candidate] = []
        self._starred: set[str] = set()
        self._fetched_at: datetime | None = None
        self._status = "loading…"
        # Cycles are kept per symbol so returning to a name is instant; the
        # timestamp travels with them so a stale quote can't pass as live.
        self._cycles: dict[str, chain_mod.Cycle] = {}
        self._detail_status = ""
        # Price context and the catalyst read, cached per symbol: the first
        # is a websocket round trip, the second costs a model call.
        self._history: dict[str, history_mod.History] = {}
        self._briefs: dict[str, catalyst_mod.Brief] = {}
        # Its own status line — one shared string would let the chain load
        # and this one overwrite each other's message.
        self._why_status = ""
        # Proposals persist across a rank-mode exit/re-entry so toggling back
        # and forth never re-fetches; only `R` forces a re-price.
        self._proposals: dict[str, Proposal] = {}
        self._passing: list[Candidate] = []  # this screen's pass set, unsorted by rank
        self._rank_rows: list[Candidate] = []
        self._pricing = False
        # Which header row the table wears. The book can arrive after the
        # first paint and adds a column, so the key carries both facts.
        self._columns_mode: tuple[str, bool] | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="meta")
        with Horizontal():
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            yield DetailPane("", id="detail")
        yield Input(placeholder="filter symbols…", id="search")
        yield Static("", id="filters")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_meta()
        self.load()
        self.load_book()

    # ---- columns ----

    @property
    def has_book(self) -> bool:
        return self._book is not None

    def _screen_columns(self) -> tuple[str, ...]:
        return COLUMNS

    def _rank_columns(self) -> tuple[str, ...]:
        # Sizing, not exposure: in the rank view the account's contribution is
        # what share of it each proposal would consume.
        if not self.has_book:
            return RANK_COLUMNS
        return _insert(RANK_COLUMNS, RANK_NL_AFTER, "%NL")

    def _sync_columns(self, mode: str, columns: tuple[str, ...]) -> DataTable:
        """Repaint the header only when it actually changes — clearing
        columns resets the cursor, so doing it every render would fight the
        user's selection."""
        table = self.query_one("#table", DataTable)
        key = (mode, self.has_book)
        if self._columns_mode != key:
            table.clear(columns=True)
            table.add_columns(*columns)
            self._columns_mode = key
        else:
            table.clear()
        return table

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
        # Same reasoning for the price and catalyst reads: a brief taken
        # before the refresh describes the market as it was, and re-reading
        # costs nothing until the user asks for it again with `w`.
        self._history.clear()
        self._briefs.clear()
        self.rebuild()

    @work(exclusive=True, group="book")
    async def load_book(self) -> None:
        """The account, alongside the screen rather than before it.

        Deliberately not awaited by `load`: a grant without account access is
        a normal configuration, and the screen must not wait on — or fail
        for — a call that only enriches it."""
        if self._book_loader is None:
            return
        try:
            self._book = await self._book_loader()
            self._book_error = ""
        except Exception as exc:
            self._book = None
            self._book_error = f"account unavailable: {exc}"
        self.build_position_rows()
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
        self._all_scored = scored
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
        if self.mode == "positions":
            self.render_positions_table()
        elif self.mode == "rank":
            self.render_rank_table()
        else:
            self.render_screen_table()

    def build_position_rows(self) -> None:
        """Open trades, soonest to expire first.

        Time is the axis that matters on this page — a trade at 12 DTE wants
        a decision today whether or not it is the biggest winner — so it is
        the default order rather than something to sort into."""
        trades = list(self._book.trades) if self._book is not None else []
        today = date.today()
        self._trades = sorted(
            trades,
            key=lambda t: (t.dte(today) is None, t.dte(today) or 0, t.underlying),
        )

    def render_positions_table(self) -> None:
        table = self._sync_columns("positions", POSITION_COLUMNS)
        cursor = table.cursor_row
        today = date.today()
        for t in self._trades:
            dte = t.dte(today)
            held = t.days_held(today)
            pnl, pct = t.pnl, t.pnl_pct
            table.add_row(
                t.underlying,
                t.describe(),
                "—" if dte is None else f"{dte}d",
                _fmt(t.credit, ",.0f"),
                _fmt(t.value, ",.0f"),
                _colour_pnl(pnl, ",.0f"),
                _colour_pnl(pct * 100 if pct is not None else None, ".0f"),
                _fmt(self._book.requirement(t.underlying) if self._book else None, ",.0f"),
                "—" if held is None else f"{held}d",
                key=f"{t.underlying}:{t.expiration}",
            )
        if self._trades:
            table.move_cursor(row=min(max(cursor, 0), len(self._trades) - 1))
        self.render_detail()

    def _marker(self, symbol: str, state: str) -> str:
        """The leftmost cell: starred, or held, or plain state.

        Existing short premium wins the slot over a star. A star is a note
        to yourself; already being short the name is a fact that changes
        whether the row in front of you is a trade at all."""
        if self._book is not None and self._book.short_premium_in(symbol):
            return "[yellow]\u25c6[/yellow]"
        return "\u2605" if symbol in self._starred else state

    def render_screen_table(self) -> None:
        table = self._sync_columns("screen", self._screen_columns())
        cursor = table.cursor_row
        today = date.today()
        for c in self._rows:
            dte = c.days_to_earnings(today)
            cells = [
                self._marker(c.symbol, "·" if c.passed else "✗"),
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
            ]
            table.add_row(*cells, key=c.symbol)
        if self._rows:
            table.move_cursor(row=min(cursor, len(self._rows) - 1))
        self.render_detail()

    def render_rank_table(self) -> None:
        columns = self._rank_columns()
        table = self._sync_columns("rank", columns)
        cursor = table.cursor_row
        blanks = len(columns) - 2  # every column after the marker and symbol
        for c in self._rank_rows:
            p = self._proposals.get(c.symbol)
            if p is None or not p.ok:
                state = "…" if p is None else "✗"
                marker = self._marker(c.symbol, state)
                table.add_row(marker, c.symbol, *(["—"] * blanks), key=c.symbol)
                continue
            cells = [
                self._marker(c.symbol, "·"),
                c.symbol,
                f"{p.cycle.dte}d",
                _fmt(p.credit),
                _fmt(p.bpr, ",.0f"),
                _fmt(p.roc * 100 if p.roc is not None else None, ".1f"),
                _fmt(p.annualized_roc * 100 if p.annualized_roc is not None else None, ".0f"),
                _fmt(p.theta_day, ",.2f"),
                _fmt(
                    p.theta_yield * 100 if p.theta_yield is not None else None,
                    ".2f",
                ),
                _fmt(p.pop * 100 if p.pop is not None else None, ".0f"),
                _fmt(p.spread_cost * 100 if p.spread_cost is not None else None, ".0f"),
                _fmt(p.be_over_em, ".2f"),
            ]
            if self.has_book:
                share = self._book.pct_of_net_liq(p.bpr)
                cells.insert(
                    RANK_NL_AFTER,
                    _fmt(share * 100 if share is not None else None, ".1f"),
                )
            table.add_row(*cells, key=c.symbol)
        if self._rank_rows:
            table.move_cursor(row=min(cursor, len(self._rank_rows) - 1))
        self.render_detail()

    def render_detail(self) -> None:
        if self.mode == "positions":
            self.query_one("#detail", DetailPane).show_trade(
                self.selected, self._book
            )
            return
        c = self.selected_candidate
        cycle = self._cycles.get(c.symbol) if c else None
        status = self._detail_status
        if not status and self.mode == "rank" and c is not None:
            p = self._proposals.get(c.symbol)
            if p is not None and not p.ok and p.error:
                status = f"pricing failed: {p.error}"
        self.query_one("#detail", DetailPane).show(
            c,
            cycle,
            status=status,
            history=self._history.get(c.symbol) if c else None,
            brief=self._briefs.get(c.symbol) if c else None,
            why_status=self._why_status,
            target_delta=chain_mod.TARGET_DELTA,
            book=self._book,
        )

    def on_data_table_row_highlighted(self, _: DataTable.RowHighlighted) -> None:
        self._detail_status = ""
        self._why_status = ""
        self.render_detail()

    def on_data_table_row_selected(self, _: DataTable.RowSelected) -> None:
        # Enter reaches the focused table as a row selection, never the
        # app-level binding, so the chain load hangs off this instead.
        self.action_load_chain()

    @work(exclusive=True)
    async def load_chain(
        self, candidate: Candidate, expiration: date | None = None
    ) -> None:
        when = f" {expiration}" if expiration else ""
        self._detail_status = f"loading {candidate.symbol}{when} chain…"
        self.render_detail()
        try:
            cycle = await self._chain_loader(
                candidate, expiration, chain_mod.TARGET_DTE
            )
            self._cycles[candidate.symbol] = cycle
            self._detail_status = ""
        except Exception as exc:
            self._detail_status = f"chain failed: {exc}"
        # The cursor may have moved on; only repaint what is selected now.
        self.render_detail()

    def action_load_chain(self) -> None:
        c = self.selected_candidate
        if c is not None:
            self.load_chain(c)

    def _step_expiration(self, step: int) -> None:
        """Walk the loaded name's monthly cycles.

        The chain fetch already returns every live monthly, so moving along
        the term structure asks the same question at a different tenor rather
        than starting over — which is the point: 45 DTE is a default, not a
        finding, and whether it is the right cycle is visible only next to
        its neighbours."""
        c = self.selected_candidate
        cycle = self._cycles.get(c.symbol) if c else None
        if c is None or cycle is None:
            self._detail_status = "load a chain first (c)"
            self.render_detail()
            return
        dates = [d for d, _ in cycle.expirations]
        if cycle.expiration not in dates:
            return
        index = dates.index(cycle.expiration) + step
        if not 0 <= index < len(dates):
            edge = "nearest" if step < 0 else "furthest"
            self._detail_status = f"already at the {edge} monthly"
            self.render_detail()
            return
        self.load_chain(c, dates[index])

    def action_prev_expiration(self) -> None:
        self._step_expiration(-1)

    def action_next_expiration(self) -> None:
        self._step_expiration(1)

    # Its own worker group: the default group is shared, so an ungrouped
    # exclusive worker here would cancel an in-flight chain load.
    @work(exclusive=True, group="why")
    async def load_why(self, candidate: Candidate) -> None:
        """Price position and the catalyst read, together. They run
        concurrently because the price side lands in about a second and the
        model call takes a good deal longer; waiting on both to show either
        would make the fast half feel slow."""
        symbol = candidate.symbol
        self._why_status = f"reading {symbol}…"
        self.render_detail()

        history_task = asyncio.create_task(self._history_loader(candidate))
        brief_task = asyncio.create_task(self._brief_loader(candidate))
        failures: list[str] = []
        try:
            try:
                self._history[symbol] = await history_task
            except Exception as exc:
                failures.append(f"history failed: {exc}")
            # Repaint so the price context appears without waiting on the
            # model; the cursor may have moved, and render_detail re-reads it.
            self._why_status = f"classifying {symbol}…"
            self.render_detail()
            try:
                self._briefs[symbol] = await brief_task
            except Exception as exc:
                failures.append(f"catalyst failed: {exc}")
        finally:
            # On cancellation (a second keypress) neither task should outlive
            # the worker that owns it.
            for task in (history_task, brief_task):
                if not task.done():
                    task.cancel()
        self._why_status = " · ".join(failures)
        self.render_detail()

    def action_load_why(self) -> None:
        c = self.selected_candidate
        if c is None:
            return
        if c.symbol in self._history and c.symbol in self._briefs:
            return  # already read; both are cached per symbol
        self.load_why(c)

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
            await self._proposal_loader(
                candidates, on_done, chain_mod.TARGET_DTE, chain_mod.TARGET_DELTA
            )
        except Exception as exc:
            self._status = f"pricing failed: {exc}"
        self._pricing = False
        self.refresh_meta()

    def action_positions(self) -> None:
        """The open book. Its own page because its rows are its own thing —
        trades you hold, not names you are considering — and mixing the two
        would make every column mean two things."""
        self.mode = "positions"
        self.build_position_rows()
        self.render_current_table()
        self.refresh_meta()
        if self._book is None and not self._book_error:
            self.load_book()

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
        # Escape unwinds one layer at a time, innermost first: an open jump
        # is closer to hand than the page, so cancelling one never also
        # costs you the page you were on.
        if self.searching:
            self._close_search(restore=True)
            return
        if self.mode != "screen":
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
        if self.mode == "positions":
            bits = [f"tau · positions · {len(self._trades)} open"]
            if self._book is not None and self._book.net_liq:
                pnl = sum(t.pnl or 0 for t in self._trades)
                bits.append(f"P/L {pnl:+,.0f}")
        elif self.mode == "rank":
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
        if self._jump_miss:
            bits.append(f"[yellow]{self._jump_miss}[/yellow]")
        bits.append(self._book_summary())
        if self._status:
            bits.append(self._status)
        self.query_one("#meta", Static).update("  ·  ".join(b for b in bits if b))

        structure = (
            f"{chain_mod.TARGET_DELTA:.2f}Δ @ {chain_mod.TARGET_DTE}d"
        )
        if self.mode == "positions":
            self.query_one("#filters", Static).update(
                "r refresh  ·  / jump  ·  ? help  ·  esc back to screener"
            )
        elif self.mode == "rank":
            label = RANK_SORTS[self.rank_sort_index][0]
            self.query_one("#filters", Static).update(
                f"{structure}   sort {label}  ·  R force re-price all  ·  "
                f"? help  ·  esc back to screener"
            )
        else:
            self.query_one("#filters", Static).update(
                f"IVR ≥ {self.min_ivr:.0f}   liquidity ≥ {self.min_liquidity}   "
                f"earnings > {self.earnings_days}d   sort {SORTS[self.sort_index][0]}"
                f"   {structure}   p positions  ·  ? help"
            )

    def _book_summary(self) -> str:
        """Net liq and how much of it is already committed. Shown because
        every %NL figure in the rank view is measured against it, and a
        denominator that is invisible is a denominator nobody checks."""
        if self._book is None:
            return self._book_error
        if self._book.net_liq is None:
            return ""
        used = self._book.utilization
        text = f"NL {self._book.net_liq:,.0f}"
        if used is not None:
            text += f" ({used:.0%} used)"
        return text

    # ---- actions ----

    def action_refresh(self) -> None:
        # The account moves for reasons that have nothing to do with the
        # screen — a fill, an assignment — so a refresh that re-read the
        # market but not the book would leave the position columns quietly
        # describing a book that no longer exists.
        self.load()
        self.load_book()

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

    # ---- jump ----

    def action_search(self) -> None:
        """`/` moves the cursor; it does not hide rows.

        Filtering answers "show me only these", which is what the thresholds
        already do. Typing a ticker asks "take me to this one" — and the rows
        either side of it are the context you were reading the list for, so
        throwing them away to honour the request would be answering a
        different question."""
        self._jump_origin = max(self.query_one("#table", DataTable).cursor_row, 0)
        self._jump_miss = ""
        box = self.query_one("#search", Input)
        box.value = ""
        box.add_class("open")
        box.focus()

    def _close_search(self, restore: bool) -> None:
        if restore and self.current_rows:
            self.query_one("#table", DataTable).move_cursor(
                row=min(self._jump_origin, len(self.current_rows) - 1)
            )
        self._jump_miss = ""
        box = self.query_one("#search", Input)
        box.value = ""
        box.remove_class("open")
        self.query_one("#table", DataTable).focus()
        self.render_detail()
        self.refresh_meta()

    def _jump_to(self, prefix: str) -> None:
        """Move to the first row whose symbol starts with what was typed.

        A miss is reported rather than ignored, and a name the screen
        excluded says so with its reason — otherwise typing the ticker of a
        name filtered out an hour ago looks exactly like the tool being
        broken."""
        if not prefix:
            self._jump_miss = ""
            self.refresh_meta()
            return
        rows = self.current_rows
        index = next(
            (i for i, c in enumerate(rows) if self._row_symbol(c).startswith(prefix)),
            None,
        )
        if index is not None:
            self._jump_miss = ""
            self.query_one("#table", DataTable).move_cursor(row=index)
            self.render_detail()
        else:
            self._jump_miss = self._explain_miss(prefix)
        self.refresh_meta()

    def _explain_miss(self, prefix: str) -> str:
        """Why a symbol that exists is not on screen."""
        hidden = next(
            (
                c
                for c in self._all_scored
                if c.symbol.startswith(prefix) and c.excluded
            ),
            None,
        )
        if hidden is not None:
            return f"{hidden.symbol} excluded: {'; '.join(hidden.excluded)} — x to show"
        if any(c.symbol.startswith(prefix) for c in self._raw):
            return f"{prefix} not in this view"
        return f"no match for {prefix}"

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "search":
            return
        self._jump_to(event.value.strip().upper())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "search":
            return
        # Enter accepts where the cursor landed and hands the keys back to
        # the table, so `c`/`w` act on what was jumped to.
        self._close_search(restore=False)

    @property
    def searching(self) -> bool:
        return self.query_one("#search", Input).has_focus

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_star(self) -> None:
        c = self.selected_candidate
        if c is None:
            return
        self._starred.symmetric_difference_update({c.symbol})
        self.render_current_table()
        self.refresh_meta()

    @property
    def current_rows(self) -> list:
        if self.mode == "positions":
            return self._trades
        return self._rank_rows if self.mode == "rank" else self._rows

    @staticmethod
    def _row_symbol(row) -> str:
        """The ticker a row is about, whichever page it came from."""
        return getattr(row, "symbol", None) or row.underlying

    @property
    def selected(self):
        table = self.query_one("#table", DataTable)
        rows = self.current_rows
        if not rows or table.cursor_row < 0:
            return None
        return rows[min(table.cursor_row, len(rows) - 1)]

    @property
    def selected_candidate(self) -> Candidate | None:
        """The selected row as a screen candidate, or None on a page whose
        rows are not candidates. Actions that price or classify a name take
        this rather than `selected`, so pressing `c` on an open position is
        a no-op instead of an AttributeError."""
        row = self.selected
        return row if isinstance(row, Candidate) else None


def run() -> None:
    TauApp().run()
