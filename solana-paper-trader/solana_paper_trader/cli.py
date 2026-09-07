from __future__ import annotations

import argparse
import json
import time
from collections.abc import Sequence

from .engine import PaperTradingEngine
from .helius import HeliusReadOnlyClient
from .live_paper import (
    LivePaperConfig,
    LivePaperLedger,
    PaperLedgerPersistenceError,
)
from .market import SyntheticMarket, read_csv_ticks
from .scanner import DexscreenerClient, MarketDataError, ScannerConfig
from .strategy import MomentumStrategy, StrategyConfig


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paper-trader",
        description="Offline Solana memecoin paper-trading simulator.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("run-demo", help="Run a deterministic synthetic-market simulation.")
    demo.add_argument("--steps", type=int, default=72)
    demo.add_argument("--seed", type=int, default=7)
    demo.add_argument("--starting-cash", type=float, default=10_000)
    demo.add_argument("--position-size", type=float, default=250)
    demo.add_argument("--json", action="store_true", help="Print machine-readable output.")

    backtest = subparsers.add_parser("backtest", help="Backtest against a local CSV file.")
    backtest.add_argument("csv_path")
    backtest.add_argument("--starting-cash", type=float, default=10_000)
    backtest.add_argument("--position-size", type=float, default=250)
    backtest.add_argument("--json", action="store_true", help="Print machine-readable output.")

    subparsers.add_parser(
        "check-helius",
        help="Read the latest confirmed Solana mainnet block height through Helius.",
    )

    scanner = subparsers.add_parser(
        "scan-live",
        help="Discover and monitor Solana tokens with public read-only market data.",
    )
    _add_live_arguments(scanner, cycles_default=1)

    loop = subparsers.add_parser(
        "paper-loop",
        help="Continuously scan and manage virtual paper positions until interrupted.",
    )
    _add_live_arguments(loop, cycles_default=0)
    return parser


def _add_live_arguments(parser: argparse.ArgumentParser, *, cycles_default: int) -> None:
    """Add the shared scanner options to one-shot and continuous commands."""
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--cycles",
        type=int,
        default=cycles_default,
        help="Number of cycles; 0 means continuous for paper-loop.",
    )
    parser.add_argument("--interval-seconds", type=float, default=60)
    parser.add_argument("--min-liquidity", type=float, default=25_000)
    parser.add_argument("--take-profit", type=float, default=20)
    parser.add_argument("--stop-loss", type=float, default=-10)
    parser.add_argument(
        "--state-file",
        "--ledger-path",
        dest="state_file",
        default=".paper_trader/live_paper_ledger.json",
        help="Local JSON file used to persist virtual open and closed positions.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable output.")


def _run_engine(args: argparse.Namespace) -> PaperTradingEngine:
    config = StrategyConfig(position_size_usd=args.position_size)
    engine = PaperTradingEngine(
        starting_cash=args.starting_cash,
        strategy=MomentumStrategy(config),
    )
    if args.command == "run-demo":
        result = engine.run(SyntheticMarket(seed=args.seed).ticks(args.steps))
    else:
        result = engine.run(read_csv_ticks(args.csv_path))
    _print_result(engine, result.last_prices, as_json=args.json)
    return engine


def _print_result(
    engine: PaperTradingEngine,
    prices: dict[str, float],
    *,
    as_json: bool,
) -> None:
    portfolio = engine.portfolio
    summary = portfolio.snapshot(prices)
    if as_json:
        print(json.dumps({"summary": summary, "trades": [_trade_json(trade) for trade in portfolio.trades]}, indent=2))
        return

    print("\nPAPER-TRADING RUN COMPLETE")
    print("Virtual fills only; no wallet or live order connection exists.")
    print("-" * 62)
    for key, value in summary.items():
        label = key.replace("_", " ").title()
        suffix = "%" if key == "return_pct" else ""
        print(f"{label:<22} {value:>12}{suffix}")
    print("-" * 62)
    print(f"Trades recorded: {len(portfolio.trades)}")
    for trade in portfolio.trades:
        pnl = f" | P&L ${trade.realized_pnl_usd:+.2f}" if trade.side == "SELL" else ""
        print(
            f"{trade.timestamp.isoformat()}  {trade.side:<4} {trade.symbol:<7}"
            f" ${trade.price_usd:.8f}  {trade.reason}{pnl}"
        )


def _run_live_scan(args: argparse.Namespace) -> int:
    continuous = args.command == "paper-loop" and args.cycles == 0
    if args.limit <= 0 or args.cycles < 0 or (not continuous and args.cycles == 0):
        raise SystemExit("--limit must be positive; --cycles must be positive for scan-live")
    if args.interval_seconds < 0:
        raise SystemExit("--interval-seconds cannot be negative")
    if args.take_profit <= 0 or args.stop_loss >= 0:
        raise SystemExit("--take-profit must be positive and --stop-loss must be negative")

    scanner = DexscreenerClient()
    scan_config = ScannerConfig(min_liquidity_usd=args.min_liquidity)
    try:
        ledger = LivePaperLedger(
            LivePaperConfig(
                notional_usd=10,
                take_profit_pct=args.take_profit,
                stop_loss_pct=args.stop_loss,
            ),
            state_path=getattr(
                args,
                "state_file",
                getattr(args, "ledger_path", ".paper_trader/live_paper_ledger.json"),
            ),
        )
    except PaperLedgerPersistenceError as error:
        raise SystemExit(str(error)) from error

    cycle = 0
    try:
        while continuous or cycle < args.cycles:
            try:
                latest_scan = scanner.scan(limit=args.limit, config=scan_config)
            except MarketDataError as error:
                if continuous and error.retryable:
                    if not args.json:
                        print(f"\n{error} Waiting {error.retry_after_seconds:.0f}s.")
                    time.sleep(error.retry_after_seconds)
                    continue
                raise

            cycle += 1

            # Refresh every already-open position independently of latest-token discovery.
            open_snapshots = []
            for position in ledger.positions.values():
                if position.status != "OPEN":
                    continue
                try:
                    snapshot = scanner.token_snapshot(position.mint)
                except MarketDataError as error:
                    if not args.json:
                        print(
                            f"Unable to refresh {position.symbol}; "
                            "keeping the position open until the next cycle."
                        )
                    continue
                if snapshot is not None:
                    open_snapshots.append(snapshot)

            ledger.update_open_positions(open_snapshots)

            # Process newly discovered tokens and open eligible paper positions.
            paper_positions = ledger.update(list(latest_scan.scanned))

            if args.json:
                print(
                    json.dumps(
                        _live_scan_json(latest_scan, paper_positions, cycle),
                        indent=2,
                    ),
                    flush=True,
                )
            else:
                _print_live_scan(latest_scan, paper_positions, cycle)

            if continuous or cycle < args.cycles:
                time.sleep(args.interval_seconds)

    except MarketDataError as error:
        raise SystemExit(str(error)) from error
    except PaperLedgerPersistenceError as error:
        raise SystemExit(str(error)) from error
    except KeyboardInterrupt:
        if not args.json:
            print("\nPaper loop stopped. Virtual position history remains saved.")
    return 0


def _live_scan_json(scan, positions, cycle: int) -> dict:
    return {
        "provider": scan.provider,
        "cycle": cycle,
        "observed_at": scan.observed_at.isoformat(),
        "scanned_count": len(scan.scanned),
        "eligible_count": len(scan.eligible),
        "rejected_count": len(scan.rejected),
        "ranked_tokens": [item.to_dict() for item in scan.eligible],
        "rejected_tokens": [item.to_dict() for item in scan.rejected],
        "paper_trades": [position.to_dict() for position in positions],
        "paper_summary": _paper_summary(positions),
    }


def _print_live_scan(scan, positions, cycle: int) -> None:
    print(f"\nLIVE SCAN {cycle} — {scan.observed_at.isoformat()}")
    print("Source: Dexscreener public API (read-only); fills are virtual $10 paper trades.")
    print(f"Eligible: {len(scan.eligible)} | Rejected: {len(scan.rejected)}")
    print("-" * 122)
    print(
        f"{'TOKEN':<10} {'PRICE':>12} {'LIQUIDITY':>12} {'MKT CAP':>12}"
        f" {'AGE':>8} {'VOL 5M':>11} {'VOL 1H':>11} {'CHG 5M':>8}"
        f" {'BUYS/SELLS':>11} {'RANK':>8}"
    )
    for item in scan.eligible:
        token = item.snapshot
        print(
            f"{token.symbol[:9]:<10} ${token.price_usd:>10.8f} ${token.liquidity_usd:>10,.0f}"
            f" ${token.market_cap_usd:>10,.0f} {token.token_age_minutes:>7.0f}m"
            f" ${token.volume_5m_usd:>9,.0f} ${token.volume_1h_usd:>9,.0f}"
            f" {token.price_change_5m_pct:>7.2f}% {token.buys_5m:>5}/{token.sells_5m:<5}"
            f" {item.rank_score:>7.2f}"
        )
    if scan.rejected:
        print("\nRejected token reasons:")
        for item in scan.rejected:
            print(f"  {item.snapshot.symbol:<10} {', '.join(item.rejection_reasons)}")
    print("\nPaper trades:")
    if not positions:
        print("  No tokens passed the filters in this cycle.")
    for position in positions:
        print(
            f"  {position.symbol:<10} entry ${position.entry_price_usd:.8f}"
            f" | current ${position.current_price_usd:.8f}"
            f" | value ${position.current_value_usd:.2f}"
            f" | P/L {position.pnl_pct:+.2f}%"
            f" | {position.status}"
        )
    summary = _paper_summary(positions)
    print(
        f"  Virtual P/L: ${summary['total_pnl_usd']:+.2f}"
        f" | open value: ${summary['open_value_usd']:.2f}"
        f" | open positions: {summary['open_positions']}"
    )


def _paper_summary(positions) -> dict[str, float | int]:
    return {
        "open_positions": sum(position.status == "OPEN" for position in positions),
        "closed_positions": sum(position.status != "OPEN" for position in positions),
        "open_value_usd": sum(
            position.current_value_usd
            for position in positions
            if position.status == "OPEN"
        ),
        "total_pnl_usd": sum(position.pnl_usd for position in positions),
    }


def _trade_json(trade) -> dict[str, str | float]:
    return {
        "timestamp": trade.timestamp.isoformat(),
        "symbol": trade.symbol,
        "mint": trade.mint,
        "side": trade.side,
        "quantity": trade.quantity,
        "price_usd": trade.price_usd,
        "notional_usd": trade.notional_usd,
        "fee_usd": trade.fee_usd,
        "reason": trade.reason,
        "realized_pnl_usd": trade.realized_pnl_usd,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command in {"scan-live", "paper-loop"}:
        return _run_live_scan(args)
    if args.command == "check-helius":
        block_height = HeliusReadOnlyClient.from_environment().get_latest_block_height()
        print(f"Helius mainnet connection OK — latest confirmed block height: {block_height}")
        print("Read-only health check; paper-trading mode remains enabled.")
        return 0
    if args.command == "run-demo" and args.steps <= 0:
        raise SystemExit("--steps must be positive")
    if args.starting_cash <= 0 or args.position_size <= 0:
        raise SystemExit("cash and position size must be positive")
    _run_engine(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
