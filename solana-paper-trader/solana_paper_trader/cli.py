from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from .engine import PaperTradingEngine
from .helius import HeliusReadOnlyClient
from .market import SyntheticMarket, read_csv_ticks
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
    return parser


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
