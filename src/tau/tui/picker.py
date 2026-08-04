"""The strategy picker: an overlay for turning structures on and off.

Toggling one is a view change, not a refetch. Every proposal already carries
every structure the search produced, so disabling a strategy re-ranks the list
from what is already in memory, and re-enabling it costs nothing either.
"""

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Static

COLUMNS = ("", "STRATEGY", "BIAS", "VARIANTS")


class StrategyPicker(ModalScreen[set[str]]):
    """Pick which strategies the rank view searches over."""

    CSS = """
    StrategyPicker {
        align: center middle;
    }
    #picker {
        width: 60; height: auto; max-height: 80%;
        border: solid $accent; background: $surface; padding: 1 2;
    }
    #picker-title { height: 1; color: $text-muted; }
    #picker-table { height: auto; max-height: 20; }
    #picker-help { height: 1; color: $text-muted; }
    """

    BINDINGS = [
        Binding("escape", "close", "done"),
        Binding("space", "toggle", "toggle"),
        Binding("a", "enable_all", "all"),
        Binding("n", "enable_none", "none"),
    ]

    def __init__(self, strategies, enabled: set[str]) -> None:
        super().__init__()
        self._strategies = list(strategies)
        # A copy: the picker edits its own set and hands it back on close, so
        # dismissing without a change cannot half-apply anything.
        self._enabled = set(enabled)

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static("Strategies searched", id="picker-title")
            yield DataTable(id="picker-table", cursor_type="row")
            yield Static(
                "space toggle · a all · n none · esc done", id="picker-help"
            )

    def on_mount(self) -> None:
        table = self.query_one("#picker-table", DataTable)
        table.add_columns(*COLUMNS)
        self.repaint()
        table.focus()

    def repaint(self) -> None:
        table = self.query_one("#picker-table", DataTable)
        cursor = table.cursor_row
        table.clear()
        for strategy in self._strategies:
            on = strategy.name in self._enabled
            cells = [
                "[x]" if on else "[ ]",
                strategy.name,
                str(strategy.bias),
                str(strategy.variant_count),
            ]
            # Text rather than plain strings: "[x]" is valid Rich markup and
            # renders as nothing at all when it is parsed rather than shown.
            # Disabled rows are greyed, matching the rejected rows elsewhere.
            style = "" if on else "dim"
            table.add_row(
                *(Text(c, style=style) for c in cells), key=strategy.name
            )
        if self._strategies:
            table.move_cursor(row=max(0, min(cursor, len(self._strategies) - 1)))

    @property
    def highlighted(self):
        table = self.query_one("#picker-table", DataTable)
        if not self._strategies or table.cursor_row < 0:
            return None
        return self._strategies[min(table.cursor_row, len(self._strategies) - 1)]

    def toggle(self, name: str) -> None:
        # Never leave every strategy off. An empty rank view looks like a
        # broken scan rather than a filter the user set two keystrokes ago.
        if name in self._enabled and len(self._enabled) == 1:
            return
        self._enabled.symmetric_difference_update({name})
        self.repaint()

    def action_toggle(self) -> None:
        strategy = self.highlighted
        if strategy is not None:
            self.toggle(strategy.name)

    def on_data_table_row_selected(self, _: DataTable.RowSelected) -> None:
        self.action_toggle()

    def action_enable_all(self) -> None:
        self._enabled = {s.name for s in self._strategies}
        self.repaint()

    def action_enable_none(self) -> None:
        """Leaves the highlighted one on, for the same reason `toggle` will not
        clear the last strategy: this is a way to isolate one, not to empty the
        list."""
        strategy = self.highlighted or (self._strategies[0] if self._strategies else None)
        self._enabled = {strategy.name} if strategy is not None else set()
        self.repaint()

    def action_close(self) -> None:
        self.dismiss(self._enabled)
