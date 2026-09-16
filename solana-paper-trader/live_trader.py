from __future__ import annotations

import base64
import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from solana_paper_trader.scanner import DexscreenerClient, MarketDataError, ScannerConfig, TokenScan

SOL_MINT = "So11111111111111111111111111111111111111112"
PRIVY_BASE_URL = "https://api.privy.io"
JUPITER_BASE_URL = "https://api.jup.ag"
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")


class LiveBotError(RuntimeError):
    pass


class FatalLiveBotError(LiveBotError):
    pass


@dataclass(frozen=True)
class Config:
    position_lamports: int = int(os.getenv("POSITION_LAMPORTS", "5000000"))
    reserve_lamports: int = int(os.getenv("RESERVE_LAMPORTS", "15000000"))
    # Fixed live exits requested for this test version.
    take_profit_pct: float = 18.0
    stop_loss_pct: float = -5.0
    scan_interval_seconds: float = min(float(os.getenv("SCAN_INTERVAL_SECONDS", "15")), 15.0)
    # Check open positions at least every 5 seconds to reduce stop overshoot.
    open_poll_seconds: float = min(float(os.getenv("OPEN_POLL_SECONDS", "1")), 1.0)
    scan_limit: int = min(int(os.getenv("SCAN_LIMIT", "50")), 50)
    discovery_pages: int = 5
    discovery_refresh_seconds: float = 60.0
    discovery_min_pool_age_minutes: float = 15.0
    discovery_max_pool_age_minutes: float = 90.0
    max_open_positions: int = min(int(os.getenv("MAX_OPEN_POSITIONS", "3")), 3)
    max_completed_round_trips: int = int(os.getenv("MAX_COMPLETED_ROUND_TRIPS", "0"))
    # Real-money execution protection: never allow stale env vars to loosen these caps.
    max_price_impact_pct: float = min(float(os.getenv("MAX_PRICE_IMPACT_PCT", "1.5")), 1.5)
    # Exit-side slippage cap: separate (looser) from the entry cap because a
    # position MUST eventually be closed, but we still refuse to eat a wildly
    # bad quote — we retry a few times first, then force through so a crashing
    # token can't be held forever.
    exit_max_price_impact_pct: float = 5.0
    exit_force_after_skips: int = 5
    min_roundtrip_return_pct: float = max(float(os.getenv("MIN_ROUNDTRIP_RETURN_PCT", "96.5")), 96.5)
    reject_cooldown_seconds: int = int(os.getenv("REJECT_COOLDOWN_SECONDS", "60"))
    # Entry-quality gate: do not buy every token that merely passes the baseline scanner.
    min_entry_rank: float = float(os.getenv("MIN_ENTRY_RANK", "5"))
    max_entry_rank: float = float(os.getenv("MAX_ENTRY_RANK", "60"))
    min_entry_buy_pressure_pct: float = float(os.getenv("MIN_ENTRY_BUY_PRESSURE_PCT", "2"))
    confirmation_seconds: float = float(os.getenv("ENTRY_CONFIRMATION_SECONDS", "15"))
    min_entry_price_change_5m_pct: float = float(os.getenv("MIN_ENTRY_PRICE_CHANGE_5M_PCT", "1.5"))
    max_entry_price_change_5m_pct: float = float(os.getenv("MAX_ENTRY_PRICE_CHANGE_5M_PCT", "40"))
    trailing_activation_pct: float = 8.0
    trailing_distance_pct: float = 4.0
    trailing_floor_pct: float = 3.0
    max_hold_seconds: float = 600.0
    min_holder_count: int = 200
    min_organic_score: float = 10.0
    max_top_holders_pct: float = 30.0

    @property
    def live_enabled(self) -> bool:
        return os.getenv("LIVE_TRADING_ENABLED", "").strip() == "YES_I_UNDERSTAND"

    @property
    def state_path(self) -> Path:
        explicit = os.getenv("LIVE_STATE_PATH")
        if explicit:
            return Path(explicit)
        data_dir = Path("/data")
        if data_dir.exists() and os.access(data_dir, os.W_OK):
            return data_dir / "solana_live_bot_state.json"
        return Path(".live_trader/solana_live_bot_state.json")


class HttpClient:
    def __init__(self, timeout: float = 25.0) -> None:
        self.timeout = timeout

    def json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        merged = {
            "Accept": "application/json",
            "User-Agent": "solana-live-bot/paper-strategy-live",
        }
        if body is not None:
            merged["Content-Type"] = "application/json"
        if headers:
            merged.update(headers)
        request = Request(url, data=data, headers=merged, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as error:
            payload = error.read().decode("utf-8", errors="replace")
            raise LiveBotError(f"HTTP {error.code} from {url}: {payload[:500]}") from error
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise LiveBotError(f"Network/API error from {url}: {error}") from error


class JupiterClient:
    def __init__(self, api_key: str, http: HttpClient) -> None:
        if not api_key:
            raise FatalLiveBotError("Missing JUPITER_API_KEY")
        self.api_key = api_key
        self.http = http
        self._last_request_at = 0.0

    def _wait_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < 1.05:
            time.sleep(1.05 - elapsed)

    def _get(self, path: str, params: dict[str, str]) -> Any:
        self._wait_rate_limit()
        try:
            return self.http.json(
                "GET",
                f"{JUPITER_BASE_URL}{path}?{urlencode(params)}",
                headers={"x-api-key": self.api_key},
            )
        finally:
            self._last_request_at = time.monotonic()

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        self._wait_rate_limit()
        try:
            return self.http.json(
                "POST",
                f"{JUPITER_BASE_URL}{path}",
                headers={"x-api-key": self.api_key},
                body=body,
            )
        finally:
            self._last_request_at = time.monotonic()

    def order(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        *,
        taker: str | None = None,
    ) -> dict[str, Any]:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
        }
        if taker:
            params["taker"] = taker
        payload = self._get("/swap/v2/order", params)
        if not isinstance(payload, dict):
            raise LiveBotError("Jupiter returned an invalid order response")
        if payload.get("errorCode") not in (None, 0, "0"):
            raise LiveBotError(
                f"Jupiter order error: {payload.get('errorCode')} {payload.get('errorMessage', '')}"
            )
        return payload

    def execute(self, signed_transaction: str, request_id: str) -> dict[str, Any]:
        payload = self._post(
            "/swap/v2/execute",
            {"signedTransaction": signed_transaction, "requestId": request_id},
        )
        if not isinstance(payload, dict):
            raise LiveBotError("Jupiter returned an invalid execute response")
        return payload

    def token_info(self, mint: str) -> dict[str, Any]:
        payload = self._get("/tokens/v2/search", {"query": mint})
        if not isinstance(payload, list):
            raise LiveBotError("Jupiter returned invalid token metadata")
        for item in payload:
            if isinstance(item, dict) and str(item.get("id") or "") == mint:
                return item
        raise LiveBotError("Jupiter token metadata not found")


class PrivySigner:
    def __init__(self, app_id: str, app_secret: str, wallet_id: str, http: HttpClient) -> None:
        if not app_id or not app_secret or not wallet_id:
            raise FatalLiveBotError("Missing PRIVY_APP_ID, PRIVY_APP_SECRET or PRIVY_WALLET_ID")
        self.app_id = app_id
        self.app_secret = app_secret
        self.wallet_id = wallet_id
        self.http = http
        auth = base64.b64encode(f"{app_id}:{app_secret}".encode()).decode()
        self.headers = {"Authorization": f"Basic {auth}", "privy-app-id": app_id}

    def wallet(self) -> dict[str, Any]:
        payload = self.http.json(
            "GET",
            f"{PRIVY_BASE_URL}/v1/wallets/{self.wallet_id}",
            headers=self.headers,
        )
        if not isinstance(payload, dict):
            raise FatalLiveBotError("Privy returned an invalid wallet response")
        if payload.get("chain_type") != "solana":
            raise FatalLiveBotError("PRIVY_WALLET_ID is not a Solana wallet")
        return payload

    def sign_transaction(self, transaction_base64: str) -> str:
        payload = self.http.json(
            "POST",
            f"{PRIVY_BASE_URL}/v1/wallets/{self.wallet_id}/rpc",
            headers=self.headers,
            body={
                "method": "signTransaction",
                "params": {"transaction": transaction_base64, "encoding": "base64"},
            },
        )
        try:
            signed = str(payload["data"]["signed_transaction"])
        except (KeyError, TypeError) as error:
            raise LiveBotError(f"Privy did not return a signed transaction: {payload}") from error
        if not signed:
            raise LiveBotError("Privy returned an empty signed transaction")
        return signed


class StateStore:
    version = 2

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._load()

    def _default(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "open_positions": {},
            "seen_mints": [],
            "rejected_until": {},
            "completed_round_trips": 0,
            "wins": 0,
            "losses": 0,
            "net_realized_pnl_lamports": 0,
            "last_trade": None,
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default()

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise FatalLiveBotError(f"Cannot safely load live state: {error}") from error

        if not isinstance(payload, dict):
            raise FatalLiveBotError("Live state file is not a JSON object")

        old_version = int(payload.get("version") or 1)

        if old_version not in (1, 2):
            raise FatalLiveBotError("Live state file has an unsupported format/version")

        if old_version == 1:
            old_open = payload.get("open_position")
            open_positions: dict[str, Any] = {}

            if isinstance(old_open, dict) and old_open.get("mint"):
                open_positions[str(old_open["mint"])] = old_open

            migrated = self._default()
            migrated["open_positions"] = open_positions
            migrated["seen_mints"] = list(payload.get("seen_mints") or [])
            migrated["rejected_until"] = dict(payload.get("rejected_until") or {})
            migrated["completed_round_trips"] = int(payload.get("completed_round_trips") or 0)
            migrated["wins"] = int(payload.get("wins") or 0)
            migrated["losses"] = int(payload.get("losses") or 0)
            migrated["net_realized_pnl_lamports"] = int(
                payload.get("net_realized_pnl_lamports") or 0
            )
            migrated["last_trade"] = payload.get("last_trade")
            payload = migrated

        payload.setdefault("version", self.version)
        payload.setdefault("open_positions", {})
        payload.setdefault("seen_mints", [])
        payload.setdefault("rejected_until", {})
        payload.setdefault("completed_round_trips", 0)
        payload.setdefault("wins", 0)
        payload.setdefault("losses", 0)
        payload.setdefault("net_realized_pnl_lamports", 0)
        payload.setdefault("last_trade", None)
        payload["version"] = self.version

        return payload

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix="live-state-",
            suffix=".json",
            dir=self.path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise


class LiveTrader:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.http = HttpClient()
        self.jupiter = JupiterClient(os.getenv("JUPITER_API_KEY", ""), self.http)
        self.signer = PrivySigner(
            os.getenv("PRIVY_APP_ID", ""),
            os.getenv("PRIVY_APP_SECRET", ""),
            os.getenv("PRIVY_WALLET_ID", ""),
            self.http,
        )

        wallet = self.signer.wallet()
        self.wallet_address = str(wallet.get("address", ""))

        if not self.wallet_address:
            raise FatalLiveBotError("Privy wallet has no address")

        self.scanner = DexscreenerClient()
        self.scan_config = ScannerConfig(
            min_liquidity_usd=15_000,
            min_market_cap_usd=12_000,
            min_token_age_minutes=5,
            min_volume_5m_usd=400,
            min_transactions_5m=3,
            min_buys_5m=1,
            max_abs_price_change_5m_pct=250,
            max_volume_to_liquidity_ratio=20,
        )
        self.state = StateStore(config.state_path)
        # Candidate must pass the entry + execution gates twice, separated in time.
        # This intentionally filters tokens that collapse immediately after first detection.
        self._pending_confirmations: dict[str, float] = {}
        self._discovery_cache: list[str] = []
        self._discovery_cache_at: float = 0.0

    def _rpc(self, method: str, params: list[Any]) -> Any:
        payload = self.http.json(
            "POST",
            SOLANA_RPC_URL,
            body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        if not isinstance(payload, dict) or payload.get("error"):
            raise LiveBotError(f"Solana RPC error: {payload}")
        return payload.get("result")

    def sol_balance_lamports(self) -> int:
        result = self._rpc(
            "getBalance",
            [self.wallet_address, {"commitment": "confirmed"}],
        )
        try:
            return int(result["value"])
        except (TypeError, KeyError, ValueError) as error:
            raise LiveBotError(f"Invalid Solana balance response: {result}") from error

    @staticmethod
    def _amount(payload: dict[str, Any], *keys: str) -> int:
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return 0

    def _open_positions(self) -> dict[str, dict[str, Any]]:
        raw = self.state.data.setdefault("open_positions", {})
        if not isinstance(raw, dict):
            raise FatalLiveBotError("open_positions state is invalid")
        return raw

    def _route_safety(self, mint: str) -> list[str]:
        reasons: list[str] = []

        buy_quote = self.jupiter.order(
            SOL_MINT,
            mint,
            self.config.position_lamports,
        )
        buy_out = self._amount(buy_quote, "outAmount")
        buy_impact = float(buy_quote.get("priceImpact") or 0)

        if buy_out <= 0:
            return ["no_buy_route"]

        if abs(buy_impact) > self.config.max_price_impact_pct:
            reasons.append(f"buy_price_impact_{buy_impact:.2f}_pct")

        sell_quote = self.jupiter.order(mint, SOL_MINT, buy_out)
        sell_out = self._amount(sell_quote, "outAmount")
        sell_impact = float(sell_quote.get("priceImpact") or 0)

        if sell_out <= 0:
            reasons.append("no_sell_route")

        if abs(sell_impact) > self.config.max_price_impact_pct:
            reasons.append(f"sell_price_impact_{sell_impact:.2f}_pct")

        if sell_out > 0:
            roundtrip_pct = sell_out / self.config.position_lamports * 100
            if roundtrip_pct < self.config.min_roundtrip_return_pct:
                reasons.append(f"roundtrip_quote_{roundtrip_pct:.1f}_pct")

        return reasons

    def _token_safety_reasons(self, mint: str) -> list[str]:
        info = self.jupiter.token_info(mint)
        reasons: list[str] = []

        if info.get("mintAuthority") not in (None, ""):
            reasons.append("mint_authority_enabled")
        if info.get("freezeAuthority") not in (None, ""):
            reasons.append("freeze_authority_enabled")

        audit = info.get("audit") if isinstance(info.get("audit"), dict) else {}
        if audit.get("isSus") is True:
            reasons.append("jupiter_suspicious")
        if audit.get("mintAuthorityDisabled") is False:
            reasons.append("mint_authority_not_disabled")
        if audit.get("freezeAuthorityDisabled") is False:
            reasons.append("freeze_authority_not_disabled")

        holders = info.get("holderCount")
        try:
            holder_count = int(holders)
        except (TypeError, ValueError):
            holder_count = 0
        if holder_count < self.config.min_holder_count:
            reasons.append(
                f"holders_{holder_count}_below_{self.config.min_holder_count}"
            )

        organic = info.get("organicScore")
        try:
            organic_score = float(organic)
        except (TypeError, ValueError):
            organic_score = 0.0
        if organic_score < self.config.min_organic_score:
            reasons.append(
                f"organic_{organic_score:.1f}_below_{self.config.min_organic_score:.1f}"
            )

        top_holders = audit.get("topHoldersPercentage")
        if top_holders is not None:
            try:
                top_holders_pct = float(top_holders)
                if top_holders_pct > self.config.max_top_holders_pct:
                    reasons.append(
                        f"top_holders_{top_holders_pct:.1f}_above_"
                        f"{self.config.max_top_holders_pct:.1f}"
                    )
            except (TypeError, ValueError):
                reasons.append("invalid_top_holders_pct")

        return reasons

    def _cleanup_rejected_cache(self) -> None:
        rejected_until = self.state.data.setdefault("rejected_until", {})
        now = time.time()

        expired = [
            mint
            for mint, until in list(rejected_until.items())
            if float(until or 0) <= now
        ]

        for mint in expired:
            rejected_until.pop(mint, None)

    def _entry_quality_reasons(self, scan: TokenScan) -> list[str]:
        snapshot = scan.snapshot
        reasons: list[str] = []

        if scan.rank_score < self.config.min_entry_rank:
            reasons.append(
                f"rank_{scan.rank_score:.2f}_below_{self.config.min_entry_rank:.2f}"
            )

        if scan.rank_score > self.config.max_entry_rank:
            reasons.append(
                f"rank_{scan.rank_score:.2f}_above_{self.config.max_entry_rank:.2f}"
            )

        if snapshot.buy_pressure_pct < self.config.min_entry_buy_pressure_pct:
            reasons.append(
                f"buy_pressure_{snapshot.buy_pressure_pct:.1f}_below_"
                f"{self.config.min_entry_buy_pressure_pct:.1f}"
            )

        if snapshot.price_change_5m_pct < self.config.min_entry_price_change_5m_pct:
            reasons.append(
                f"change5m_{snapshot.price_change_5m_pct:.1f}_below_"
                f"{self.config.min_entry_price_change_5m_pct:.1f}"
            )

        if snapshot.price_change_5m_pct > self.config.max_entry_price_change_5m_pct:
            reasons.append(
                f"change5m_{snapshot.price_change_5m_pct:.1f}_above_"
                f"{self.config.max_entry_price_change_5m_pct:.1f}"
            )

        return reasons

    @staticmethod
    def _parse_iso8601(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    def _discover_new_pool_mints(self) -> list[str]:
        now_mono = time.monotonic()
        if (
            self._discovery_cache
            and now_mono - self._discovery_cache_at
            < self.config.discovery_refresh_seconds
        ):
            return list(self._discovery_cache)

        now = datetime.now(timezone.utc)
        candidates: list[tuple[datetime, str]] = []
        seen: set[str] = set()
        page_errors = 0

        for page in range(1, self.config.discovery_pages + 1):
            url = (
                "https://api.geckoterminal.com/api/v2/"
                "networks/solana/new_pools"
                f"?page={page}&include=base_token"
            )
            try:
                payload = self.http.json("GET", url)
            except LiveBotError as error:
                page_errors += 1
                print(
                    f"DISCOVERY PAGE ERROR page={page}: {error}",
                    flush=True,
                )
                continue

            rows = payload.get("data", []) if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                continue

            for row in rows:
                if not isinstance(row, dict):
                    continue
                attrs = row.get("attributes")
                rels = row.get("relationships")
                if not isinstance(attrs, dict) or not isinstance(rels, dict):
                    continue

                base_rel = rels.get("base_token")
                if not isinstance(base_rel, dict):
                    continue
                base_data = base_rel.get("data")
                if not isinstance(base_data, dict):
                    continue

                token_id = str(base_data.get("id") or "")
                if token_id.startswith("solana_"):
                    mint = token_id[len("solana_"):]
                else:
                    mint = token_id

                if not mint or mint == SOL_MINT or mint in seen:
                    continue

                created = self._parse_iso8601(attrs.get("pool_created_at"))
                if created is None:
                    continue

                age_minutes = max((now - created).total_seconds() / 60.0, 0.0)
                if age_minutes < self.config.discovery_min_pool_age_minutes:
                    continue
                if age_minutes > self.config.discovery_max_pool_age_minutes:
                    continue

                seen.add(mint)
                candidates.append((created, mint))

        # Newest qualifying pools first.
        candidates.sort(key=lambda item: item[0], reverse=True)
        mints = [mint for _, mint in candidates]

        # If the keyless new-pool feed is temporarily unavailable, retain a small
        # Dexscreener profile fallback so discovery does not go completely blind.
        if not mints:
            try:
                profiles = self.scanner.latest_solana_profiles(self.config.scan_limit)
                for profile in profiles:
                    mint = str(profile.get("tokenAddress") or "").strip()
                    if mint and mint != SOL_MINT and mint not in seen:
                        seen.add(mint)
                        mints.append(mint)
            except MarketDataError as error:
                print(f"DISCOVERY FALLBACK ERROR: {error}", flush=True)

        self._discovery_cache = mints
        self._discovery_cache_at = now_mono
        print(
            f"DISCOVERY V8: {len(mints)} unique Solana new-pool mints "
            f"from {self.config.discovery_pages} GeckoTerminal pages "
            f"(page_errors={page_errors})",
            flush=True,
        )
        return list(mints)

    def _baseline_reasons(self, snapshot: Any) -> list[str]:
        reasons: list[str] = []
        cfg = self.scan_config

        if not snapshot.mint or snapshot.symbol == "UNKNOWN":
            reasons.append("missing_token_identity")
        if snapshot.price_usd <= 0:
            reasons.append("invalid_price")
        if snapshot.liquidity_usd < cfg.min_liquidity_usd:
            reasons.append("low_liquidity")
        if snapshot.market_cap_usd < cfg.min_market_cap_usd:
            reasons.append("low_or_missing_market_cap")
        if snapshot.token_age_minutes < cfg.min_token_age_minutes:
            reasons.append("token_too_new")
        if snapshot.volume_5m_usd < cfg.min_volume_5m_usd:
            reasons.append("low_5m_volume")
        if snapshot.transactions_5m < cfg.min_transactions_5m:
            reasons.append("low_5m_activity")
        if snapshot.buys_5m < cfg.min_buys_5m:
            reasons.append("no_recent_buys")
        if abs(snapshot.price_change_5m_pct) > cfg.max_abs_price_change_5m_pct:
            reasons.append("extreme_5m_price_change")
        if (
            snapshot.liquidity_usd > 0
            and snapshot.volume_5m_usd / snapshot.liquidity_usd
            > cfg.max_volume_to_liquidity_ratio
        ):
            reasons.append("suspicious_volume_to_liquidity")
        return reasons

    @staticmethod
    def _rank_snapshot(snapshot: Any) -> tuple[float, float, float]:
        volume_intensity = (
            min(snapshot.volume_5m_usd / snapshot.liquidity_usd, 1.0)
            if snapshot.liquidity_usd > 0
            else 0.0
        )
        momentum_score = (
            snapshot.price_change_5m_pct
            + max(snapshot.buy_pressure_pct, 0.0) * 0.25
            + volume_intensity * 10.0
        )
        liquidity_score = math.log10(max(snapshot.liquidity_usd, 1.0))
        rank_score = momentum_score * 0.75 + liquidity_score * 2.5
        return momentum_score, liquidity_score, rank_score

    def _scan_candidates(self) -> list[TokenScan]:
        discovered_mints = self._discover_new_pool_mints()

        scans: list[TokenScan] = []
        evaluated = 0
        baseline_passed = 0
        baseline_reason_counts: dict[str, int] = {}
        near_misses: list[tuple[int, str, str]] = []

        for mint in discovered_mints:
            if evaluated >= self.config.scan_limit:
                break
            evaluated += 1

            try:
                snapshot = self.scanner.token_snapshot(mint)
            except MarketDataError as error:
                print(f"SNAPSHOT ERROR {mint}: {error}", flush=True)
                continue

            if snapshot is None:
                continue

            baseline_reasons = self._baseline_reasons(snapshot)
            for reason in baseline_reasons:
                baseline_reason_counts[reason] = baseline_reason_counts.get(reason, 0) + 1

            if baseline_reasons:
                near_misses.append(
                    (
                        len(baseline_reasons),
                        snapshot.symbol,
                        ",".join(baseline_reasons),
                    )
                )

            momentum_score, liquidity_score, rank_score = self._rank_snapshot(snapshot)

            scan = TokenScan(
                snapshot=snapshot,
                passed_filters=not baseline_reasons,
                rejection_reasons=tuple(baseline_reasons),
                momentum_score=momentum_score,
                liquidity_score=liquidity_score,
                rank_score=rank_score,
            )
            scans.append(scan)
            if not baseline_reasons:
                baseline_passed += 1

        eligible = sorted(
            (item for item in scans if item.passed_filters),
            key=lambda item: item.rank_score,
            reverse=True,
        )

        print(
            f"DISCOVERY V9 SNAPSHOTS: evaluated={evaluated}, "
            f"snapshots={len(scans)}, baseline_passed={baseline_passed}",
            flush=True,
        )
        if baseline_reason_counts:
            ordered_reasons = sorted(
                baseline_reason_counts.items(),
                key=lambda item: item[1],
                reverse=True,
            )
            print(
                "BASELINE REJECT SUMMARY: "
                + " | ".join(f"{reason}={count}" for reason, count in ordered_reasons),
                flush=True,
            )

        if near_misses:
            near_misses.sort(key=lambda item: item[0])
            preview = near_misses[:5]
            print(
                "BASELINE NEAR MISSES: "
                + " | ".join(
                    f"{symbol}[{reasons}]"
                    for _, symbol, reasons in preview
                ),
                flush=True,
            )

        self._cleanup_rejected_cache()

        seen = set(self.state.data.get("seen_mints", []))
        rejected_until = self.state.data.setdefault("rejected_until", {})
        open_positions = self._open_positions()
        now = time.time()
        candidates: list[TokenScan] = []

        pipeline_signal_pass = 0
        pipeline_token_safety_pass = 0
        pipeline_execution_pass = 0
        pipeline_confirm_wait = 0
        pipeline_confirmed = 0
        pipeline_seen_or_open = 0
        pipeline_cooldown = 0

        for scan in eligible:
            mint = scan.snapshot.mint

            if mint in open_positions or mint in seen:
                pipeline_seen_or_open += 1
                continue

            if float(rejected_until.get(mint, 0) or 0) > now:
                pipeline_cooldown += 1
                print(
                    f"SKIP {scan.snapshot.symbol} {mint}: rejected recently",
                    flush=True,
                )
                continue

            quality_reasons = self._entry_quality_reasons(scan)
            if quality_reasons:
                self._pending_confirmations.pop(mint, None)
                rejected_until[mint] = (
                    time.time() + self.config.reject_cooldown_seconds
                )
                self.state.save()
                print(
                    f"SIGNAL REJECT {scan.snapshot.symbol} {mint}: "
                    f"{', '.join(quality_reasons)}",
                    flush=True,
                )
                continue

            pipeline_signal_pass += 1

            try:
                safety_reasons = self._token_safety_reasons(mint)
            except LiveBotError as error:
                self._pending_confirmations.pop(mint, None)
                rejected_until[mint] = (
                    time.time() + self.config.reject_cooldown_seconds
                )
                self.state.save()
                print(
                    f"TOKEN SAFETY ERROR {scan.snapshot.symbol} {mint}: {error}",
                    flush=True,
                )
                continue

            if safety_reasons:
                self._pending_confirmations.pop(mint, None)
                rejected_until[mint] = (
                    time.time() + self.config.reject_cooldown_seconds
                )
                self.state.save()
                print(
                    f"TOKEN SAFETY REJECT {scan.snapshot.symbol} {mint}: "
                    f"{', '.join(safety_reasons)}",
                    flush=True,
                )
                continue

            pipeline_token_safety_pass += 1

            try:
                reasons = self._route_safety(mint)
            except LiveBotError as error:
                self._pending_confirmations.pop(mint, None)
                rejected_until[mint] = (
                    time.time() + self.config.reject_cooldown_seconds
                )
                self.state.save()
                print(
                    f"EXECUTION CHECK ERROR {scan.snapshot.symbol} {mint}: {error}",
                    flush=True,
                )
                continue

            if reasons:
                self._pending_confirmations.pop(mint, None)
                rejected_until[mint] = (
                    time.time() + self.config.reject_cooldown_seconds
                )
                self.state.save()
                print(
                    f"EXECUTION REJECT {scan.snapshot.symbol} {mint}: {', '.join(reasons)}",
                    flush=True,
                )
                continue

            pipeline_execution_pass += 1

            first_pass_at = self._pending_confirmations.get(mint)
            if first_pass_at is None:
                pipeline_confirm_wait += 1
                self._pending_confirmations[mint] = time.time()
                print(
                    f"CONFIRM WAIT {scan.snapshot.symbol} {mint} | "
                    f"rank={scan.rank_score:.2f} | first clean pass",
                    flush=True,
                )
                continue

            confirmation_age = time.time() - first_pass_at
            if confirmation_age < self.config.confirmation_seconds:
                pipeline_confirm_wait += 1
                print(
                    f"CONFIRM WAIT {scan.snapshot.symbol} {mint} | "
                    f"{confirmation_age:.0f}s/"
                    f"{self.config.confirmation_seconds:.0f}s",
                    flush=True,
                )
                continue

            self._pending_confirmations.pop(mint, None)
            print(
                f"CONFIRMED CANDIDATE {scan.snapshot.symbol} {mint} | "
                f"rank={scan.rank_score:.2f} | "
                f"confirmed_after={confirmation_age:.0f}s",
                flush=True,
            )
            pipeline_confirmed += 1
            candidates.append(scan)

        print(
            "PIPELINE V9: "
            f"discovered={len(discovered_mints)} | "
            f"snapshots={len(scans)} | "
            f"baseline={len(eligible)} | "
            f"signal={pipeline_signal_pass} | "
            f"token_safety={pipeline_token_safety_pass} | "
            f"execution={pipeline_execution_pass} | "
            f"confirm_wait={pipeline_confirm_wait} | "
            f"confirmed={pipeline_confirmed} | "
            f"cooldown={pipeline_cooldown} | "
            f"seen_or_open={pipeline_seen_or_open}",
            flush=True,
        )

        return candidates

    def _execute_order(self, order: dict[str, Any]) -> dict[str, Any]:
        transaction = str(order.get("transaction") or "")
        request_id = str(order.get("requestId") or "")

        if not transaction or not request_id:
            raise LiveBotError("Jupiter order is missing transaction/requestId")

        signed = self.signer.sign_transaction(transaction)
        result = self.jupiter.execute(signed, request_id)

        if result.get("status") != "Success" or int(result.get("code") or 0) != 0:
            raise LiveBotError(
                f"Swap failed: status={result.get('status')} "
                f"code={result.get('code')} "
                f"error={result.get('error')} "
                f"signature={result.get('signature')}"
            )

        return result

    def _can_open_more(self) -> bool:
        return len(self._open_positions()) < self.config.max_open_positions

    def _trade_limit_reached(self) -> bool:
        if self.config.max_completed_round_trips <= 0:
            return False

        return (
            int(self.state.data.get("completed_round_trips", 0))
            >= self.config.max_completed_round_trips
        )

    def _open(self, scan: TokenScan) -> bool:
        if not self.config.live_enabled:
            print(
                "LIVE_TRADING_ENABLED is not armed; candidate found but NO REAL TRADE was sent.",
                flush=True,
            )
            return False

        if not self._can_open_more():
            return False

        balance = self.sol_balance_lamports()
        required = self.config.position_lamports + self.config.reserve_lamports

        if balance < required:
            print(
                f"NO ENTRY {scan.snapshot.symbol}: "
                f"balance={balance / 1e9:.6f} SOL, "
                f"required={required / 1e9:.6f} SOL",
                flush=True,
            )
            return False

        mint = scan.snapshot.mint

        order = self.jupiter.order(
            SOL_MINT,
            mint,
            self.config.position_lamports,
            taker=self.wallet_address,
        )

        price_impact = float(order.get("priceImpact") or 0)

        if abs(price_impact) > self.config.max_price_impact_pct:
            print(
                f"NO ENTRY {scan.snapshot.symbol}: fresh buy price impact "
                f"{price_impact:.2f}% exceeds {self.config.max_price_impact_pct:.2f}%",
                flush=True,
            )
            return False

        print(
            f"BUYING {scan.snapshot.symbol}: "
            f"{self.config.position_lamports / 1e9:.6f} SOL",
            flush=True,
        )

        result = self._execute_order(order)

        token_amount = self._amount(
            result,
            "outputAmountResult",
            "totalOutputAmount",
        )
        sol_spent = self._amount(
            result,
            "inputAmountResult",
            "totalInputAmount",
        )

        if token_amount <= 0 or sol_spent <= 0:
            raise FatalLiveBotError(
                "Buy confirmed but returned amounts are missing. "
                "Bot stopped to avoid an untracked live position."
            )

        opened_at = datetime.now(timezone.utc).isoformat()

        self._open_positions()[mint] = {
            "symbol": scan.snapshot.symbol,
            "mint": mint,
            "token_amount": token_amount,
            "entry_sol_lamports": sol_spent,
            "opened_at": opened_at,
            "buy_signature": str(result.get("signature") or ""),
        }

        seen = list(
            dict.fromkeys(
                [
                    *self.state.data.get("seen_mints", []),
                    mint,
                ]
            )
        )[-5000:]

        self.state.data["seen_mints"] = seen
        self.state.save()

        print(
            f"\033[92m🟢🟢🟢 BUY OPENED — {scan.snapshot.symbol} — "
            f"{self.config.position_lamports / 1e9:.6f} SOL 🟢🟢🟢\033[0m",
            flush=True,
        )
        print(
            f"BUY SUCCESS {scan.snapshot.symbol} | "
            f"signature={result.get('signature')} | "
            f"received_atomic={token_amount}",
            flush=True,
        )

        return True

    def _close_position(
        self,
        mint: str,
        position: dict[str, Any],
        reason: str,
        pnl_pct: float,
    ) -> None:
        amount = int(position["token_amount"])
        entry_sol = int(position["entry_sol_lamports"])

        if not self.config.live_enabled:
            print(
                f"{reason} reached, but live trading is not armed; NO SELL sent.",
                flush=True,
            )
            return

        order = self.jupiter.order(
            mint,
            SOL_MINT,
            amount,
            taker=self.wallet_address,
        )

        exit_impact = float(order.get("priceImpact") or 0)
        skip_count = int(position.get("exit_skip_count", 0))

        if (
            abs(exit_impact) > self.config.exit_max_price_impact_pct
            and skip_count < self.config.exit_force_after_skips
        ):
            position["exit_skip_count"] = skip_count + 1
            self.state.save()
            print(
                f"EXIT SLIPPAGE TOO HIGH {position['symbol']}: "
                f"{exit_impact:.2f}% exceeds {self.config.exit_max_price_impact_pct:.2f}% "
                f"(retry {skip_count + 1}/{self.config.exit_force_after_skips}), holding.",
                flush=True,
            )
            return

        if skip_count >= self.config.exit_force_after_skips and abs(exit_impact) > self.config.exit_max_price_impact_pct:
            print(
                f"EXIT FORCED {position['symbol']}: slippage still {exit_impact:.2f}% "
                f"after {skip_count} retries, selling anyway to avoid indefinite exposure.",
                flush=True,
            )

        print(
            f"SELLING {position['symbol']} because {reason}",
            flush=True,
        )

        result = self._execute_order(order)

        sol_received = self._amount(
            result,
            "outputAmountResult",
            "totalOutputAmount",
        )

        if sol_received <= 0:
            raise FatalLiveBotError(
                "Sell confirmed but returned SOL amount is missing. "
                "Bot stopped for manual reconciliation."
            )

        realized = sol_received - entry_sol
        completed = int(self.state.data.get("completed_round_trips", 0)) + 1
        wins = int(self.state.data.get("wins", 0))
        losses = int(self.state.data.get("losses", 0))
        net_pnl = (
            int(self.state.data.get("net_realized_pnl_lamports", 0))
            + realized
        )

        if realized > 0:
            wins += 1
        elif realized < 0:
            losses += 1

        self.state.data["completed_round_trips"] = completed
        self.state.data["wins"] = wins
        self.state.data["losses"] = losses
        self.state.data["net_realized_pnl_lamports"] = net_pnl

        self.state.data["last_trade"] = {
            **position,
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "sell_signature": str(result.get("signature") or ""),
            "exit_reason": reason,
            "sol_received_lamports": sol_received,
            "realized_pnl_lamports": realized,
            "realized_pnl_pct": realized / entry_sol * 100,
        }

        self._open_positions().pop(mint, None)
        self.state.save()

        result_emoji = "✅" if realized > 0 else "❌"
        print(
            f"{result_emoji} SELL SUCCESS {position['symbol']} | {reason} | "
            f"realized={realized / 1e9:+.6f} SOL "
            f"({realized / entry_sol * 100:+.2f}%) | "
            f"signature={result.get('signature')} | "
            f"SUMMARY Trades={completed} Wins={wins} Losses={losses} "
            f"NetP/L={net_pnl / 1e9:+.6f} SOL",
            flush=True,
        )

    def _manage_open_positions(self) -> None:
        open_positions = list(self._open_positions().items())

        for mint, position in open_positions:
            amount = int(position["token_amount"])
            entry_sol = int(position["entry_sol_lamports"])

            try:
                quote = self.jupiter.order(
                    mint,
                    SOL_MINT,
                    amount,
                )
            except LiveBotError as error:
                print(
                    f"OPEN {position['symbol']}: quote error {error}; will retry.",
                    flush=True,
                )
                continue

            executable_sol = self._amount(quote, "outAmount")

            if executable_sol <= 0:
                print(
                    f"OPEN {position['symbol']}: no executable sell quote; will retry.",
                    flush=True,
                )
                continue

            pnl_pct = (executable_sol / entry_sol - 1) * 100
            previous_peak = float(position.get("peak_pnl_pct", pnl_pct))
            peak_pnl_pct = max(previous_peak, pnl_pct)

            if peak_pnl_pct != previous_peak:
                current = self._open_positions().get(mint)
                if isinstance(current, dict):
                    current["peak_pnl_pct"] = peak_pnl_pct
                    self.state.save()

            try:
                opened_at = datetime.fromisoformat(
                    str(position.get("opened_at", "")).replace("Z", "+00:00")
                )
                held_seconds = max(
                    0.0,
                    (datetime.now(timezone.utc) - opened_at).total_seconds(),
                )
            except (TypeError, ValueError):
                held_seconds = 0.0

            print(
                f"OPEN {position['symbol']} | "
                f"executable P/L={pnl_pct:+.2f}% | "
                f"peak={peak_pnl_pct:+.2f}% | "
                f"quote={executable_sol / 1e9:.6f} SOL",
                flush=True,
            )

            if pnl_pct >= self.config.take_profit_pct:
                self._close_position(
                    mint,
                    position,
                    "TAKE_PROFIT",
                    pnl_pct,
                )
            elif pnl_pct <= self.config.stop_loss_pct:
                self._close_position(
                    mint,
                    position,
                    "STOP_LOSS",
                    pnl_pct,
                )
            elif held_seconds >= self.config.max_hold_seconds and pnl_pct <= 0:
                self._close_position(
                    mint,
                    position,
                    "TIME_STOP",
                    pnl_pct,
                )

    def _open_new_candidates(self) -> None:
        if self._trade_limit_reached() or not self._can_open_more():
            return

        candidates = self._scan_candidates()

        for scan in candidates:
            if self._trade_limit_reached() or not self._can_open_more():
                break
            self._open(scan)

    def run(self) -> None:
        print("=" * 72, flush=True)
        print(
            "SOLANA LIVE BOT — V9 HIGH-FLOW SIGNAL / STRICT EXECUTION",
            flush=True,
        )
        print(f"Privy wallet: {self.wallet_address}", flush=True)
        print(f"Live armed: {self.config.live_enabled}", flush=True)
        print(
            f"Position per entry: "
            f"{self.config.position_lamports / 1e9:.6f} SOL",
            flush=True,
        )
        print(
            f"TP/SL: +{self.config.take_profit_pct:.1f}% / "
            f"{self.config.stop_loss_pct:.1f}%",
            flush=True,
        )
        print(
            f"Token safety: holders>={self.config.min_holder_count}, "
            f"organic>={self.config.min_organic_score:.1f}, "
            f"top holders<={self.config.max_top_holders_pct:.1f}%",
            flush=True,
        )
        print(
            "Baseline scanner: liquidity>=$15k, market cap>=$12k, "
            "age>=5m, 5m volume>=$400, tx5m>=3, buys5m>=1",
            flush=True,
        )
        print(
            f"Candidate pool: up to {self.config.scan_limit} per scan, "
            f"reject cooldown={self.config.reject_cooldown_seconds}s",
            flush=True,
        )
        print(
            f"Discovery V8: GeckoTerminal Solana new_pools, "
            f"pages=1..{self.config.discovery_pages}, "
            f"pool age={self.config.discovery_min_pool_age_minutes:.0f}.."
            f"{self.config.discovery_max_pool_age_minutes:.0f}m, "
            f"refresh={self.config.discovery_refresh_seconds:.0f}s",
            flush=True,
        )
        print(
            f"Max simultaneous positions: {self.config.max_open_positions}",
            flush=True,
        )
        print(
            f"Entry gate: rank={self.config.min_entry_rank:.1f}.."
            f"{self.config.max_entry_rank:.1f}, "
            f"buy pressure>={self.config.min_entry_buy_pressure_pct:.1f}%, "
            f"5m change={self.config.min_entry_price_change_5m_pct:.1f}%.."
            f"{self.config.max_entry_price_change_5m_pct:.1f}%",
            flush=True,
        )
        print(
            f"Execution gate: max impact={self.config.max_price_impact_pct:.1f}%, "
            f"min roundtrip={self.config.min_roundtrip_return_pct:.1f}%",
            flush=True,
        )
        print(
            f"Confirmation gate: 2 clean scans, >="
            f"{self.config.confirmation_seconds:.0f}s apart",
            flush=True,
        )
        print(
            "Max completed round trips: "
            + (
                "unlimited"
                if self.config.max_completed_round_trips <= 0
                else str(self.config.max_completed_round_trips)
            ),
            flush=True,
        )
        print(f"State: {self.config.state_path}", flush=True)
        print("Private key/seed is NOT used by this program.", flush=True)
        print("=" * 72, flush=True)

        last_scan_at = 0.0

        while True:
            try:
                if self._open_positions():
                    self._manage_open_positions()

                now = time.monotonic()

                if now - last_scan_at >= self.config.scan_interval_seconds:
                    self._open_new_candidates()
                    last_scan_at = now

                time.sleep(self.config.open_poll_seconds)

            except MarketDataError as error:
                print(
                    f"Market-data error: {error}; retrying.",
                    flush=True,
                )
                time.sleep(
                    max(
                        getattr(
                            error,
                            "retry_after_seconds",
                            10,
                        ),
                        10,
                    )
                )
            except FatalLiveBotError:
                raise
            except LiveBotError as error:
                print(
                    f"Live bot recoverable error: {error}; retrying.",
                    flush=True,
                )
                time.sleep(15)


def main() -> int:
    config = Config()

    if config.position_lamports <= 0 or config.reserve_lamports < 0:
        raise SystemExit("Invalid position/reserve configuration")

    if config.take_profit_pct <= 0 or config.stop_loss_pct >= 0:
        raise SystemExit(
            "TAKE_PROFIT_PCT must be positive and STOP_LOSS_PCT negative"
        )

    if config.scan_interval_seconds <= 0 or config.open_poll_seconds <= 0:
        raise SystemExit("Scan/open-poll intervals must be positive")

    if config.max_open_positions <= 0:
        raise SystemExit("MAX_OPEN_POSITIONS must be positive")

    if config.min_entry_rank >= config.max_entry_rank:
        raise SystemExit("MIN_ENTRY_RANK must be below MAX_ENTRY_RANK")

    if config.confirmation_seconds < 0:
        raise SystemExit("ENTRY_CONFIRMATION_SECONDS cannot be negative")

    if config.max_completed_round_trips < 0:
        raise SystemExit("MAX_COMPLETED_ROUND_TRIPS cannot be negative")

    trader = LiveTrader(config)
    trader.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
