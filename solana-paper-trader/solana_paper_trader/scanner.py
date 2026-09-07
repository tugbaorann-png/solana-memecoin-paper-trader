from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class MarketDataError(RuntimeError):
    """Raised when the public market-data provider cannot be read."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after_seconds: float = 0,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class ScannerConfig:
    """Conservative filters for noisy and potentially suspicious new tokens."""

    min_liquidity_usd: float = 25_000
    min_market_cap_usd: float = 20_000
    min_token_age_minutes: float = 15
    min_volume_5m_usd: float = 1_000
    min_transactions_5m: int = 5
    min_buys_5m: int = 1
    max_abs_price_change_5m_pct: float = 250
    max_volume_to_liquidity_ratio: float = 20


@dataclass(frozen=True)
class TokenSnapshot:
    observed_at: datetime
    symbol: str
    mint: str
    price_usd: float
    liquidity_usd: float
    market_cap_usd: float
    token_age_minutes: float
    volume_5m_usd: float
    volume_1h_usd: float
    price_change_5m_pct: float
    buys_5m: int
    sells_5m: int
    pair_address: str
    dex_id: str
    source_url: str

    @property
    def transactions_5m(self) -> int:
        return self.buys_5m + self.sells_5m

    @property
    def buy_pressure_pct(self) -> float:
        if self.transactions_5m == 0:
            return 0.0
        return (self.buys_5m - self.sells_5m) / self.transactions_5m * 100

    def to_dict(self) -> dict[str, str | float | int]:
        return {
            "observed_at": self.observed_at.isoformat(),
            "symbol": self.symbol,
            "mint": self.mint,
            "price_usd": self.price_usd,
            "liquidity_usd": self.liquidity_usd,
            "market_cap_usd": self.market_cap_usd,
            "token_age_minutes": self.token_age_minutes,
            "volume_5m_usd": self.volume_5m_usd,
            "volume_1h_usd": self.volume_1h_usd,
            "price_change_5m_pct": self.price_change_5m_pct,
            "buys_5m": self.buys_5m,
            "sells_5m": self.sells_5m,
            "transactions_5m": self.transactions_5m,
            "buy_pressure_pct": self.buy_pressure_pct,
            "pair_address": self.pair_address,
            "dex_id": self.dex_id,
            "source_url": self.source_url,
        }


@dataclass(frozen=True)
class TokenScan:
    snapshot: TokenSnapshot
    passed_filters: bool
    rejection_reasons: tuple[str, ...]
    momentum_score: float
    liquidity_score: float
    rank_score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.snapshot.to_dict(),
            "passed_filters": self.passed_filters,
            "rejection_reasons": list(self.rejection_reasons),
            "momentum_score": self.momentum_score,
            "liquidity_score": self.liquidity_score,
            "rank_score": self.rank_score,
        }


@dataclass(frozen=True)
class LiveScanResult:
    scanned: tuple[TokenScan, ...]
    observed_at: datetime
    provider: str = "Dexscreener public API"

    @property
    def eligible(self) -> list[TokenScan]:
        return sorted(
            (item for item in self.scanned if item.passed_filters),
            key=lambda item: item.rank_score,
            reverse=True,
        )

    @property
    def rejected(self) -> list[TokenScan]:
        return [item for item in self.scanned if not item.passed_filters]


class DexscreenerClient:
    """Read-only client for Dexscreener's public Solana market-data API."""

    base_url = "https://api.dexscreener.com"
    user_agent = "solana-memecoin-paper-trader/0.1"

    def __init__(self, timeout_seconds: float = 15.0) -> None:
        self.timeout_seconds = timeout_seconds

    def latest_solana_profiles(self, limit: int = 20) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        payload = self._get_json("/token-profiles/latest/v1")
        profiles = payload if isinstance(payload, list) else []
        seen: set[str] = set()
        solana_profiles: list[dict[str, Any]] = []
        for profile in profiles:
            if not isinstance(profile, dict) or profile.get("chainId") != "solana":
                continue
            address = str(profile.get("tokenAddress", "")).strip()
            if not address or address in seen:
                continue
            seen.add(address)
            solana_profiles.append(profile)
            if len(solana_profiles) >= limit:
                break
        return solana_profiles

    def token_pairs(self, mint: str) -> list[dict[str, Any]]:
        payload = self._get_json(f"/latest/dex/tokens/{quote(mint, safe='')}")
        pairs = payload.get("pairs", []) if isinstance(payload, dict) else []
        if not isinstance(pairs, list):
            return []
        return [pair for pair in pairs if isinstance(pair, dict)]
        def token_snapshot(self, mint: str) -> TokenSnapshot | None:
        pairs = self.token_pairs(mint)
        solana_pairs = [
            pair
            for pair in pairs
            if pair.get("chainId") == "solana"
            and str(pair.get("baseToken", {}).get("address", "")) == mint
        ]
        pair = _best_liquidity_pair(solana_pairs)
        if pair is None:
            return None
        return _snapshot_from_pair(pair, datetime.now(timezone.utc))
    def scan(self, limit: int = 20, config: ScannerConfig | None = None) -> LiveScanResult:
        scanner_config = config or ScannerConfig()
        observed_at = datetime.now(timezone.utc)
        scans: list[TokenScan] = []
        for profile in self.latest_solana_profiles(limit):
            mint = str(profile["tokenAddress"])
            pairs = [
                pair
                for pair in self.token_pairs(mint)
                if pair.get("chainId") == "solana"
                and str(pair.get("baseToken", {}).get("address", "")) == mint
            ]
            pair = _best_liquidity_pair(pairs)
            if pair is None:
                continue
            snapshot = _snapshot_from_pair(pair, observed_at)
            reasons = _filter_reasons(snapshot, scanner_config)
            momentum_score, liquidity_score, rank_score = _rank_scores(snapshot)
            scans.append(
                TokenScan(
                    snapshot=snapshot,
                    passed_filters=not reasons,
                    rejection_reasons=tuple(reasons),
                    momentum_score=momentum_score,
                    liquidity_score=liquidity_score,
                    rank_score=rank_score,
                )
            )
        return LiveScanResult(scanned=tuple(scans), observed_at=observed_at)

    def _get_json(self, path: str) -> Any:
        request = Request(
            self.base_url + path,
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            if error.code == 429:
                retry_after = _number(error.headers.get("Retry-After"))
                raise MarketDataError(
                    "Dexscreener rate limit reached; the paper loop will back off and retry.",
                    retryable=True,
                    retry_after_seconds=max(retry_after, 1),
                ) from error
            raise MarketDataError(f"Dexscreener returned HTTP {error.code}.") from error
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
                raise MarketDataError(
                "Unable to read Dexscreener public market data.",
                retryable=True,
                retry_after_seconds=10,
                        ) from error


def _best_liquidity_pair(pairs: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not pairs:
        return None
    return max(pairs, key=lambda pair: _number(pair.get("liquidity", {}).get("usd")))


def _snapshot_from_pair(pair: dict[str, Any], observed_at: datetime) -> TokenSnapshot:
    base_token = pair.get("baseToken", {})
    txns = pair.get("txns", {}).get("m5", {})
    volume = pair.get("volume", {})
    price_change = pair.get("priceChange", {}) or {}
    created_at_ms = pair.get("pairCreatedAt")
    age_minutes = (
        max((observed_at.timestamp() * 1000 - float(created_at_ms)) / 60_000, 0)
        if created_at_ms
        else 0
    )
    return TokenSnapshot(
        observed_at=observed_at,
        symbol=str(base_token.get("symbol") or "UNKNOWN"),
        mint=str(base_token.get("address") or ""),
        price_usd=_number(pair.get("priceUsd")),
        liquidity_usd=_number(pair.get("liquidity", {}).get("usd")),
        market_cap_usd=_number(pair.get("marketCap") or pair.get("fdv")),
        token_age_minutes=age_minutes,
        volume_5m_usd=_number(volume.get("m5")),
        volume_1h_usd=_number(volume.get("h1")),
        price_change_5m_pct=_number(price_change.get("m5")),
        buys_5m=_integer(txns.get("buys")),
        sells_5m=_integer(txns.get("sells")),
        pair_address=str(pair.get("pairAddress") or ""),
        dex_id=str(pair.get("dexId") or "unknown"),
        source_url=str(pair.get("url") or ""),
    )


def _filter_reasons(snapshot: TokenSnapshot, config: ScannerConfig) -> list[str]:
    reasons: list[str] = []
    if not snapshot.mint or snapshot.symbol == "UNKNOWN":
        reasons.append("missing_token_identity")
    if snapshot.price_usd <= 0:
        reasons.append("invalid_price")
    if snapshot.liquidity_usd < config.min_liquidity_usd:
        reasons.append("low_liquidity")
    if snapshot.market_cap_usd < config.min_market_cap_usd:
        reasons.append("low_or_missing_market_cap")
    if snapshot.token_age_minutes < config.min_token_age_minutes:
        reasons.append("token_too_new")
    if snapshot.volume_5m_usd < config.min_volume_5m_usd:
        reasons.append("low_5m_volume")
    if snapshot.transactions_5m < config.min_transactions_5m:
        reasons.append("low_5m_activity")
    if snapshot.buys_5m < config.min_buys_5m:
        reasons.append("no_recent_buys")
    if abs(snapshot.price_change_5m_pct) > config.max_abs_price_change_5m_pct:
        reasons.append("extreme_5m_price_change")
    if (
        snapshot.liquidity_usd > 0
        and snapshot.volume_5m_usd / snapshot.liquidity_usd
        > config.max_volume_to_liquidity_ratio
    ):
        reasons.append("suspicious_volume_to_liquidity")
    return reasons


def _rank_scores(snapshot: TokenSnapshot) -> tuple[float, float, float]:
    """Combine short-term momentum and liquidity without hiding the components."""
    volume_intensity = (
        min(snapshot.volume_5m_usd / snapshot.liquidity_usd, 1)
        if snapshot.liquidity_usd > 0
        else 0
    )
    momentum_score = (
        snapshot.price_change_5m_pct
        + max(snapshot.buy_pressure_pct, 0) * 0.25
        + volume_intensity * 10
    )
    liquidity_score = math.log10(max(snapshot.liquidity_usd, 1))
    rank_score = momentum_score * 0.75 + liquidity_score * 2.5
    return momentum_score, liquidity_score, rank_score


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _integer(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0
