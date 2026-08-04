"""The detail pane: everything known about the highlighted name.

Two tiers, deliberately. The vol context comes from the metrics pull already
in memory and renders the instant the cursor moves; the chain costs ~1.2s and
loads only on request. That split is why moving down the list stays free.
"""

from datetime import date

from rich.markup import escape
from textual.widgets import Static

from tau import catalyst as catalyst_mod
from tau import chain as chain_mod
from tau import history as history_mod
from tau.build import BuiltLeg, Structure
from tau.propose import Proposal
from tau.screen import Candidate

TERM_NEAR_DTE = 7
TERM_FAR_DTE = 60
TERM_FLAT_BAND = 2.0  # vol points; inside this the curve reads flat
HEADLINE_LINES = 8  # shown only when there is no verdict to show instead


def _fmt(value, spec: str = ".2f", dash: str = "—") -> str:
    if value is None:
        return dash
    if value in (float("inf"), float("-inf")):
        return "∞" if value > 0 else "-∞"
    return format(value, spec)


def _pct(value, spec: str = ".0f", dash: str = "—") -> str:
    return dash if value is None else _fmt(value * 100, spec) + "%"


def _leg_line(b: BuiltLeg) -> str:
    """One resolved leg: what was asked for on the left, what the chain had
    on the right. The quantity is only shown when it isn't 1, so a ratio or a
    butterfly body stands out instead of hiding in a column of ones."""
    qty = f"{b.spec.qty}x " if b.spec.qty != 1 else ""
    delta = "—" if b.leg.delta is None else f"{abs(b.leg.delta):.3f}Δ"
    return (
        f"{b.spec.side:<5} {qty}{b.spec.type} {b.leg.strike:<8g} {delta:>7}  "
        f"{_fmt(b.leg.bid)}/{_fmt(b.leg.ask)}"
    )


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
        proposal: Proposal | None = None,
        structure: Structure | None = None,
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
        cycle = proposal.cycle if proposal is not None else None
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
            lines += [""] + self._cycle_lines(candidate, proposal, structure)
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
        # catalyst/event/note are model-written from untrusted headlines, and
        # this pane renders markup — so they are escaped. Unescaped, a crafted
        # headline could close the [yellow] around a pending_binary and strip
        # the warning off the one verdict that most needs it.
        if b.catalyst:
            lines.append(f"catalyst {escape(b.catalyst)}")
        for k in b.key_dates:
            lines.append(f"[yellow]  {escape(k.day)} — {escape(k.event)}[/yellow]")
        lines.append(escape(b.note))
        # With no verdict the headlines are all there is, so show them — that
        # is the whole read when no API key is configured. A classified name
        # doesn't need them; the verdict already stands for them.
        if b.classification == catalyst_mod.UNKNOWN and b.headlines:
            lines.append("")
            for h in b.headlines[:HEADLINE_LINES]:
                lines.append(f"[dim]{escape(h.render())}[/dim]")
        return lines

    def _cycle_lines(
        self, c: Candidate, p: Proposal, chosen: Structure | None = None
    ) -> list[str]:
        cy = p.cycle
        head = f"[b]{cy.expiration}[/b] · {cy.dte} DTE"
        atm = cy.atm_iv
        # The fair comparison is metrics' own term-structure IV *at this
        # expiration*, not the fixed-tenor iv30 (shown separately in the
        # context lines): iv30 is pinned to 30 days, but the chosen
        # expiration usually isn't, and on names with steep term structure
        # that alone produces a large gap that looks like mid-quote
        # inflation but isn't. Verified live 2026-07-27: apparent 20+pt
        # gaps against iv30 (SMH, MU, INTC) shrank to a consistent -2 to
        # -7pt gap once compared at the matching expiration.
        term_iv = next((v for d, v in c.term if d == cy.expiration), None)
        iv_line = f"spot {_fmt(cy.underlying)} · ATM IV {_fmt(atm and atm * 100, '.1f')}%"
        if atm is not None and term_iv is not None:
            iv_line += f" [dim](metrics @exp {term_iv:.1f}%)[/dim]"
        em_note = ""
        if cy.expected_move_method == "straddle×0.85":
            em_note = " [dim](wings unpriced, straddle-only)[/dim]"
        lines = [
            head,
            iv_line,
            f"expected move ±{_fmt(cy.expected_move)}{em_note}",
            "",
        ]

        # In the drill-in the highlighted variant is what the pane is for;
        # everywhere else it is the winner across every strategy searched.
        shown = chosen if chosen is not None else p.best
        if shown is None:
            lines.append(f"[yellow]no structure: {p.error or 'nothing passed'}[/yellow]")
            return lines
        if not shown.complete:
            lines.append(f"[b]{shown.label}[/b]")
            lines.append(f"[yellow]not built: {shown.reason}[/yellow]")
            return lines

        lines += self._structure_lines(shown)
        if chosen is None:
            lines += self._ladder_lines(p, shown)
            # The winner is one of many, and how many were rejected is part of
            # reading it — one passing variant out of twelve is a different
            # market from twelve out of twelve.
            considered = len(p.structures)
            passing = sum(1 for s in p.structures if s.ok)
            lines.append(
                f"[dim]{passing} of {considered} variants passed · v for all[/dim]"
            )
        return lines

    def _ladder_lines(self, p: Proposal, best: Structure) -> list[str]:
        """The winner's siblings: the same strategy's other variants, in ladder
        order.

        The rank view can only show one row per name, and on return alone the
        widest delta almost always wins, so the cheaper strikes were only
        visible by drilling in. Seeing what the extra credit costs in
        probability is the actual decision, so it belongs beside the winner.
        """
        siblings = [
            s
            for s in p.structures
            if s.strategy.name == best.strategy.name and s.complete
        ]
        if len(siblings) < 2:
            return []
        siblings.sort(key=lambda s: s.variant)
        lines = [
            "",
            f"[b]{best.strategy.name}[/b] [dim]· credit / POP / ANN[/dim]",
        ]
        for s in siblings:
            mark = "›" if s is best else " "
            row = (
                f"{mark} {s.variant:<12} {_fmt(s.credit):>6} "
                f"{_pct(s.pop):>5} {_pct(s.annualized_roc):>6}"
            )
            lines.append(row if s.ok else f"[dim]{row}[/dim]")
        return lines

    def _structure_lines(self, s: Structure) -> list[str]:
        """The winning structure, leg by leg. Every figure here is per the
        engine's generic derivation; nothing knows what family it is."""
        lines = [f"[b]{s.label}[/b] [dim]{s.strategy.bias}[/dim]"]
        lines += [f"  {_leg_line(b)}" for b in s.legs]
        be = " / ".join(f"{value:g}" for value in s.breakevens) or "—"
        premium = s.net_premium
        taken = (
            f"credit {_fmt(s.credit)}"
            if s.credit is not None
            else f"[yellow]debit {_fmt(abs(premium)) if premium else '—'}[/yellow]"
        )
        lines.append(f"{taken} · BE {be}")
        # Premium is per share and the rest is per contract. Marking the
        # dollar figures keeps two different units off adjacent lines wearing
        # the same clothes.
        lines.append(
            f"max profit ${_fmt(s.max_profit, ',.0f')} · "
            f"BPR~ ${_fmt(s.bpr, ',.0f')} · "
            f"ANN {_pct(s.annualized_roc, '.0f')}"
        )
        ratio = s.be_over_em
        risk = f"POP {_pct(s.pop, '.0f')} · spread {_pct(s.spread_cost, '.0f')}"
        if ratio is not None:
            tag, close = ("[yellow]", "[/yellow]") if ratio < 1.0 else ("", "")
            risk += f" · BE/EM {tag}{ratio:.2f}{close}"
        lines.append(risk)
        for failure in s.failures:
            lines.append(f"[yellow]{failure.reason}[/yellow]")
        miss = s.worst_off_target
        if miss is not None and miss > chain_mod.DELTA_TOLERANCE:
            lines.append(
                f"[yellow]nearest strikes miss the requested delta "
                f"by {miss:.2f}[/yellow]"
            )
        return lines
