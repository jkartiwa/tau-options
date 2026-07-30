"""CLI: `tau` launches the interactive TUI by default; `tau scan` pulls
metrics for the universe and prints the ranked screen as text, logging the
scan. `.env` resolves repo-root first, then cwd-upward; existing shell vars
win either way (dotenv never overrides)."""

import argparse
import asyncio
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from tau import screen, store, universe
from tau.session import get_session


def _load_env() -> None:
    repo_env = Path(__file__).resolve().parents[2] / ".env"
    if repo_env.exists():
        load_dotenv(repo_env)
    else:
        load_dotenv()


def _fmt(value, spec: str = ".1f") -> str:
    return "—" if value is None else format(value, spec)


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
    if not args.no_log:
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


def main() -> None:
    _load_env()
    parser = argparse.ArgumentParser(prog="tau", description=__doc__)
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("scan", help="run the premium-selling screen (text output)")
    p.add_argument("--min-ivr", type=float, default=30.0, help="IV rank floor (default 30)")
    p.add_argument("--min-liquidity", type=int, default=3, help="tasty liquidity rating floor, 4 best (default 3)")
    p.add_argument("--days", type=int, default=45, help="exclude symbols with earnings within N days; 0 disables (default 45)")
    p.add_argument("--top", type=int, default=0, help="show only the top N passing rows")
    p.add_argument("--all", action="store_true", help="show every symbol with exclusion reasons")
    p.add_argument("--universe", help="path to a custom universe file")
    p.add_argument("--no-log", action="store_true", help="skip writing the scan log")

    sub.add_parser("tui", help="interactive triage over the screen (default)")

    args = parser.parse_args()
    if args.command in (None, "tui"):
        from tau.tui.app import run

        run()
        return
    asyncio.run(scan(args))


if __name__ == "__main__":
    main()
