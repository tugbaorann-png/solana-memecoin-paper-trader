# Solana Memecoin Paper Trader

An offline Python simulator for testing Solana memecoin strategy ideas with virtual money only.

## Run & Operate

- `cd solana-paper-trader && python -m solana_paper_trader run-demo` — run the deterministic paper-trading demo
- `cd solana-paper-trader && python -m unittest discover -s tests` — run the Python safety and behavior tests
- `cd solana-paper-trader && python -m solana_paper_trader backtest data/sample_market.csv` — backtest a local CSV
- `cd solana-paper-trader && python -m solana_paper_trader check-helius` — perform the read-only Helius block-height health check

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `solana-paper-trader/solana_paper_trader/` — simulator models, strategy, market feeds, engine, and CLI
- `solana-paper-trader/data/sample_market.csv` — local backtest fixture
- `solana-paper-trader/tests/` — behavior and safety-boundary tests

## Architecture decisions

- Market input is intentionally limited to deterministic synthetic data and local CSV files.
- Helius is used only for a read-only `getBlockHeight` health check via `HELIUS_API_KEY`.
- The execution engine only creates virtual fills in an in-memory portfolio; it has no wallet or transaction abstraction.
- Strategy controls are explicit dataclass fields so backtests are reproducible and easy to audit.

## Product

- Run a deterministic demo with virtual cash.
- Backtest against local market observations.
- Review every simulated fill, fee, exit reason, and realized P&L.

## User preferences

- Never execute real trades or request wallet private keys.

## Gotchas

- This is a paper-trading simulator only; do not add live wallet or order-routing dependencies without an explicit product change.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
