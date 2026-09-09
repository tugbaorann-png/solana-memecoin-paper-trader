from __future__ import annotations

import base64
import json
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
STANDARD_SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
PRIVY_BASE_URL = "https://api.privy.io"
JUPITER_BASE_URL = "https://api.jup.ag"
SWAP_BASE_URL = f"{JUPITER_BASE_URL}/swap/v2"
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")


class LiveBotError(RuntimeError):
    pass


class FatalLiveBotError(LiveBotError):
    pass


@dataclass(frozen=True)
class Config:
    position_lamports: int = int(os.getenv("POSITION_LAMPORTS", "5000000"))  # 0.005 SOL
    reserve_lamports: int = int(os.getenv("RESERVE_LAMPORTS", "15000000"))  # 0.015 SOL
    take_profit_pct: float = float(os.getenv("TAKE_PROFIT_PCT", "20"))
    stop_loss_pct: float = float(os.getenv("STOP_LOSS_PCT", "-10"))
    scan_interval_seconds: float = float(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
    open_poll_seconds: float = float(os.getenv("OPEN_POLL_SECONDS", "2"))
    scan_limit: int = int(os.getenv("SCAN_LIMIT", "20"))
    max_price_impact_pct: float = float(os.getenv("MAX_PRICE_IMPACT_PCT", "2.5"))
    min_roundtrip_return_pct: float = float(os.getenv("MIN_ROUNDTRIP_RETURN_PCT", "94"))
    min_organic_score: float = float(os.getenv("MIN_ORGANIC_SCORE", "35"))
    min_holder_count: int = int(os.getenv("MIN_HOLDER_COUNT", "300"))
    max_top_holders_pct: float = float(os.getenv("MAX_TOP_HOLDERS_PCT", "60"))
    max_dev_balance_pct: float = float(os.getenv("MAX_DEV_BALANCE_PCT", "20"))
    max_completed_round_trips: int = int(os.getenv("MAX_COMPLETED_ROUND_TRIPS", "3"))

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
        merged = {"Accept": "application/json", "User-Agent": "solana-live-bot/first-live-test"}
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

    def token_info(self, mint: str) -> dict[str, Any] | None:
        payload = self._get("/tokens/v2/search", {"query": mint})
        if not isinstance(payload, list):
            return None
        for item in payload:
            if isinstance(item, dict) and str(item.get("id")) == mint:
                return item
        return None

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
            raise LiveBotError(f"Jupiter order error: {payload.get('errorCode')} {payload.get('errorMessage', '')}")
        return payload

    def execute(self, signed_transaction: str, request_id: str) -> dict[str, Any]:
        payload = self._post(
            "/swap/v2/execute",
            {"signedTransaction": signed_transaction, "requestId": request_id},
        )
        if not isinstance(payload, dict):
            raise LiveBotError("Jupiter returned an invalid execute response")
        return payload


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
    version = 1

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._load()

    def _default(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "open_position": None,
            "seen_mints": [],
            "completed_round_trips": 0,
            "last_trade": None,
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise FatalLiveBotError(f"Cannot safely load live state: {error}") from error
        if not isinstance(payload, dict) or payload.get("version") != self.version:
            raise FatalLiveBotError("Live state file has an unsupported format/version")
        payload.setdefault("open_position", None)
        payload.setdefault("seen_mints", [])
        payload.setdefault("completed_round_trips", 0)
        payload.setdefault("last_trade", None)
        return payload

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="live-state-", suffix=".json", dir=self.path.parent)
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
        self.scan_config = ScannerConfig(min_liquidity_usd=25_000)
        self.state = StateStore(config.state_path)

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
        result = self._rpc("getBalance", [self.wallet_address, {"commitment": "confirmed"}])
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

    def _token_safety_reasons(self, mint: str) -> tuple[dict[str, Any] | None, list[str]]:
        info = self.jupiter.token_info(mint)
        if info is None:
            return None, ["missing_jupiter_token_info"]
        reasons: list[str] = []
        audit = info.get("audit") if isinstance(info.get("audit"), dict) else {}
        tags = {str(tag).lower() for tag in (info.get("tags") or [])}

        if "banned" in tags or str(info.get("verification", "")).lower() == "banned":
            reasons.append("jupiter_banned")
        if audit.get("isSus") is True:
            reasons.append("jupiter_suspicious")
        if audit.get("mintAuthorityDisabled") is not True:
            reasons.append("mint_authority_not_confirmed_disabled")
        if audit.get("freezeAuthorityDisabled") is not True:
            reasons.append("freeze_authority_not_confirmed_disabled")
        if str(info.get("tokenProgram", "")) != STANDARD_SPL_TOKEN_PROGRAM:
            reasons.append("non_standard_token_program")

        organic = float(info.get("organicScore") or 0)
        holders = int(info.get("holderCount") or 0)
        if organic < self.config.min_organic_score:
            reasons.append(f"organic_score_{organic:.1f}_below_{self.config.min_organic_score:.1f}")
        if holders < self.config.min_holder_count:
            reasons.append(f"holders_{holders}_below_{self.config.min_holder_count}")

        top_holders = audit.get("topHoldersPercentage")
        if top_holders is not None and float(top_holders) > self.config.max_top_holders_pct:
            reasons.append(f"top_holders_{float(top_holders):.1f}_pct")
        dev_balance = audit.get("devBalancePercentage")
        if dev_balance is not None and float(dev_balance) > self.config.max_dev_balance_pct:
            reasons.append(f"dev_balance_{float(dev_balance):.1f}_pct")
        return info, reasons

    def _route_safety(self, mint: str) -> tuple[dict[str, Any] | None, list[str]]:
        reasons: list[str] = []
        buy_quote = self.jupiter.order(SOL_MINT, mint, self.config.position_lamports)
        buy_out = self._amount(buy_quote, "outAmount")
        buy_impact = float(buy_quote.get("priceImpact") or 0)
        if buy_out <= 0:
            reasons.append("no_buy_route")
            return None, reasons
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
        return buy_quote, reasons

    def _candidate(self) -> TokenScan | None:
        result = self.scanner.scan(limit=self.config.scan_limit, config=self.scan_config)
        seen = set(self.state.data.get("seen_mints", []))

        rejected_until = self.state.data.setdefault("rejected_until", {})
        now = time.time()

        # Süresi dolan reddedilmiş coinleri temizle
        expired = [
            mint
            for mint, until in rejected_until.items()
            if float(until) <= now
        ]
        for mint in expired:
            rejected_until.pop(mint, None)

        for scan in result.eligible:
            mint = scan.snapshot.mint

            if mint in seen:
                continue

            # Son 15 dakika içinde reddedildiyse başka adaya geç
            if float(rejected_until.get(mint, 0)) > now:
                print(
                    f"SKIP {scan.snapshot.symbol} {mint}: rejected recently",
                    flush=True,
                )
                continue

            try:
                info, reasons = self._token_safety_reasons(mint)

                if reasons:
                    rejected_until[mint] = time.time() + 900
                    self.state.save()
                    print(
                        f"REJECT {scan.snapshot.symbol} {mint}: {', '.join(reasons)}",
                        flush=True,
                    )
                    continue

                _, route_reasons = self._route_safety(mint)

                if route_reasons:
                    rejected_until[mint] = time.time() + 900
                    self.state.save()
                    print(
                        f"REJECT {scan.snapshot.symbol} {mint}: {', '.join(route_reasons)}",
                        flush=True,
                    )
                    continue

                organic = float((info or {}).get("organicScore") or 0)
                holders = int((info or {}).get("holderCount") or 0)

                print(
                    f"SAFE CANDIDATE {scan.snapshot.symbol} {mint} | "
                    f"rank={scan.rank_score:.2f} organic={organic:.1f} holders={holders}",
                    flush=True,
                )
                return scan

            except LiveBotError as error:
                rejected_until[mint] = time.time() + 900
                self.state.save()
                print(
                    f"Candidate validation error for {scan.snapshot.symbol}: {error}",
                    flush=True,
                )
                continue

        return None    

    def _execute_order(self, order: dict[str, Any]) -> dict[str, Any]:
        transaction = str(order.get("transaction") or "")
        request_id = str(order.get("requestId") or "")
        if not transaction or not request_id:
            raise LiveBotError("Jupiter order is missing transaction/requestId")
        signed = self.signer.sign_transaction(transaction)
        result = self.jupiter.execute(signed, request_id)
        if result.get("status") != "Success" or int(result.get("code") or 0) != 0:
            raise LiveBotError(
                f"Swap failed: status={result.get('status')} code={result.get('code')} "
                f"error={result.get('error')} signature={result.get('signature')}"
            )
        return result

    def _open(self, scan: TokenScan) -> None:
        if not self.config.live_enabled:
            print("LIVE_TRADING_ENABLED is not armed; candidate found but NO REAL TRADE was sent.", flush=True)
            return
        balance = self.sol_balance_lamports()
        required = self.config.position_lamports + self.config.reserve_lamports
        if balance < required:
            raise LiveBotError(
                f"Insufficient SOL reserve: balance={balance / 1e9:.6f}, required={required / 1e9:.6f} SOL"
            )

        mint = scan.snapshot.mint
        # Fresh order immediately before signing; earlier route checks are not reused.
        order = self.jupiter.order(SOL_MINT, mint, self.config.position_lamports, taker=self.wallet_address)
        price_impact = float(order.get("priceImpact") or 0)
        if abs(price_impact) > self.config.max_price_impact_pct:
            raise LiveBotError(f"Fresh buy price impact too high: {price_impact:.2f}%")

        print(f"BUYING {scan.snapshot.symbol}: {self.config.position_lamports / 1e9:.6f} SOL", flush=True)
        result = self._execute_order(order)
        token_amount = self._amount(result, "outputAmountResult", "totalOutputAmount")
        sol_spent = self._amount(result, "inputAmountResult", "totalInputAmount")
        if token_amount <= 0 or sol_spent <= 0:
            raise FatalLiveBotError(
                "Buy confirmed but returned amounts are missing. Bot stopped to avoid an untracked live position."
            )

        opened_at = datetime.now(timezone.utc).isoformat()
        self.state.data["open_position"] = {
            "symbol": scan.snapshot.symbol,
            "mint": mint,
            "token_amount": token_amount,
            "entry_sol_lamports": sol_spent,
            "opened_at": opened_at,
            "buy_signature": str(result.get("signature") or ""),
        }
        seen = list(dict.fromkeys([*self.state.data.get("seen_mints", []), mint]))[-5000:]
        self.state.data["seen_mints"] = seen
        self.state.save()
        print(
            f"BUY SUCCESS {scan.snapshot.symbol} | signature={result.get('signature')} | "
            f"received_atomic={token_amount}",
            flush=True,
        )

    def _manage_open(self) -> None:
        position = self.state.data.get("open_position")
        if not isinstance(position, dict):
            return
        mint = str(position["mint"])
        amount = int(position["token_amount"])
        entry_sol = int(position["entry_sol_lamports"])

        quote = self.jupiter.order(mint, SOL_MINT, amount)
        executable_sol = self._amount(quote, "outAmount")
        if executable_sol <= 0:
            print(f"OPEN {position['symbol']}: no executable sell quote; will retry.", flush=True)
            return
        pnl_pct = (executable_sol / entry_sol - 1) * 100
        print(
            f"OPEN {position['symbol']} | executable P/L={pnl_pct:+.2f}% | "
            f"quote={executable_sol / 1e9:.6f} SOL",
            flush=True,
        )
        if pnl_pct < self.config.take_profit_pct and pnl_pct > self.config.stop_loss_pct:
            return

        reason = "TAKE_PROFIT" if pnl_pct >= self.config.take_profit_pct else "STOP_LOSS"
        if not self.config.live_enabled:
            print(f"{reason} reached, but live trading is not armed; NO SELL sent.", flush=True)
            return

        order = self.jupiter.order(mint, SOL_MINT, amount, taker=self.wallet_address)
        sell_impact = float(order.get("priceImpact") or 0)

        print(
    f"EXIT ORDER {position['symbol']} | reason={reason} | "
    f"priceImpact={sell_impact:.2f}%",
    flush=True,
       )
        print(f"SELLING {position['symbol']} because {reason}", flush=True)
        result = self._execute_order(order)
        sol_received = self._amount(result, "outputAmountResult", "totalOutputAmount")
        if sol_received <= 0:
            raise FatalLiveBotError(
                "Sell confirmed but returned SOL amount is missing. Bot stopped for manual reconciliation."
            )
        realized = sol_received - entry_sol
        completed = int(self.state.data.get("completed_round_trips", 0)) + 1
        self.state.data["completed_round_trips"] = completed
        self.state.data["last_trade"] = {
            **position,
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "sell_signature": str(result.get("signature") or ""),
            "exit_reason": reason,
            "sol_received_lamports": sol_received,
            "realized_pnl_lamports": realized,
            "realized_pnl_pct": realized / entry_sol * 100,
        }
        self.state.data["open_position"] = None
        self.state.save()
        print(
            f"SELL SUCCESS {position['symbol']} | {reason} | realized={realized / 1e9:+.6f} SOL "
            f"({realized / entry_sol * 100:+.2f}%) | signature={result.get('signature')}",
            flush=True,
        )

    def run(self) -> None:
        print("=" * 72, flush=True)
        print("SOLANA FIRST-LIVE TEST BOT", flush=True)
        print(f"Privy wallet: {self.wallet_address}", flush=True)
        print(f"Live armed: {self.config.live_enabled}", flush=True)
        print(f"Position: {self.config.position_lamports / 1e9:.6f} SOL", flush=True)
        print(f"TP/SL: +{self.config.take_profit_pct:.1f}% / {self.config.stop_loss_pct:.1f}%", flush=True)
        print(f"Max completed round trips: {self.config.max_completed_round_trips}", flush=True)
        print(f"State: {self.config.state_path}", flush=True)
        print("Private key/seed is NOT used by this program.", flush=True)
        print("=" * 72, flush=True)

        while True:
            try:
                if self.state.data.get("open_position"):
                    self._manage_open()
                    time.sleep(self.config.open_poll_seconds)
                    continue

                if int(self.state.data.get("completed_round_trips", 0)) >= self.config.max_completed_round_trips:
                    print("FIRST-LIVE TEST COMPLETE. Trade limit reached; no more entries will be opened.", flush=True)
                    time.sleep(300)
                    continue

                candidate = self._candidate()
                if candidate is not None:
                    self._open(candidate)
                time.sleep(self.config.scan_interval_seconds)
            except MarketDataError as error:
                print(f"Dexscreener error: {error}; retrying.", flush=True)
                time.sleep(max(getattr(error, "retry_after_seconds", 10), 10))
            except FatalLiveBotError:
                raise
            except LiveBotError as error:
                print(f"Live bot recoverable error: {error}; retrying.", flush=True)
                time.sleep(15)


def main() -> int:
    config = Config()
    if config.position_lamports <= 0 or config.reserve_lamports < 0:
        raise SystemExit("Invalid position/reserve configuration")
    if config.take_profit_pct <= 0 or config.stop_loss_pct >= 0:
        raise SystemExit("TAKE_PROFIT_PCT must be positive and STOP_LOSS_PCT negative")
    if config.max_completed_round_trips != 3:
        raise SystemExit("This first-live package intentionally requires MAX_COMPLETED_ROUND_TRIPS=1")
    trader = LiveTrader(config)
    trader.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
