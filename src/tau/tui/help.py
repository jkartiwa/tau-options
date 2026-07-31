"""The `?` overlay.

Every column in this tool is an abbreviation of a judgment, and an
abbreviation only works if you already know what it stands for. BE/EM, SPRD%
and θ/BPR are all decision rules compressed to five characters; the reasoning
behind them lives in module docstrings the user of a binary never reads. This
screen is where it surfaces — what each number means, and which direction is
good.
"""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

HELP = """\
[b]tau[/b] — premium-selling scanner. Screen, price, rank.

[b]screen columns[/b]
  [b]IVR[/b]    IV rank, 0–100. Where 30-day IV sits in its own year.
  [b]IVP[/b]    IV percentile. Share of days in the year IV was lower.
         IVR is set by the extremes; IVP by the whole distribution.
         IVR 80 with IVP 40 is one spike, not a sustained bid.
  [b]IV/HV[/b]  Implied over realized. [b]Under 1.00 you are selling vol
         below what the name actually does[/b] — no edge, whatever IVR says.
  [b]IV30[/b]   30-day implied volatility, annualized %.
  [b]HV30[/b]   30-day realized volatility, same scale.
  [b]LIQ[/b]    tastytrade liquidity rating, 4 best. Symbol-level, so it
         cannot see the spread at the strike you actually sell.
  [b]BETA[/b]   Beta to the broad market.
  [b]ERN[/b]    Days to the next expected earnings report.
  [b]◆[/b]      In the left column: you are already short premium on this
         name. Another sale here concentrates, it does not diversify.

[b]rank columns[/b] ([b]P[/b]) — the shortlist priced as comparable trades
  [b]DTE[/b]    Days to expiration of the chosen cycle.
  [b]CREDIT[/b] Mid-price credit for the strangle, per share.
  [b]BPR~[/b]   Estimated buying power, dollars. [b]A formula, not a broker
         quote[/b] — yours will differ.
  [b]%NL[/b]    That estimate as a share of account net liq. Sizing.
  [b]ROC%[/b]   Credit over buying power, held to expiration.
  [b]ANN%[/b]   The same annualized — what makes a 30-day and a 60-day
         trade comparable. Not a promise of repeating it.
  [b]θ/DAY[/b]  Dollars earned per day at today's greeks.
  [b]θ/BPR[/b]  That decay as a share of capital, daily. The number that
         survives closing early, which is how these are usually managed.
  [b]POP%[/b]   Probability of profit from the real breakevens under a
         driftless lognormal — not the 1−Δ shortcut, which understates.
         Compare proposals with it; do not forecast with it.
  [b]SPRD%[/b]  Both legs' bid-ask as a share of the credit. [b]Lower is
         better[/b] — crossing two wide markets can cost the edge.
  [b]BE/EM[/b]  Nearest breakeven in expected moves. [b]Under 1.00 means a
         single standard deviation reaches it.[/b]

[b]positions[/b] ([b]p[/b]) — what you already hold, soonest to expire first
  [b]STRUCTURE[/b] The legs as one trade, since that is how it is managed.
  [b]DTE[/b]    Days left. Short gamma rises fastest in the last few weeks.
  [b]CREDIT[/b] What the trade took in at open.
  [b]NOW[/b]    What closing it would cost today.
  [b]P/L%[/b]   Profit as a share of that credit — the units the usual
         management rules are written in.
  [b]BPR[/b]    The broker's own requirement, not an estimate.

[b]detail pane[/b]
  [b]term[/b]         Near vs far IV. Backwardation = the market pricing
               something soon. Contango is the resting state.
  [b]52w / at[/b]     Where price sits in its yearly range. A strangle at
               either edge is not the symmetric bet it looks like.
  [b]σ vs baseline[/b] The recent move against the name's prior noise,
               measured on a baseline that [i]excludes[/i] the move. Past
               ±2σ, check it is a repricing and not a spike.
  [b]why vol is bid[/b] Headline classification. Triage, not clearance —
               unmeasured accuracy, and it can miss events.

[b]keys[/b]
  [b][ ][/b]        IV rank floor down / up
  [b]l  e[/b]      cycle liquidity floor / earnings window
  [b]s[/b]         re-sort  ·  [b]x[/b] show excluded with reasons
  [b]/[/b]         jump to a symbol — moves the cursor, hides nothing
  [b]c[/b] Enter   price this name's cycle
  [b]< >[/b]      previous / next expiration on a loaded chain
  [b]w[/b]         why vol is bid — price context and catalyst read
  [b]p[/b]         positions  ·  [b]P[/b] price the shortlist, ranked
  [b]R[/b]         force a re-price  ·  [b]esc[/b] back to the screener
  [b]space[/b]     star  ·  [b]r[/b] refresh from the API  ·  [b]q[/b] quit

[dim]Estimates, not broker numbers. Mids can be stale outside market hours.
Verify every number with your broker before trading.[/dim]

[dim]any key to close[/dim]"""


class HelpScreen(ModalScreen):
    """Column and key reference. Dismissed by any key, since it is read and
    left rather than interacted with."""

    CSS = """
    HelpScreen { align: center middle; background: $background 80%; }
    #help-box {
        width: 78; max-width: 96%; height: auto; max-height: 92%;
        padding: 1 2; border: round $accent; background: $surface;
    }
    """

    BINDINGS = [Binding("escape,question_mark,q", "dismiss_help", "close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-box"):
            yield Static(HELP)

    def action_dismiss_help(self) -> None:
        self.dismiss()

    def on_key(self, event) -> None:
        # Scrolling still has to work, so the keys that move the viewport are
        # the one exception to "any key closes".
        if event.key in ("up", "down", "pageup", "pagedown", "home", "end"):
            return
        event.stop()
        self.dismiss()
