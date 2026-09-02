# Solana Memecoin Paper Trader

A deliberately safe Python simulator for experimenting with Solana memecoin trading ideas using **virtual money only**.

## Safety boundary

This project:

- never connects to a Solana wallet;
- never asks for, reads, or stores private keys or seed phrases;
- never imports a Solana transaction or trading SDK;
- never sends orders to a DEX, RPC endpoint, exchange, or broker;
- uses deterministic synthetic prices for demos and local CSV files for backtests.

It is a strategy research tool, not financial advice and not a live trading bot.

## Quick start

```bash
cd solana-paper-trader
python -m solana_paper_trader run-demo --steps 72 --seed 7
```

Run a backtest against the included sample data:

```bash
python -m solana_paper_trader backtest data/sample_market.csv \
  --starting-cash 10000 \
  --position-size 250
```

Install it as a local command if desired:

```bash
python -m pip install -e .
paper-trader run-demo
```

## CSV format

Backtest files must include these columns:

```text
timestamp,symbol,mint,price_usd,liquidity_usd,volume_24h_usd
```

One row represents one market observation. Rows can contain multiple tokens at the
same timestamp; the engine processes them in file order.

## Strategy defaults

The included strategy is intentionally simple and transparent:

- buys only when short-term momentum exceeds the configured entry threshold;
- ignores markets below the minimum liquidity threshold;
- limits the number of open positions;
- uses fixed-dollar position sizing;
- exits on stop-loss, take-profit, or negative momentum;
- charges a configurable simulated fee in basis points.

All assumptions are in `solana_paper_trader/strategy.py` and can be changed without
adding a live-trading path.
