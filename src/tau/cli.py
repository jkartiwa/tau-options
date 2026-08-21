"""CLI: `tau` launches the interactive TUI by default.

`tau scan` runs the vol screen and prints the ranked candidates as text.
`tau rank` goes a step further and prices them — one chain fetch per symbol,
every selected strategy searched over it, best structure per name. `tau
variants` shows the whole search on a single symbol, rejections included.
`tau strategies` lists what ships.

`.env` resolves repo-root first, then cwd-upward; existing shell vars win
either way (dotenv never overrides).
"""

import argparse
import asyncio
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from tau import chain as chain_mod
from tau import propose as propose_mod
from tau import screen, store, universe
from tau.session import get_session
from tau.strategies import ALL as ALL_STRATEGIES
from tau.strategies import MIN_POP, STRATEGIES
from tau.strategy import with_min_pop

# A ranked row costs a chain fetch, so the default is a shortlist rather than
# the whole pass set. The screen is free; pricing is not.
DEFAULT_RANK_TOP = 15


def _load_env() -> None:
    repo_env = Path(__file__).resolve().parents[2] / ".env"
    if repo_env.exists():
        load_dotenv(repo_env)
    else:
        load_dotenv()


def _fmt(value, spec: str = ".1f") -> str:
    if value is None:
        return "—"
    if value in (float("inf"), float("-inf")):
        return "∞" if value > 0 else "-∞"
    return format(value, spec)


def _pct(value, spec: str = ".0f") -> str:
    return "—" if value is None else _fmt(value * 100, spec)


def _bpr(value, source: str) -> str:
    """Buying power with the source readable per row: broker figures plain,
    formula estimates carrying the tilde the column header used to. A bare
    number next to a `BPR` header would silently be either, and this column
    is exactly the one place a guess is not acceptable."""
    if value is None:
        return "—"
    return _fmt(value, ",.0f") if source == "broker" else _fmt(value, ",.0f") + "~"


def _print_table(rows: list[screen.Candidate], show_reasons: bool) -> None:
    header = f"{'SYM':<6} {'IVR':>5} {'IVP':>5} {'IV30':>5} {'HV30':>5} {'LIQ':>3} {'BETA':>5}  {'EARNINGS':<10}"
    print(header + ("  EXCLUDED" if show_reasons else ""))
    for c in rows:
        line = (
            f"{c.symbol:<6} {_fmt(c.ivr):>5} {_fmt(c.ivp):>5} "
            f"{_fmt(c.iv30):>5} {_fmt(c.hv30):>5} "
            f"{c.liquidity if c.liquidity is not None else '—':>3} "
            f"{_fmt(c.beta, '.2f'):>5}  "
            f"{c.earnings_date.isoformat() if c.earnings_date else '—':<10}"
        )
        if show_reasons:
            line += f"  {'; '.join(c.excluded)}"
        print(line)


def _selected_strategies(names: list[str] | None, min_pop: float = MIN_POP):
    """The strategies to search, defaulting to all of them. An unknown name is
    a hard error rather than a silent empty search — a typo'd `--strategy`
    that quietly returned nothing would read as "no trades today".

    `min_pop` overrides every selected strategy's shipped pop floor (default
    `MIN_POP`), so `--min-pop` can tune the gate without a code change.
    """
    if not names:
        strategies = ALL_STRATEGIES
    else:
        unknown = [n for n in names if n not in STRATEGIES]
        if unknown:
            known = ", ".join(sorted(STRATEGIES))
            raise SystemExit(
                f"unknown strategy {', '.join(unknown)} — available: {known}"
            )
        strategies = tuple(STRATEGIES[n] for n in names)
    return with_min_pop(strategies, min_pop)


async def scan(args: argparse.Namespace) -> None:
    symbols = universe.load_universe(args.universe)
    metrics = await screen.fetch_metrics(get_session(), symbols)
    today = date.today()
    candidates = screen.evaluate(
        metrics,
        min_ivr=args.min_ivr,
        min_liquidity=args.min_liquidity,
        earnings_days=args.days,
        today=today,
    )
    passed = [c for c in candidates if c.passed]
    shown = passed if not args.all else candidates
    if args.top and not args.all:
        shown = shown[: args.top]
    _print_table(shown, show_reasons=args.all)
    print(
        f"\n{len(symbols)} scanned, {len(metrics)} with metrics, "
        f"{len(passed)} passed "
        f"(IVR ≥ {args.min_ivr:.0f}, liquidity ≥ {args.min_liquidity}, "
        f"no earnings within {args.days}d)"
    )
    if args.log:
        scan_id = store.log_scan(
            {
                "min_ivr": args.min_ivr,
                "min_liquidity": args.min_liquidity,
                "days": args.days,
                "universe_size": len(symbols),
                "date": today.isoformat(),
            },
            candidates,
        )
        print(f"logged scan #{scan_id} → {store.db_path()}")


async def _screened(args: argparse.Namespace) -> tuple[list, list]:
    symbols = universe.load_universe(args.universe)
    metrics = await screen.fetch_metrics(get_session(), symbols)
    candidates = screen.evaluate(
        metrics,
        min_ivr=args.min_ivr,
        min_liquidity=args.min_liquidity,
        earnings_days=args.days,
        today=date.today(),
    )
    return candidates, [c for c in candidates if c.passed]


def _label_width(labels) -> int:
    """Wide enough for the longest label there actually is. A fixed width that
    clips would turn `30Δ+10-25` into `30Δ+10-2` — a different wing, stated
    with confidence, which is the failure this codebase keeps catching."""
    return max((len(x) for x in labels), default=len("STRUCTURE"))


def _rank_summary(priced: int, total: int) -> str:
    """The footer under the rank table, stated as a count so that an empty
    result reads as a result.

    A variant whose leg missed its requested delta by more than
    `build.MAX_DELTA_MISS` is refused rather than relabelled, so a thin day
    can legitimately leave every name without a structure. Printed as `0 of
    15`, that is a finding a trader can act on; printed as a table with
    nothing under the header, it is indistinguishable from a tool that broke.
    """
    if total and not priced:
        return (
            f"no structure on any of the {total} names priced — "
            f"the reason for each is on its row above"
        )
    return f"{priced} of {total} names produced a structure"


async def rank(args: argparse.Namespace) -> None:
    strategies = _selected_strategies(args.strategy, args.min_pop)
    candidates, passed = await _screened(args)
    shortlist = passed[: args.top] if args.top else passed
    if not shortlist:
        print("nothing passed the screen")
        return
    print(
        f"pricing {len(shortlist)} of {len(passed)} passing names against "
        f"{len(strategies)} strateg{'y' if len(strategies) == 1 else 'ies'} "
        f"at ~{args.dte} DTE…\n"
    )
    proposals = await propose_mod.price_many(
        get_session(), shortlist, strategies, args.dte
    )
    ordered = propose_mod.rank_proposals(proposals)
    w = _label_width(p.best.label for p in ordered if p.best is not None)
    print(
        f"{'SYM':<6} {'STRUCTURE':<{w}} {'BIAS':<8} {'DTE':>4} {'CREDIT':>7} "
        f"{'BPR':>8} {'ROC%':>6} {'ANN%':>7} {'POP%':>5} {'SPRD%':>6} {'BE/EM':>6}"
    )
    for p in ordered:
        s = p.best
        if s is None:
            print(f"{p.symbol:<6} {'—':<{w}} {p.error or 'no structure'}")
            continue
        print(
            f"{p.symbol:<6} {s.label:<{w}} {str(s.strategy.bias):<8} "
            f"{p.cycle.dte:>3}d {_fmt(s.credit, '.2f'):>7} "
            f"{_bpr(s.bpr, s.bpr_source):>8} {_pct(s.roc, '.1f'):>6} "
            f"{_pct(s.annualized_roc):>7} {_pct(s.pop):>5} "
            f"{_pct(s.spread_cost):>6} {_fmt(s.be_over_em, '.2f'):>6}"
        )
    priced = sum(1 for p in ordered if p.best is not None)
    print(f"\n{_rank_summary(priced, len(ordered))}")
    if args.log:
        scan_id = store.log_scan(
            {
                "min_ivr": args.min_ivr,
                "min_liquidity": args.min_liquidity,
                "days": args.days,
                "dte": args.dte,
                "strategies": [s.name for s in strategies],
                "date": date.today().isoformat(),
            },
            candidates,
        )
        picks = store.log_picks(scan_id, proposals)
        print(f"\nlogged scan #{scan_id} with {picks} picks → {store.db_path()}")


async def variants(args: argparse.Namespace) -> None:
    """Everything considered on one name, rejections included. The point of
    keeping failures is that "no lizard on MU today, worst_loss_up 340 > 0" is
    a market condition worth reading, and a missing row says nothing."""
    strategies = _selected_strategies(args.strategy, args.min_pop)
    symbol = args.symbol.upper()
    session = get_session()
    cycle = await chain_mod.fetch_cycle(session, symbol, target_dte=args.dte)
    # No metrics pull here: this command is about one name's chain, and the
    # vol context it would add is what `tau scan` is for.
    candidate = screen.Candidate(
        symbol=symbol, ivr=None, ivp=None, iv30=None, hv30=None,
        liquidity=None, beta=None, earnings_date=None,
    )
    proposal = await propose_mod.enrich_with_broker_bpr(
        session, propose_mod.propose_on(candidate, cycle, strategies)
    )
    spot = _fmt(cycle.underlying, ".2f")
    print(f"{symbol} · {cycle.expiration} · {cycle.dte} DTE · spot {spot}\n")
    ordered = proposal.variants(args.sort)
    w = _label_width(s.label for s in ordered)
    print(f"{'':<2}{'STRUCTURE':<{w}} {'BIAS':<8} {'CREDIT':>7} {'BPR':>8} "
          f"{'ANN%':>7} {'POP%':>5} {'SPRD%':>6}  WHY NOT")
    passing = 0
    for s in ordered:
        mark = "· " if s.ok else "✗ "
        if not s.complete:
            print(f"{mark}{s.label:<{w}} {str(s.strategy.bias):<8} "
                  f"{'—':>7} {'—':>8} {'—':>7} {'—':>5} {'—':>6}  {s.reason}")
            continue
        passing += bool(s.ok)
        why = "; ".join(f.reason for f in s.failures)
        print(f"{mark}{s.label:<{w}} "
              f"{str(s.strategy.bias):<8} {_fmt(s.credit, '.2f'):>7} "
              f"{_bpr(s.bpr, s.bpr_source):>8} {_pct(s.annualized_roc):>7} "
              f"{_pct(s.pop):>5} {_pct(s.spread_cost):>6}  {why}")
    print(f"\n{passing} of {len(proposal.structures)} variants passed")


def strategies(args: argparse.Namespace) -> None:
    """What ships, and what each one is looking for. Definitions live in the
    package (`src/tau/strategies/`) rather than in a config directory, so this
    is a readable index of them rather than the only way to see them."""
    for s in ALL_STRATEGIES:
        print(f"{s.name}  [{s.bias}]  {s.variant_count} variants, ranked on {s.rank}")
        for spec in s.legs:
            qty = f"{spec.qty}x " if spec.qty != 1 else ""
            print(
                f"    {spec.side:<5} {qty}{spec.type}  {spec.id:<12} "
                f"{spec.strike.describe()}"
            )
        for rule in s.require:
            print(f"    require  {rule.metric} {rule.op} {rule.value}")
        print()


def _add_screen_filters(p: argparse.ArgumentParser) -> None:
    p.add_argument("--min-ivr", type=float, default=30.0, help="IV rank floor (default 30)")
    p.add_argument("--min-liquidity", type=int, default=3, help="tasty liquidity rating floor, 4 best (default 3)")
    p.add_argument("--days", type=int, default=45, help="exclude symbols with earnings within N days; 0 disables (default 45)")
    p.add_argument("--universe", help="path to a custom universe file")


def _add_strategy_selection(p: argparse.ArgumentParser) -> None:
    p.add_argument("--strategy", action="append", metavar="NAME",
                   help="strategy to search, repeatable (default: all; see `tau strategies`)")
    p.add_argument("--dte", type=int, default=chain_mod.TARGET_DTE,
                   help=f"target days to expiration, monthly cycles only (default {chain_mod.TARGET_DTE})")
    p.add_argument("--min-pop", type=float, default=MIN_POP,
                   help=f"minimum probability of profit to be eligible as best "
                        f"(default {MIN_POP:.0%})".replace("%", "%%"))


def main() -> None:
    _load_env()
    parser = argparse.ArgumentParser(prog="tau", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("scan", help="run the premium-selling screen (text output)")
    _add_screen_filters(p)
    p.add_argument("--top", type=int, default=0, help="show only the top N passing rows")
    p.add_argument("--all", action="store_true", help="show every symbol with exclusion reasons")
    p.add_argument("--log", action="store_true", help="record this scan to the local scan log")

    p = sub.add_parser("rank", help="price the screen's shortlist and rank the best structure per name")
    _add_screen_filters(p)
    _add_strategy_selection(p)
    p.add_argument("--top", type=int, default=DEFAULT_RANK_TOP,
                   help=f"price only the top N passing names; 0 for all (default {DEFAULT_RANK_TOP})")
    p.add_argument("--log", action="store_true", help="record this scan and its picks to the local scan log")

    p = sub.add_parser("variants", help="every structure considered on one symbol, rejections included")
    p.add_argument("symbol")
    _add_strategy_selection(p)
    p.add_argument("--sort", default="annualized_roc", help="metric to rank by (default annualized_roc)")

    sub.add_parser("strategies", help="list the shipped strategy definitions")
    sub.add_parser("tui", help="interactive triage over the screen (default)")

    args = parser.parse_args()
    if args.command in (None, "tui"):
        from tau.tui.app import run

        run()
        return
    if args.command == "strategies":
        strategies(args)
        return
    asyncio.run({"scan": scan, "rank": rank, "variants": variants}[args.command](args))


if __name__ == "__main__":
    main()
