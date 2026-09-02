from __future__ import annotations

from dataclasses import dataclass

from .models import Position


@dataclass(frozen=True)
class StrategyConfig:
    """Transparent knobs for the offline momentum strategy."""

    momentum_window: int = 3
    entry_momentum_pct: float = 0.06
    exit_momentum_pct: float = -0.04
    stop_loss_pct: float = -0.12
    take_profit_pct: float = 0.25
    min_liquidity_usd: float = 50_000
    position_size_usd: float = 250
    max_positions: int = 3
    fee_bps: float = 50


class MomentumStrategy:
    def __init__(self, config: StrategyConfig | None = None) -> None:
        self.config = config or StrategyConfig()

    def signal(
        self,
        prices: list[float],
        position: Position | None,
        liquidity_usd: float,
    ) -> str:
        """Return BUY, SELL, or HOLD from price history and current position."""
        if len(prices) < self.config.momentum_window + 1:
            return "HOLD"

        current_price = prices[-1]
        reference_price = prices[-(self.config.momentum_window + 1)]
        if reference_price <= 0:
            return "HOLD"

        momentum = current_price / reference_price - 1

        if position is not None:
            change_from_entry = current_price / position.entry_price - 1
            if change_from_entry <= self.config.stop_loss_pct:
                return "SELL"
            if change_from_entry >= self.config.take_profit_pct:
                return "SELL"
            if momentum <= self.config.exit_momentum_pct:
                return "SELL"
            return "HOLD"

        if liquidity_usd < self.config.min_liquidity_usd:
            return "HOLD"
        if momentum >= self.config.entry_momentum_pct:
            return "BUY"
        return "HOLD"

    def exit_reason(self, prices: list[float], position: Position) -> str:
        current_price = prices[-1]
        change_from_entry = current_price / position.entry_price - 1
        if change_from_entry <= self.config.stop_loss_pct:
            return "stop_loss"
        if change_from_entry >= self.config.take_profit_pct:
            return "take_profit"
        return "momentum_reversal"
