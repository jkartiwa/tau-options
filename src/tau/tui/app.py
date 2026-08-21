"""The scan TUI — a triage loop over the screen.

One metrics pull feeds every view: raw `Candidate`s are held unfiltered, so
moving a threshold re-filters and re-ranks in memory with no API call. That
is the whole reason this exists rather than re-running `tau scan` with new
flags.

The app takes its data through a `loader` callable so tests (and a future
cached mode) can drive it without the network.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime

from rich.text import Text
from tastytrade.instruments import Equity
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Static

from tau import catalyst as catalyst_mod
from tau import chain as chain_mod
from tau import history as history_mod
from tau import propose as propose_mod
from tau import screen
from tau.build import Structure
from tau.propose import Proposal
from tau.screen import Candidate
from tau.session import get_session
from tau.strategies import ALL as ALL_STRATEGIES
from tau.tui.detail import DetailPane
from tau.tui.picker import StrategyPicker

ChainLoader = Callable[[Candidate], Awaitable[chain_mod.Cycle]]
Loader = Callable[[], Awaitable[list[Candidate]]]
ProposalLoader = Callable[[list[Candidate], Callable[[Proposal], None]], Awaitable[None]]
HistoryLoader = Callable[[Candidate], Awaitable[history_mod.History]]
BriefLoader = Callable[[Candidate], Awaitable[catalyst_mod.Brief]]

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

# One row per symbol: the best structure any strategy found on it. STRUCTURE
# carries the strategy and the variant that won, because "SMH 480%" without
# saying which trade earned it is not actionable.
RANK_COLUMNS = (
    "", "SYM", "STRUCTURE", "BIAS", "DTE", "CREDIT", "BPR",
    "ROC%", "ANN%", "POP%", "SPRD%", "BE/EM",
)
# The drill-in: every variant considered on one name, failures included.
# Narrower than the rank view on purpose. Bias belongs to the strategy already
# named in the label, and within one symbol the tenor is fixed, so ROC% and
# ANN% order the rows identically — dropping both buys the width the failure
# reason needs, and a clipped reason reads as no reason at all.
VARIANT_COLUMNS = (
    "", "STRUCTURE", "CREDIT", "BPR", "ANN%", "POP%",
    "SPRD%", "BE/EM", "WHY NOT",
)
# (label, metric attribute, higher-is-better) — read off the winning Structure
# in rank mode and off each variant in the drill-in; "symbol" is handled as a
# literal case rather than an attribute.
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
    if value is None:
        return "—"
    if value in (float("inf"), float("-inf")):
        return "∞" if value > 0 else "-∞"
    return format(value, spec)


def _pct(value, spec: str = ".0f") -> str:
    """A rate stored as a fraction, shown as a percentage. The column headers
    already carry the % sign, so this doesn't repeat it."""
    return "—" if value is None else _fmt(value * 100, spec)


def _bpr(value, source: str) -> str:
    """Buying power with the source readable per row: broker figures plain,
    formula estimates carrying the tilde the column header used to. The
    header is `BPR` for everyone, so the row itself has to say which model
    the number came from."""
    if value is None:
        return "—"
    return _fmt(value, ",.0f") if source == "broker" else _fmt(value, ",.0f") + "~"


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
        Binding("w", "load_why", "why"),
        Binding("r", "refresh", "refresh"),
        Binding("s", "sort", "sort"),
        Binding("x", "toggle_excluded", "excluded"),
        Binding("space", "star", "star"),
        Binding("left_square_bracket", "ivr_down", "IVR-"),
        Binding("right_square_bracket", "ivr_up", "IVR+"),
        Binding("l", "cycle_liquidity", "liq"),
        Binding("e", "cycle_earnings", "ern"),
        Binding("p", "rank_shortlist", "rank"),
        Binding("v", "show_variants", "variants"),
        Binding("S", "pick_strategies", "strategies"),
        Binding("R", "reprice", "re-price", show=False),
        Binding("escape", "back", "back", show=False),
    ]

    min_ivr: reactive[float] = reactive(30.0)
    min_liquidity: reactive[int] = reactive(3)
    earnings_days: reactive[int] = reactive(45)
    sort_index: reactive[int] = reactive(0)
    show_excluded: reactive[bool] = reactive(False)
    mode: reactive[str] = reactive("screen")  # "screen" | "rank" | "variants"
    rank_sort_index: reactive[int] = reactive(0)

    def __init__(
        self,
        loader: Loader | None = None,
        chain_loader: ChainLoader | None = None,
        proposal_loader: ProposalLoader | None = None,
        history_loader: HistoryLoader | None = None,
        brief_loader: BriefLoader | None = None,
    ) -> None:
        super().__init__()
        self._loader: Loader = loader or fetch_candidates
        self._chain_loader: ChainLoader = chain_loader or fetch_cycle_for
        self._proposal_loader: ProposalLoader = proposal_loader or price_shortlist
        self._history_loader: HistoryLoader = history_loader or fetch_history_for
        self._brief_loader: BriefLoader = brief_loader or fetch_brief_for
        self._raw: list[Candidate] = []
        self._rows: list[Candidate] = []
        self._starred: set[str] = set()
        self._fetched_at: datetime | None = None
        self._status = "loading…"
        self._detail_status = ""
        # Price context and the catalyst read, cached per symbol: the first
        # is a websocket round trip, the second costs a model call.
        self._history: dict[str, history_mod.History] = {}
        self._briefs: dict[str, catalyst_mod.Brief] = {}
        # Its own status line — one shared string would let the chain load
        # and this one overwrite each other's message.
        self._why_status = ""
        # A proposal is a symbol's whole chain search — the cycle plus every
        # structure the strategies found on it — so it is the only per-symbol
        # cache there needs to be. Chain loads and the rank pricing both fill
        # it, which is why a name inspected with `c` costs nothing to rank.
        # They persist across a mode exit/re-entry; only `R` forces a
        # re-price, and a refetch clears them so stale quotes can't pass as
        # live.
        self._proposals: dict[str, Proposal] = {}
        self._passing: list[Candidate] = []  # this screen's pass set, unsorted by rank
        self._rank_rows: list[Candidate] = []
        self._pricing = False
        self._columns_mode = "screen"  # tracks which header row the table wears
        self._strategies = ALL_STRATEGIES
        # Which strategies the views show. Every proposal is always searched
        # over all of them, so this is a filter over results rather than over
        # work — toggling one costs no fetch in either direction.
        self._enabled: set[str] = {s.name for s in ALL_STRATEGIES}
        # The drill-in: which name is open, and its variants in display order.
        self._variants_symbol: str | None = None
        self._variant_rows: list[Structure] = []

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
        # Same reasoning for the price and catalyst reads: a brief taken
        # before the refresh describes the market as it was, and re-reading
        # costs nothing until the user asks for it again with `w`.
        self._history.clear()
        self._briefs.clear()
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

    def view(self, proposal: Proposal | None) -> Proposal | None:
        """A proposal as the current strategy filter shows it."""
        return None if proposal is None else proposal.only(self._enabled)

    def proposal_for(self, symbol: str | None) -> Proposal | None:
        return self.view(self._proposals.get(symbol)) if symbol else None

    def build_rank_rows(self) -> None:
        """Sort the current pass set by the active rank metric. A candidate
        with no proposal yet (or one that failed to price) sorts last rather
        than vanishing — the rank view is the same shortlist, just ordered
        differently once numbers exist."""
        label, attr, desc = RANK_SORTS[self.rank_sort_index]

        def keyfn(c: Candidate):
            if attr == "symbol":
                return (False, c.symbol)
            p = self.proposal_for(c.symbol)
            if p is None or not p.ok:
                return (True, 0.0)
            value = getattr(p, attr)
            if value is None:
                return (True, 0.0)
            return (False, -value if desc else value)

        self._rank_rows = sorted(self._passing, key=keyfn)

    def build_variant_rows(self) -> None:
        """The open name's whole search, ranked by the active metric. Failed
        and unbuildable variants stay in the list — "no lizard on MU today,
        worst_loss_up 340 > 0" is information; a missing row is not."""
        if self._variants_symbol is None:
            self._variant_rows = []
            return
        proposal = self.proposal_for(self._variants_symbol)
        if proposal is None:
            self._variant_rows = []
            return
        _, attr, _ = RANK_SORTS[self.rank_sort_index]
        key = "annualized_roc" if attr == "symbol" else attr
        self._variant_rows = proposal.variants(key)

    def render_current_table(self) -> None:
        if self.mode == "rank":
            self.render_rank_table()
        elif self.mode == "variants":
            self.render_variant_table()
        else:
            self.render_screen_table()

    def _reset_columns(self, mode: str, columns: tuple[str, ...]):
        """Empty the table for a repaint, returning it with the cursor row it
        had. The cursor has to be read before the clear, which resets it."""
        table = self.query_one("#table", DataTable)
        cursor = table.cursor_row
        if self._columns_mode != mode:
            table.clear(columns=True)
            table.add_columns(*columns)
            self._columns_mode = mode
        else:
            table.clear()
        return table, cursor

    def render_screen_table(self) -> None:
        table, cursor = self._reset_columns("screen", COLUMNS)
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
        table, cursor = self._reset_columns("rank", RANK_COLUMNS)
        blanks = len(RANK_COLUMNS) - 2
        for c in self._rank_rows:
            p = self.proposal_for(c.symbol)
            star = c.symbol in self._starred
            best = p.best if p is not None else None
            if best is None:
                marker = "★" if star else ("…" if p is None else "✗")
                table.add_row(marker, c.symbol, *(["—"] * blanks), key=c.symbol)
                continue
            table.add_row(
                "★" if star else "·",
                c.symbol,
                best.label,
                str(best.strategy.bias),
                f"{p.cycle.dte}d",
                _fmt(best.credit, ".2f"),
                _bpr(best.bpr, best.bpr_source),
                _pct(best.roc, ".1f"),
                _pct(best.annualized_roc, ".0f"),
                _pct(best.pop, ".0f"),
                _pct(best.spread_cost, ".0f"),
                _fmt(best.be_over_em, ".2f"),
                key=c.symbol,
            )
        if self._rank_rows:
            table.move_cursor(row=min(cursor, len(self._rank_rows) - 1))
        self.render_detail()

    def render_variant_table(self) -> None:
        table, cursor = self._reset_columns("variants", VARIANT_COLUMNS)
        for i, s in enumerate(self._variant_rows):
            if not s.complete:
                # Never built — no numbers to show. The reason is a sentence
                # about the ladder, so it goes to the detail pane in full.
                cells = ["✗", s.label] + ["—"] * 6 + ["not built"]
                table.add_row(*(Text(str(x), style="dim") for x in cells), key=str(i))
                continue
            # Which constraint bit, not the whole sentence — the numbers behind
            # it are in the detail pane for the highlighted row.
            why = " ".join(dict.fromkeys(f.require.metric for f in s.failures))
            cells = [
                "·" if s.ok else "✗",
                s.label,
                _fmt(s.credit, ".2f"),
                _bpr(s.bpr, s.bpr_source),
                _pct(s.annualized_roc, ".0f"),
                _pct(s.pop, ".0f"),
                _pct(s.spread_cost, ".0f"),
                _fmt(s.be_over_em, ".2f"),
                why,
            ]
            if s.ok:
                table.add_row(*cells, key=str(i))
            else:
                # Greyed, not hidden: a rejected variant and its reason are
                # the answer to "why is there no condor on this name today".
                table.add_row(*(Text(str(x), style="dim") for x in cells), key=str(i))
        if self._variant_rows:
            table.move_cursor(row=min(cursor, len(self._variant_rows) - 1))
        self.render_detail()

    def render_detail(self) -> None:
        c = self.selected
        p = self.proposal_for(c.symbol) if c else None
        status = self._detail_status
        if not status and self.mode in ("rank", "variants") and p is not None:
            if not p.ok and p.error:
                status = f"pricing failed: {p.error}"
        self.query_one("#detail", DetailPane).show(
            c,
            p,
            structure=self.selected_structure,
            status=status,
            history=self._history.get(c.symbol) if c else None,
            brief=self._briefs.get(c.symbol) if c else None,
            why_status=self._why_status,
        )

    def on_data_table_row_highlighted(self, _: DataTable.RowHighlighted) -> None:
        self._detail_status = ""
        self._why_status = ""
        self.render_detail()

    def on_data_table_row_selected(self, _: DataTable.RowSelected) -> None:
        # Enter reaches the focused table as a row selection, never the
        # app-level binding, so these hang off this instead. In the rank view
        # a name is already priced, so Enter drills into its variants rather
        # than re-fetching the chain it already has.
        if self.mode == "rank":
            self.action_show_variants()
        elif self.mode == "screen":
            self.action_load_chain()

    @work(exclusive=True)
    async def load_chain(self, candidate: Candidate) -> None:
        self._detail_status = f"loading {candidate.symbol} chain…"
        self.render_detail()
        try:
            cycle = await self._chain_loader(candidate)
            proposal = propose_mod.propose_on(candidate, cycle, self._strategies)
            session = None
            try:
                session = get_session()
            except Exception:
                pass  # no credentials — the formula estimate is the whole story
            # The broker dry-run is an upgrade, never a requirement: without a
            # session, or with one that cannot reach the account API, the
            # proposal keeps its formula figures untouched.
            self._proposals[candidate.symbol] = await propose_mod.enrich_with_broker_bpr(
                session, proposal
            )
            self._detail_status = ""
        except Exception as exc:
            self._detail_status = f"chain failed: {exc}"
        # The cursor may have moved on; only repaint what is selected now.
        self.render_current_table()

    def action_load_chain(self) -> None:
        c = self.selected
        if c is not None:
            self.load_chain(c)

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
        c = self.selected
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
            self.build_rank_rows()
            if self.mode == "variants" and p.symbol == self._variants_symbol:
                self.build_variant_rows()
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

    def action_show_variants(self) -> None:
        """Open the highlighted name's full search. Needs a proposal, so from
        the screen view it loads the chain first and the keypress is repeated
        once the numbers exist rather than opening an empty table."""
        c = self.selected
        if c is None or self.mode == "variants":
            return
        if c.symbol not in self._proposals:
            self.load_chain(c)
            return
        self._variants_symbol = c.symbol
        self.mode = "variants"
        self.build_variant_rows()
        self.render_current_table()
        self.refresh_meta()

    def action_pick_strategies(self) -> None:
        def applied(enabled: set[str] | None) -> None:
            if enabled is None or enabled == self._enabled:
                return
            self._enabled = enabled
            # No refetch: the structures are already in hand, so this only
            # changes which of them the views consider.
            self.build_rank_rows()
            self.build_variant_rows()
            self.render_current_table()
            self.refresh_meta()

        self.push_screen(StrategyPicker(self._strategies, self._enabled), applied)

    def action_back(self) -> None:
        """One step out: variants to the rank list it was opened from, rank to
        the screen, screen nowhere."""
        if self.mode == "variants":
            self.mode = "rank" if self._passing else "screen"
            self._variants_symbol = None
            self._variant_rows = []
        elif self.mode == "rank":
            self.mode = "screen"
        else:
            return
        self.render_current_table()
        self.refresh_meta()

    # ---- chrome ----

    def strategy_summary(self) -> str:
        """Names the filter when one is on, so a short list never looks like a
        thin market when it is really a setting."""
        total = len(self._strategies)
        if len(self._enabled) == total:
            return f"all {total} strategies"
        if len(self._enabled) == 1:
            return next(iter(self._enabled)) + " only"
        return f"{len(self._enabled)}/{total} strategies"

    def refresh_meta(self) -> None:
        fetched = (
            self._fetched_at.astimezone().strftime("%H:%M")
            if self._fetched_at
            else "—"
        )
        passed = getattr(self, "_passed", 0)
        if self.mode == "variants":
            rows = self._variant_rows
            passing = sum(1 for s in rows if s.ok)
            bits = [
                f"tau · {self._variants_symbol} · "
                f"{passing}/{len(rows)} variants passed",
                self.strategy_summary(),
                f"fetched {fetched}",
            ]
        elif self.mode == "rank":
            priced = sum(1 for c in self._passing if c.symbol in self._proposals)
            bits = [f"tau · rank view · {priced}/{len(self._passing)} priced"]
            if self._pricing:
                bits.append("pricing…")
            bits.append(self.strategy_summary())
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

        if self.mode == "variants":
            label = RANK_SORTS[self.rank_sort_index][0]
            self.query_one("#filters", Static).update(
                f"sort {label}  ·  greyed rows failed a constraint  "
                f"·  esc back to rank"
            )
        elif self.mode == "rank":
            label = RANK_SORTS[self.rank_sort_index][0]
            self.query_one("#filters", Static).update(
                f"sort {label}  ·  enter/v all variants  ·  "
                f"R force re-price all  ·  esc back to screen"
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
        if self.mode in ("rank", "variants"):
            self.rank_sort_index = (self.rank_sort_index + 1) % len(RANK_SORTS)
            self.build_rank_rows()
            self.build_variant_rows()
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
        """The candidate behind each visible row. In the drill-in every row
        belongs to the one open name, so they are all the same candidate —
        the row identity there is the variant, not the symbol."""
        if self.mode == "variants":
            here = [c for c in self._raw if c.symbol == self._variants_symbol]
            return here * len(self._variant_rows)
        if self.mode == "rank":
            return self._rank_rows
        return self._rows

    def _cursor(self) -> int:
        return self.query_one("#table", DataTable).cursor_row

    @property
    def selected(self) -> Candidate | None:
        rows = self.current_rows
        cursor = self._cursor()
        if not rows or cursor < 0:
            return None
        return rows[min(cursor, len(rows) - 1)]

    @property
    def selected_structure(self) -> Structure | None:
        """The highlighted variant in the drill-in. Everywhere else the detail
        pane shows the winner, so there is nothing to override."""
        if self.mode != "variants" or not self._variant_rows:
            return None
        cursor = self._cursor()
        if cursor < 0:
            return None
        return self._variant_rows[min(cursor, len(self._variant_rows) - 1)]


def run() -> None:
    TauApp().run()
