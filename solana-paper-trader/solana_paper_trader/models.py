from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class MarketTick:
    """One read-only market observation supplied to the simulator."""

    timestamp: datetime
    symbol: str
    mint: str
    price_usd: float
    liquidity_usd: float
    volume_24h_usd: float


@dataclass
class Position:
    symbol: str
    mint: str
    quantity: float
    entry_price: float
    entry_fee: float
    opened_at: datetime

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.entry_price + self.entry_fee


@dataclass(frozen=True)
class Trade:
    timestamp: datetime
    symbol: str
    mint: str
    side: str
    quantity: float
    price_usd: float
    notional_usd: float
    fee_usd: float
    reason: str
    realized_pnl_usd: float = 0.0


@dataclass
class Portfolio:
    starting_cash: float
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    realized_pnl_usd: float = 0.0

    def equity(self, prices: dict[str, float]) -> float:
        marked_positions = sum(
            position.quantity * prices.get(position.symbol, position.entry_price)
            for position in self.positions.values()
        )
        return self.cash + marked_positions

    def unrealized_pnl_usd(self, prices: dict[str, float]) -> float:
        return sum(
            (prices.get(position.symbol, position.entry_price) - position.entry_price)
            * position.quantity
            - position.entry_fee
            for position in self.positions.values()
        )

    def snapshot(self, prices: dict[str, float]) -> dict[str, float | int]:
        equity = self.equity(prices)
        return {
            "cash_usd": round(self.cash, 2),
            "equity_usd": round(equity, 2),
            "return_pct": round((equity / self.starting_cash - 1) * 100, 2),
            "open_positions": len(self.positions),
            "closed_trades": len([trade for trade in self.trades if trade.side == "SELL"]),
            "realized_pnl_usd": round(self.realized_pnl_usd, 2),
            "unrealized_pnl_usd": round(self.unrealized_pnl_usd(prices), 2),
        }
