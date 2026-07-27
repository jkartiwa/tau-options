"""The detail pane: everything known about the highlighted name.

Two tiers, deliberately. The vol context comes from the metrics pull already
in memory and renders the instant the cursor moves; the chain costs ~1.2s and
loads only on request. That split is why moving down the list stays free.
"""

from datetime import date

from textual.widgets import Static

from tau import catalyst as catalyst_mod
from tau import chain as chain_mod
from tau import history as history_mod
from tau.screen import Candidate

TERM_NEAR_DTE = 7
TERM_FAR_DTE = 60
TERM_FLAT_BAND = 2.0  # vol points; inside this the curve reads flat


def _fmt(value, spec: str = ".2f", dash: str = "—") -> str:
    return dash if value is None else format(value, spec)


def term_shape(term: tuple[tuple[date, float], ...], today: date) -> str | None:
    """Contango, backwardation or flat between the near and far cycles. The
    front week is excluded: it is the noisiest point on the curve and an
    event or an expiring contract distorts it."""
    points = [(d, iv, (d - today).days) for d, iv in term]
    near = [p for p in points if p[2] >= TERM_NEAR_DTE]
    far = [p for p in points if p[2] >= TERM_FAR_DTE]
    if not near or not far:
        return None
    n, f = near[0], far[0]
    delta = f[1] - n[1]
    if abs(delta) < TERM_FLAT_BAND:
        shape = "flat"
    elif delta > 0:
        shape = "contango"
    else:
        shape = "backwardation"
    return f"{n[1]:.0f}% ({n[2]}d) → {f[1]:.0f}% ({f[2]}d)  {shape}"


class DetailPane(Static):
    """Renders one candidate, optionally with a loaded cycle."""

    def show(
        self,
        candidate: Candidate | None,
        cycle: chain_mod.Cycle | None = None,
        status: str = "",
        today: date | None = None,
        history: history_mod.History | None = None,
        brief: catalyst_mod.Brief | None = None,
        why_status: str = "",
    ) -> None:
        if candidate is None:
            self.update("[dim]no selection[/dim]")
            return
        today = today or date.today()
        lines = self._context_lines(candidate, today)
        # Price position sits with the vol context: both answer "what is
        # this name doing", and the verdict below reads against them.
        if history is not None:
            lines += [""] + self._position_lines(history)
        if brief is not None:
            lines += [""] + self._catalyst_lines(brief)
        if why_status:
            lines += ["", f"[dim]{why_status}[/dim]"]
        if status:
            lines += ["", f"[dim]{status}[/dim]"]
        if cycle is not None:
            lines += [""] + self._cycle_lines(candidate, cycle)
        self.update("\n".join(lines))

    def _context_lines(self, c: Candidate, today: date) -> list[str]:
        dte = c.days_to_earnings(today)
        earnings = "—" if dte is None else f"{c.earnings_date} ({dte}d)"
        ivhv = c.iv_hv
        ivhv_txt = _fmt(ivhv)
        if ivhv is not None and ivhv < 1.0:
            ivhv_txt = f"[yellow]{ivhv_txt} below realized[/yellow]"
        lines = [
            f"[b]{c.symbol}[/b] · liquidity {c.liquidity or '—'} · β {_fmt(c.beta)}",
            "",
            f"IVR {_fmt(c.ivr, '.0f')} · IVP {_fmt(c.ivp, '.0f')} · IV/HV {ivhv_txt}",
            f"IV30 {_fmt(c.iv30, '.1f')} · HV30 {_fmt(c.hv30, '.1f')}",
            f"earnings {earnings}",
        ]
        shape = term_shape(c.term, today)
        if shape:
            lines.append(f"term {shape}")
        if c.excluded:
            lines += ["", f"[yellow]excluded: {'; '.join(c.excluded)}[/yellow]"]
        return lines

    def _position_lines(self, h: history_mod.History) -> list[str]:
        """Where price sits, and whether it got there calmly. IV rank can't
        tell a panic spike that mean-reverts from a repricing that keeps
        going; a stretched move at the edge of the range is the tell."""
        if not h.bars:
            return ["[yellow]no price history[/yellow]"]
        pos = h.range_position
        band = _fmt(h.low_52w) + "–" + _fmt(h.high_52w)
        pos_txt = "—" if pos is None else f"{pos:.0%}"
        if pos is not None and (pos >= 0.95 or pos <= 0.05):
            edge = "high" if pos >= 0.95 else "low"
            pos_txt = f"[yellow]{pos_txt} ({edge} of range)[/yellow]"
        lines = [
            f"[b]price[/b] {_fmt(h.last)} · 52w {band} · at {pos_txt}",
        ]
        move, z = h.move, h.move_z
        move_txt = "—" if move is None else f"{move * 100:+.1f}%"
        z_txt = "—" if z is None else f"{z:+.2f}σ"
        detail = (
            f"{history_mod.MOVE_WINDOW}d move {move_txt} · {z_txt} vs own "
            f"baseline {_fmt(h.baseline_vol_annual, '.0f')}%"
        )
        lines.append(f"[yellow]{detail}[/yellow]" if h.stretched else detail)
        if h.stretched:
            lines.append(
                "[yellow]stretched — check this is repricing, not noise[/yellow]"
            )
        return lines

    def _catalyst_lines(self, b: catalyst_mod.Brief) -> list[str]:
        """The verdict on why vol is bid. Anything that isn't a clean sale
        is coloured, including 'we can't tell' — an unreadable name is not
        an all-clear."""
        colour = "green" if b.tradable else "yellow"
        lines = [
            f"[b]why vol is bid[/b] "
            f"[{colour}]{b.classification}[/{colour}] ({b.confidence})",
            f"[dim]{b.gloss}[/dim]",
        ]
        if b.catalyst:
            lines.append(f"catalyst {b.catalyst}")
        for k in b.key_dates:
            lines.append(f"[yellow]  {k.day} — {k.event}[/yellow]")
        lines.append(b.note)
        return lines

    def _cycle_lines(self, c: Candidate, cy: chain_mod.Cycle) -> list[str]:
        st = chain_mod.build_strangle(cy)
        head = f"[b]{cy.expiration}[/b] · {cy.dte} DTE"
        atm = cy.atm_iv
        # Chain IV and the metrics 30-day read are two different measurements;
        # showing both makes a disagreement visible instead of picking a
        # winner silently. They diverge most where the quotes are wide.
        iv_line = f"spot {_fmt(cy.underlying)} · ATM IV {_fmt(atm and atm * 100, '.1f')}%"
        if atm is not None and c.iv30 is not None:
            iv_line += f" [dim](metrics iv30 {c.iv30:.1f}%)[/dim]"
        em_note = ""
        if cy.expected_move_method == "straddle×0.85":
            em_note = " [dim](wings unpriced, straddle-only)[/dim]"
        lines = [
            head,
            iv_line,
            f"expected move ±{_fmt(cy.expected_move)}{em_note}",
            "",
        ]

        if not st.complete:
            lines.append(f"[yellow]no structure: {st.reason}[/yellow]")
            return lines

        be = st.breakevens
        ratio = chain_mod.be_vs_expected_move(cy, st)
        lines += [
            f"[b]{st.target_delta:.2f}Δ strangle[/b]",
            f"  put  {st.put.strike:g} @ {abs(st.put.delta):.3f}Δ   "
            f"{_fmt(st.put.bid)}/{_fmt(st.put.ask)}",
            f"  call {st.call.strike:g} @ {abs(st.call.delta):.3f}Δ   "
            f"{_fmt(st.call.bid)}/{_fmt(st.call.ask)}",
            f"credit {_fmt(st.credit)} · BE {_fmt(be[0])} / {_fmt(be[1])}",
        ]
        if ratio is not None:
            tag = "[yellow]" if ratio < 1.0 else ""
            close = "[/yellow]" if tag else ""
            lines.append(
                f"BE/EM {tag}{ratio:.2f}{close}"
                f" · worst spread {_fmt(st.worst_spread)}"
            )
        if st.off_target and st.off_target > chain_mod.DELTA_TOLERANCE:
            lines.append(
                f"[yellow]nearest strikes miss {st.target_delta:.2f}Δ "
                f"by {st.off_target:.2f}[/yellow]"
            )
        return lines
