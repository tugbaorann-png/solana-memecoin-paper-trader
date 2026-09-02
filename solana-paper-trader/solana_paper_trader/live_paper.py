from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .scanner import TokenScan, TokenSnapshot


@dataclass(frozen=True)
class LivePaperConfig:
    notional_usd: float = 10.0
    take_profit_pct: float = 20.0
    stop_loss_pct: float = -10.0


@dataclass
class LivePaperPosition:
    symbol: str
    mint: str
    entry_price_usd: float
    quantity: float
    entry_value_usd: float
    current_price_usd: float
    current_value_usd: float
    pnl_usd: float
    pnl_pct: float
    take_profit_pct: float
    stop_loss_pct: float
    status: str
    opened_at: datetime
    updated_at: datetime
    exit_price_usd: float | None = None
    exit_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "mint": self.mint,
            "entry_price_usd": self.entry_price_usd,
            "quantity": self.quantity,
            "entry_value_usd": self.entry_value_usd,
            "current_price_usd": self.current_price_usd,
            "current_value_usd": self.current_value_usd,
            "pnl_usd": self.pnl_usd,
            "pnl_pct": self.pnl_pct,
            "take_profit_pct": self.take_profit_pct,
            "stop_loss_pct": self.stop_loss_pct,
            "status": self.status,
            "opened_at": self.opened_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "exit_price_usd": self.exit_price_usd,
            "exit_reason": self.exit_reason,
        }


class LivePaperLedger:
    """Tracks virtual $10 entries; it has no order, wallet, or settlement methods."""

    def __init__(self, config: LivePaperConfig | None = None) -> None:
        self.config = config or LivePaperConfig()
        if self.config.notional_usd <= 0:
            raise ValueError("notional_usd must be positive")
        self.positions: dict[str, LivePaperPosition] = {}

    def update(self, scans: list[TokenScan]) -> list[LivePaperPosition]:
        for scan in scans:
            snapshot = scan.snapshot
            position = self.positions.get(snapshot.mint)
            if position is None and scan.passed_filters:
                position = self._open(snapshot)
                self.positions[snapshot.mint] = position
            elif position is not None and position.status == "OPEN":
                self._mark(position, snapshot)
        return list(self.positions.values())

    def _open(self, snapshot: TokenSnapshot) -> LivePaperPosition:
        quantity = self.config.notional_usd / snapshot.price_usd
        return LivePaperPosition(
            symbol=snapshot.symbol,
            mint=snapshot.mint,
            entry_price_usd=snapshot.price_usd,
            quantity=quantity,
            entry_value_usd=self.config.notional_usd,
            current_price_usd=snapshot.price_usd,
            current_value_usd=self.config.notional_usd,
            pnl_usd=0.0,
            pnl_pct=0.0,
            take_profit_pct=self.config.take_profit_pct,
            stop_loss_pct=self.config.stop_loss_pct,
            status="OPEN",
            opened_at=snapshot.observed_at,
            updated_at=snapshot.observed_at,
        )

    def _mark(self, position: LivePaperPosition, snapshot: TokenSnapshot) -> None:
        current_value = position.quantity * snapshot.price_usd
        pnl_usd = current_value - position.entry_value_usd
        pnl_pct = pnl_usd / position.entry_value_usd * 100
        position.current_price_usd = snapshot.price_usd
        position.current_value_usd = current_value
        position.pnl_usd = pnl_usd
        position.pnl_pct = pnl_pct
        position.updated_at = snapshot.observed_at
        if pnl_pct >= position.take_profit_pct:
            position.status = "TAKE_PROFIT"
            position.exit_price_usd = snapshot.price_usd
            position.exit_reason = "take_profit"
        elif pnl_pct <= position.stop_loss_pct:
            position.status = "STOP_LOSS"
            position.exit_price_usd = snapshot.price_usd
            position.exit_reason = "stop_loss"
