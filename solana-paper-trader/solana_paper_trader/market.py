from __future__ import annotations

import csv
import random
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import MarketTick


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


class SyntheticMarket:
    """Deterministic local price generator; it performs no network requests."""

    _TOKENS = (
        ("WIF", "DemoMintWif111111111111111111111111111111111", 0.82, 2_500_000),
        ("BONK", "DemoMintBonk11111111111111111111111111111111", 0.000021, 1_200_000),
        ("POPCAT", "DemoMintPopcat11111111111111111111111111111", 0.44, 850_000),
        ("MEW", "DemoMintMew111111111111111111111111111111111", 0.0065, 42_000),
    )

    def __init__(self, seed: int = 7, interval_minutes: int = 5) -> None:
        self.random = random.Random(seed)
        self.interval = timedelta(minutes=interval_minutes)

    def ticks(self, steps: int = 72) -> Iterator[MarketTick]:
        prices = {symbol: price for symbol, _, price, _ in self._TOKENS}
        timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)

        for _ in range(steps):
            for symbol, mint, starting_price, liquidity in self._TOKENS:
                drift = 0.003 if symbol in {"WIF", "POPCAT"} else 0.001
                shock = self.random.gauss(drift, 0.035)
                if symbol == "MEW":
                    shock += self.random.choice((-0.02, 0.0, 0.03))
                prices[symbol] = max(prices[symbol] * (1 + shock), starting_price * 0.05)
                yield MarketTick(
                    timestamp=timestamp,
                    symbol=symbol,
                    mint=mint,
                    price_usd=prices[symbol],
                    liquidity_usd=liquidity,
                    volume_24h_usd=liquidity * (0.4 + self.random.random()),
                )
            timestamp += self.interval


def read_csv_ticks(path: str | Path) -> Iterable[MarketTick]:
    """Read local observations only. No URLs or remote sources are accepted."""
    csv_path = Path(path)
    with csv_path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        required = {
            "timestamp",
            "symbol",
            "mint",
            "price_usd",
            "liquidity_usd",
            "volume_24h_usd",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")

        for row_number, row in enumerate(reader, start=2):
            try:
                yield MarketTick(
                    timestamp=_parse_timestamp(row["timestamp"]),
                    symbol=row["symbol"].strip().upper(),
                    mint=row["mint"].strip(),
                    price_usd=float(row["price_usd"]),
                    liquidity_usd=float(row["liquidity_usd"]),
                    volume_24h_usd=float(row["volume_24h_usd"]),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"Invalid market row {row_number}: {error}") from error
