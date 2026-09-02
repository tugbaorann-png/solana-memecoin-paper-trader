# Solana Memecoin Paper Trader

A deliberately safe Python simulator for experimenting with Solana memecoin trading ideas using **virtual money only**.

## Safety boundary

This project:

- never connects to a Solana wallet;
- never asks for, reads, or stores private keys or seed phrases;
- never imports a Solana transaction or trading SDK;
- never sends orders or transactions to a DEX, exchange, or broker;
- uses Helius only for a read-only mainnet block-height health check;
- uses Dexscreener only for public read-only market data;
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

Check the read-only Helius mainnet connection. The `HELIUS_API_KEY` environment
secret must be configured first:

```bash
python -m solana_paper_trader check-helius
```

This calls Solana JSON-RPC `getBlockHeight` through Helius and prints only the
returned block height. It does not enable live market data or live execution.

Scan and monitor live Solana token markets:

```bash
python -m solana_paper_trader scan-live --limit 20 --cycles 1
```

Run the continuous paper-trading loop. It keeps scanning until you stop it with
Ctrl+C:

```bash
python -m solana_paper_trader paper-loop \
  --limit 20 \
  --interval-seconds 60
```

For a bounded smoke run, pass `--cycles 2`. Every cycle reuses the existing
filters and ranking strategy, opens only virtual positions for newly eligible
tokens, marks existing positions to current public prices, evaluates their
take-profit/stop-loss thresholds, and saves the ledger.

The scanner discovers recent Solana token profiles from Dexscreener, selects the
highest-liquidity pair for each token, and collects price, liquidity, market cap,
token age, 5-minute and 1-hour volume, 5-minute price change, and 5-minute buys
and sells. It rejects low-liquidity, too-new, inactive, extreme-move, and
volume/liquidity-outlier tokens before ranking survivors by momentum and liquidity.

Every token that passes filters gets a virtual $10 paper entry. With multiple
cycles, the ledger marks each position as `TAKE_PROFIT` or `STOP_LOSS` when its
simulated P/L reaches the configured thresholds:

```bash
python -m solana_paper_trader scan-live \
  --cycles 5 \
  --interval-seconds 60 \
  --take-profit 20 \
  --stop-loss -10 \
  --json
```

If Dexscreener temporarily rate-limits a request, `paper-loop` backs off and
retries instead of ending the paper-trading session.

The scanner and continuous loop persist their virtual open and closed positions to
`.paper_trader/live_paper_ledger.json` by default. Use `--state-file` to choose a
different local JSON path. State writes use an atomic replace, and invalid state
fails clearly instead of silently erasing history.

`scan-live` never uses `HELIUS_API_KEY`; Helius remains limited to the separate
read-only health check.

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
adding a live-trading path. The Helius client in `solana_paper_trader/helius.py`
is intentionally limited to the block-height health check.
