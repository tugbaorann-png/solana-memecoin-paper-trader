from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from .models import MarketTick, Portfolio, Position, Trade
from .strategy import MomentumStrategy


@dataclass(frozen=True)
class RunResult:
    portfolio: Portfolio
    last_prices: dict[str, float]


class PaperTradingEngine:
    """Executes virtual fills against supplied observations."""

    def __init__(
        self,
        starting_cash: float = 10_000,
        strategy: MomentumStrategy | None = None,
    ) -> None:
        if starting_cash <= 0:
            raise ValueError("starting_cash must be positive")
        self.strategy = strategy or MomentumStrategy()
        self.portfolio = Portfolio(starting_cash=starting_cash, cash=starting_cash)
        self.price_history: dict[str, list[float]] = defaultdict(list)
        self.last_prices: dict[str, float] = {}

    def run(self, ticks: Iterable[MarketTick], close_at_end: bool = True) -> RunResult:
        for tick in ticks:
            self.process_tick(tick)
        if close_at_end:
            self.close_all()
        return RunResult(portfolio=self.portfolio, last_prices=dict(self.last_prices))

    def process_tick(self, tick: MarketTick) -> None:
        if tick.price_usd <= 0 or tick.liquidity_usd < 0:
            raise ValueError(f"Invalid market values for {tick.symbol}")

        history = self.price_history[tick.symbol]
        history.append(tick.price_usd)
        self.last_prices[tick.symbol] = tick.price_usd
        position = self.portfolio.positions.get(tick.symbol)
        signal = self.strategy.signal(history, position, tick.liquidity_usd)

        if signal == "BUY" and position is None:
            self._buy(tick)
        elif signal == "SELL" and position is not None:
            self._sell(tick, self.strategy.exit_reason(history, position))

    def close_all(self) -> None:
        for symbol, position in list(self.portfolio.positions.items()):
            price = self.last_prices.get(symbol, position.entry_price)
            tick = MarketTick(
                timestamp=position.opened_at,
                symbol=symbol,
                mint=position.mint,
                price_usd=price,
                liquidity_usd=0,
                volume_24h_usd=0,
            )
            self._sell(tick, "end_of_run")

    def _fee(self, notional: float) -> float:
        return notional * self.strategy.config.fee_bps / 10_000

    def _buy(self, tick: MarketTick) -> None:
        if len(self.portfolio.positions) >= self.strategy.config.max_positions:
            return

        notional = min(self.strategy.config.position_size_usd, self.portfolio.cash)
        if notional <= 0:
            return
        fee = self._fee(notional)
        if notional + fee > self.portfolio.cash:
            return

        quantity = notional / tick.price_usd
        self.portfolio.cash -= notional + fee
        self.portfolio.positions[tick.symbol] = Position(
            symbol=tick.symbol,
            mint=tick.mint,
            quantity=quantity,
            entry_price=tick.price_usd,
            entry_fee=fee,
            opened_at=tick.timestamp,
        )
        self.portfolio.trades.append(
            Trade(
                timestamp=tick.timestamp,
                symbol=tick.symbol,
                mint=tick.mint,
                side="BUY",
                quantity=quantity,
                price_usd=tick.price_usd,
                notional_usd=notional,
                fee_usd=fee,
                reason="momentum_entry",
            )
        )

    def _sell(self, tick: MarketTick, reason: str) -> None:
        position = self.portfolio.positions.pop(tick.symbol, None)
        if position is None:
            return

        gross = position.quantity * tick.price_usd
        fee = self._fee(gross)
        pnl = gross - position.quantity * position.entry_price - position.entry_fee - fee
        self.portfolio.cash += gross - fee
        self.portfolio.realized_pnl_usd += pnl
        self.portfolio.trades.append(
            Trade(
                timestamp=tick.timestamp,
                symbol=tick.symbol,
                mint=tick.mint,
                side="SELL",
                quantity=position.quantity,
                price_usd=tick.price_usd,
                notional_usd=gross,
                fee_usd=fee,
                reason=reason,
                realized_pnl_usd=pnl,
            )
        )
