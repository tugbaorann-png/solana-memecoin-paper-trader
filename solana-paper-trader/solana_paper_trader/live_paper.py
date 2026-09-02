from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .scanner import TokenScan, TokenSnapshot


@dataclass(frozen=True)
class LivePaperConfig:
    notional_usd: float = 10.0
    take_profit_pct: float = 20.0
    stop_loss_pct: float = -10.0


class PaperLedgerPersistenceError(RuntimeError):
    """Raised when saved paper-trading state cannot be safely read or written."""


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
    """Tracks virtual $10 entries with optional local JSON persistence."""

    state_version = 1

    def __init__(
        self,
        config: LivePaperConfig | None = None,
        state_path: str | Path | None = None,
        *,
        persistence_path: str | Path | None = None,
    ) -> None:
        self.config = config or LivePaperConfig()
        if self.config.notional_usd <= 0:
            raise ValueError("notional_usd must be positive")
        if state_path is not None and persistence_path is not None:
            raise ValueError("provide only one of state_path or persistence_path")
        self.positions: dict[str, LivePaperPosition] = {}
        selected_path = state_path if state_path is not None else persistence_path
        self.state_path = Path(selected_path) if selected_path else None
        self.persistence_path = self.state_path
        if self.state_path:
            self._load()

    def update(self, scans: list[TokenScan]) -> list[LivePaperPosition]:
        for scan in scans:
            snapshot = scan.snapshot
            position = self.positions.get(snapshot.mint)
            if position is None and scan.passed_filters:
                position = self._open(snapshot)
                self.positions[snapshot.mint] = position
            elif position is not None and position.status == "OPEN":
                self._mark(position, snapshot)
        self.save()
        return list(self.positions.values())

    def save(self) -> None:
        """Persist open and closed virtual positions with an atomic file replace."""
        if self.state_path is None:
            return
        parent = self.state_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.state_version,
            "positions": [position.to_dict() for position in self.positions.values()],
        }
        temporary_path: str | None = None
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=f".{self.state_path.name}.",
                suffix=".tmp",
                dir=parent,
                text=True,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(payload, file, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.state_path)
            temporary_path = None
        except (OSError, TypeError, ValueError) as error:
            raise PaperLedgerPersistenceError(
                f"Unable to save paper ledger to {self.state_path}."
            ) from error
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            with self.state_path.open(encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            raise PaperLedgerPersistenceError(
                f"Unable to read paper ledger from {self.state_path}."
            ) from error

        if not isinstance(payload, dict) or payload.get("version") != self.state_version:
            raise PaperLedgerPersistenceError(
                f"Unsupported paper ledger format in {self.state_path}."
            )
        saved_positions = payload.get("positions")
        if not isinstance(saved_positions, list):
            raise PaperLedgerPersistenceError(
                f"Paper ledger positions must be a list in {self.state_path}."
            )
        try:
            loaded = [self._position_from_dict(item) for item in saved_positions]
        except (KeyError, TypeError, ValueError) as error:
            raise PaperLedgerPersistenceError(
                f"Invalid paper position in {self.state_path}."
            ) from error
        self.positions = {position.mint: position for position in loaded}

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


    @classmethod
    def _position_from_dict(cls, data: dict[str, Any]) -> LivePaperPosition:
        position = LivePaperPosition(
            symbol=str(data["symbol"]),
            mint=str(data["mint"]),
            entry_price_usd=float(data["entry_price_usd"]),
            quantity=float(data["quantity"]),
            entry_value_usd=float(data["entry_value_usd"]),
            current_price_usd=float(data["current_price_usd"]),
            current_value_usd=float(data["current_value_usd"]),
            pnl_usd=float(data["pnl_usd"]),
            pnl_pct=float(data["pnl_pct"]),
            take_profit_pct=float(data["take_profit_pct"]),
            stop_loss_pct=float(data["stop_loss_pct"]),
            status=str(data["status"]),
            opened_at=datetime.fromisoformat(str(data["opened_at"])),
            updated_at=datetime.fromisoformat(str(data["updated_at"])),
            exit_price_usd=(
                float(data["exit_price_usd"])
                if data.get("exit_price_usd") is not None
                else None
            ),
            exit_reason=(
                str(data["exit_reason"]) if data.get("exit_reason") is not None else None
            ),
        )
        if not position.mint or position.entry_price_usd <= 0 or position.entry_value_usd <= 0:
            raise ValueError("position identity and entry values must be positive")
        if position.status not in {"OPEN", "TAKE_PROFIT", "STOP_LOSS"}:
            raise ValueError("unknown paper position status")
        return position
