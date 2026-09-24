from __future__ import annotations

import base64
import json
import math
import os
import tempfile
import time
import uuid
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
RUGCHECK_BASE_URL = "https://api.rugcheck.xyz"
GMGN_BASE_URL = "https://openapi.gmgn.ai"
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
# Some newer pump.fun-style launches mint under Token-2022 (Token Extensions)
# instead of the legacy Token program. A CloseAccount instruction sent to the
# WRONG program for a given account is rejected on-chain, which is exactly
# what every one of this bot's close-account attempts has hit so far
# (InvalidAccountData, 100% failure rate across 7 different tokens over 8+
# hours — see _close_token_account, which now reads the account's actual
# owner program via getAccountInfo instead of assuming legacy Token).
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
# CAIP-2 chain id Privy expects for Solana mainnet-beta: the first 32 chars
# of the mainnet genesis hash, per docs.privy.io/wallets/using-wallets/
# solana/send-a-transaction and the CAIP-2 Solana namespace spec
# (namespaces.chainagnostic.org/solana/caip350) — confirmed from both.
SOLANA_MAINNET_CAIP2 = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58_decode(value: str) -> bytes:
    num = 0
    for char in value:
        num = num * 58 + _BASE58_ALPHABET.index(char)
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(value) - len(value.lstrip("1"))
    return b"\x00" * pad + raw


def _encode_compact_u16(n: int) -> bytes:
    out = bytearray()
    while True:
        elem = n & 0x7F
        n >>= 7
        if n:
            out.append(elem | 0x80)
        else:
            out.append(elem)
            break
    return bytes(out)


class LiveBotError(RuntimeError):
    pass


class FatalLiveBotError(LiveBotError):
    pass


@dataclass(frozen=True)
class Config:
    position_lamports: int = int(os.getenv("POSITION_LAMPORTS", "5000000"))
    reserve_lamports: int = int(os.getenv("RESERVE_LAMPORTS", "15000000"))
    # Requested trade size in USD. When > 0, this takes priority over the
    # static POSITION_LAMPORTS above: each buy converts this USD amount to
    # SOL using a live SOL/USD price fetched right before the trade, so the
    # position size tracks $1 even as SOL's price moves — a fixed lamports
    # figure would silently drift off-target over time. Falls back to the
    # static POSITION_LAMPORTS if the live price lookup ever fails
    # (fail-open, same pattern as every other external check in this bot).
    position_usd: float = float(os.getenv("POSITION_USD", "1.0"))
    # Fixed live exits requested for this test version.
    take_profit_pct: float = 18.0
    stop_loss_pct: float = -5.0
    # Rug-pull circuit breaker: if a position's value collapses far beyond
    # the normal stop-loss (dev dumping / liquidity pulled), we bypass the
    # exit slippage retry loop entirely and sell immediately at whatever
    # price is available — waiting during a real liquidity drain only makes
    # the outcome worse, it never recovers like a stale quote would.
    rug_catastrophic_loss_pct: float = -40.0
    scan_interval_seconds: float = min(float(os.getenv("SCAN_INTERVAL_SECONDS", "15")), 15.0)
    # Check open positions at least every 5 seconds to reduce stop overshoot.
    open_poll_seconds: float = min(float(os.getenv("OPEN_POLL_SECONDS", "1")), 1.0)
    scan_limit: int = min(int(os.getenv("SCAN_LIMIT", "50")), 50)
    discovery_pages: int = 5
    discovery_refresh_seconds: float = 60.0
    # Confirmed via CoinGecko's own docs (docs.coingecko.com/demo/reference/
    # latest-pools-network): the fully keyless api.geckoterminal.com/api.
    # coingecko.com endpoint shares its rate limit across every user on the
    # same outbound IP ("not suitable for production or scheduled polling").
    # A free Demo API key (no credit card, coingecko.com/en/api/pricing)
    # gives a dedicated, non-shared 100 calls/min instead. Optional — if
    # unset, discovery keeps working exactly as before, just rate-limited.
    coingecko_api_key: str = os.getenv("COINGECKO_API_KEY", "").strip()
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
    # Was a hardcoded 200 — for tokens whose pools are only 15-90 minutes
    # old (our discovery window), reaching 200 unique holders is rare, so
    # this single gate was very likely the main reason trade frequency was
    # ~1/day: almost every fresh candidate died here before reaching the
    # execution/confirmation stages. Lowered default to 50 and made it
    # tunable via env without a code change. Loosening this raises exposure
    # to thin-holder tokens, which is exactly what the top-holder-% cap,
    # RugCheck LP-lock check and GMGN rat/bundler/insider gates below exist
    # to catch — those are left untouched.
    min_holder_count: int = int(os.getenv("MIN_HOLDER_COUNT", "50"))
    min_organic_score: float = 0.0
    max_top_holders_pct: float = 30.0
    # GMGN smart-money / holder-quality gate. These are read-only checks
    # against GMGN's OpenAPI (holder tagging: rat traders, bundler bots,
    # suspected insiders) — a dimension neither Jupiter's audit nor RugCheck
    # covers. If GMGN_API_KEY is not set, or GMGN is unreachable, this check
    # is skipped entirely rather than blocking trading (fail-open, same
    # pattern as RugCheck).
    max_gmgn_rat_trader_pct: float = float(os.getenv("MAX_GMGN_RAT_TRADER_PCT", "15"))
    max_gmgn_bundler_pct: float = float(os.getenv("MAX_GMGN_BUNDLER_PCT", "40"))
    max_gmgn_insider_pct: float = float(os.getenv("MAX_GMGN_INSIDER_PCT", "20"))
    # Smart-money wallets are NOT made a hard buy requirement — too few fresh
    # tokens have any smart-money holders yet, and requiring it would starve
    # trade flow the same way the old organic-score requirement did (learned
    # the hard way earlier in this bot's history). Instead it's used as a
    # tie-breaker on tokens that are borderline-risky: if GMGN shows ZERO
    # smart-money wallets holding AND any of the rat-trader/bundler/insider
    # ratios are already past this soft fraction of their hard cap (e.g. 0.6
    # of MAX_GMGN_RAT_TRADER_PCT), the token is rejected even though it did
    # not cross the hard cap itself — no smart money + meaningfully elevated
    # risk ratios together are a materially worse combination than either
    # alone. A token with genuinely low risk ratios still passes with zero
    # smart money, exactly as before.
    gmgn_soft_risk_ratio: float = float(os.getenv("GMGN_SOFT_RISK_RATIO", "0.6"))

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

    def sign_and_send_transaction(self, transaction_base64: str) -> str:
        # Confirmed from Privy's own docs (docs.privy.io/api-reference/
        # wallets/solana/sign-and-send-transaction): same wallet RPC
        # endpoint, method "signAndSendTransaction", plus a required
        # "caip2" chain id. Privy signs AND broadcasts in one call and
        # returns the transaction signature/hash.
        payload = self.http.json(
            "POST",
            f"{PRIVY_BASE_URL}/v1/wallets/{self.wallet_id}/rpc",
            headers=self.headers,
            body={
                "method": "signAndSendTransaction",
                "caip2": SOLANA_MAINNET_CAIP2,
                "params": {"transaction": transaction_base64, "encoding": "base64"},
            },
        )
        try:
            data = payload["data"]
            signature = str(data.get("hash") or data.get("signature") or "")
        except (KeyError, TypeError) as error:
            raise LiveBotError(
                f"Privy did not return a signAndSendTransaction result: {payload}"
            ) from error
        if not signature:
            raise LiveBotError("Privy returned an empty transaction signature")
        return signature


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

        # One-time, opt-in stats reset: set RESET_TRADE_STATS=YES in Render's
        # env vars, deploy once, then remove the var again (leaving it set
        # would wipe the counters on every restart). Only zeroes the
        # cumulative trade counters — open positions, seen-mint dedupe and
        # rejection cooldowns are left untouched so nothing else breaks.
        if os.getenv("RESET_TRADE_STATS", "").strip().upper() == "YES":
            payload["completed_round_trips"] = 0
            payload["wins"] = 0
            payload["losses"] = 0
            payload["net_realized_pnl_lamports"] = 0
            payload["last_trade"] = None
            print(
                "TRADE STATS RESET: completed_round_trips/wins/losses/"
                "net_realized_pnl_lamports zeroed (RESET_TRADE_STATS=YES). "
                "Remove this env var now so it doesn't reset again on the "
                "next restart.",
                flush=True,
            )

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
        self._gmgn_api_key = os.getenv("GMGN_API_KEY", "").strip()
        self._gmgn_debug_logs_left = 5
        self._coingecko_api_key = os.getenv("COINGECKO_API_KEY", "").strip()
        self._sol_usd_price: float | None = None
        self._sol_usd_price_at: float = 0.0

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

    def _resolve_position_lamports(self) -> int:
        """Converts config.position_usd to lamports using a live SOL/USD
        price (cached 30s so we don't hit the price API on every scan
        cycle), so trade size tracks the requested USD amount instead of
        drifting as SOL's price moves. Falls back to the static
        POSITION_LAMPORTS if position_usd is disabled (<=0) or the live
        price lookup fails for any reason — fail-open, same pattern as
        every other external check in this bot.
        """
        if self.config.position_usd <= 0:
            return self.config.position_lamports

        now_mono = time.monotonic()
        if self._sol_usd_price is None or now_mono - self._sol_usd_price_at > 30:
            try:
                headers = (
                    {"x-cg-demo-api-key": self._coingecko_api_key}
                    if self._coingecko_api_key
                    else None
                )
                payload = self.http.json(
                    "GET",
                    "https://api.coingecko.com/api/v3/simple/price"
                    "?ids=solana&vs_currencies=usd",
                    headers=headers,
                )
                price = float((payload or {}).get("solana", {}).get("usd") or 0)
                if price > 0:
                    self._sol_usd_price = price
                    self._sol_usd_price_at = now_mono
            except (LiveBotError, TypeError, ValueError, AttributeError) as error:
                print(f"SOL/USD PRICE LOOKUP FAILED: {error}", flush=True)

        if not self._sol_usd_price:
            return self.config.position_lamports

        lamports = int(round(self.config.position_usd / self._sol_usd_price * 1e9))
        # Never size below the static fallback's floor or so small that
        # Jupiter's swap/price-impact math turns unreliable on dust amounts.
        return max(lamports, 1_000_000)

    def _close_token_account(self, mint: str) -> None:
        """Best-effort cleanup: after a full sell, the wallet is left
        holding an now-empty SPL token account for `mint`. Solana locks a
        rent-exempt SOL deposit in that account (confirmed via Solana's
        current, September-2026 rent schedule to be roughly 0.0015 SOL for
        a standard token account) until it is explicitly closed — Jupiter's
        swap API never does this on its own. Reclaiming it immediately
        turns that otherwise-stranded SOL back into usable trading balance,
        which matters a lot at small position sizes. This is a nice-to-have
        cleanup step: any failure here is logged and swallowed, never
        allowed to affect the already-recorded sell.
        """
        try:
            accounts = self._rpc(
                "getTokenAccountsByOwner",
                [
                    self.wallet_address,
                    {"mint": mint},
                    {"encoding": "jsonParsed", "commitment": "confirmed"},
                ],
            )
            entries = (accounts or {}).get("value") if isinstance(accounts, dict) else None
            if not entries:
                return  # nothing left to close

            entry = entries[0]
            token_account_pubkey = str(entry.get("pubkey") or "")
            account_data = entry.get("account", {}) if isinstance(entry, dict) else {}
            parsed = (
                account_data.get("data", {})
                .get("parsed", {})
                .get("info", {})
            )
            remaining = str((parsed.get("tokenAmount") or {}).get("amount") or "0")
            if not token_account_pubkey or remaining != "0":
                return  # not actually empty yet — don't risk it

            # Use the account's ACTUAL owning program (legacy Token or
            # Token-2022), not an assumed one — sending CloseAccount to the
            # wrong program is what caused every prior close attempt to be
            # rejected on-chain with InvalidAccountData. getTokenAccountsByOwner
            # already told us this (it had to know the program to parse the
            # account at all), so read it straight from that same response
            # instead of a second RPC round-trip.
            token_program_id = str(account_data.get("owner") or "") or TOKEN_PROGRAM_ID
            if token_program_id != TOKEN_PROGRAM_ID:
                print(
                    f"TOKEN ACCOUNT {mint}: owned by non-legacy program "
                    f"{token_program_id} (likely Token-2022), using it for close",
                    flush=True,
                )

            blockhash_result = self._rpc(
                "getLatestBlockhash", [{"commitment": "finalized"}]
            )
            recent_blockhash = str(
                ((blockhash_result or {}).get("blockhash"))
                or ((blockhash_result or {}).get("value") or {}).get("blockhash")
                or ""
            )
            if not recent_blockhash:
                raise LiveBotError("No recent blockhash for close-account transaction")

            unsigned_tx = self._build_close_account_transaction(
                token_account_pubkey, recent_blockhash, token_program_id
            )
            signature = self.signer.sign_and_send_transaction(unsigned_tx)
            print(
                f"TOKEN ACCOUNT CLOSED {mint}: rent reclaimed, "
                f"signature={signature}",
                flush=True,
            )
        except Exception as error:  # cleanup must never break the trading loop
            print(f"TOKEN ACCOUNT CLOSE SKIPPED {mint}: {error}", flush=True)

    def _build_close_account_transaction(
        self, token_account_pubkey: str, recent_blockhash: str, token_program_id: str
    ) -> str:
        """Hand-builds a minimal, unsigned legacy Solana transaction
        containing a single Token Program CloseAccount instruction against
        whichever program (`token_program_id`) actually owns the account —
        legacy Token and Token-2022 both use the same CloseAccount layout
        (instruction index 9; accounts: [account_to_close, destination,
        owner], per the SPL Token program spec), base64-encoded for Privy's
        signAndSendTransaction call. No solana/solders SDK is available in
        this environment, so the wire format (message header, compact-u16
        array lengths, account key ordering) is constructed by hand.
        """
        wallet_bytes = _base58_decode(self.wallet_address)
        token_account_bytes = _base58_decode(token_account_pubkey)
        token_program_bytes = _base58_decode(token_program_id)
        blockhash_bytes = _base58_decode(recent_blockhash)

        for name, raw in (
            ("wallet address", wallet_bytes),
            ("token account", token_account_bytes),
            ("token program", token_program_bytes),
            ("recent blockhash", blockhash_bytes),
        ):
            if len(raw) != 32:
                raise LiveBotError(f"Decoded {name} is not 32 bytes ({len(raw)})")

        # Account order: writable signer (wallet) first, then writable
        # non-signer (token account), then readonly non-signer (program).
        account_keys = [wallet_bytes, token_account_bytes, token_program_bytes]
        header = bytes([1, 0, 1])

        message = bytearray()
        message += header
        message += _encode_compact_u16(len(account_keys))
        for key in account_keys:
            message += key
        message += blockhash_bytes

        # CloseAccount(accounts=[to_close, destination, owner], data=[9]).
        # to_close=index 1 (token account), destination=owner=index 0 (wallet).
        instruction_accounts = bytes([1, 0, 0])
        instruction_data = bytes([9])
        message += _encode_compact_u16(1)
        message += bytes([2])  # programIdIndex -> token program
        message += _encode_compact_u16(len(instruction_accounts))
        message += instruction_accounts
        message += _encode_compact_u16(len(instruction_data))
        message += instruction_data

        transaction = bytearray()
        transaction += _encode_compact_u16(1)  # one signature slot
        transaction += bytes(64)  # zero-filled; Privy fills this in
        transaction += message

        return base64.b64encode(bytes(transaction)).decode("ascii")

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
        position_lamports = self._resolve_position_lamports()

        buy_quote = self.jupiter.order(
            SOL_MINT,
            mint,
            position_lamports,
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
            roundtrip_pct = sell_out / position_lamports * 100
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

        reasons.extend(self._rugcheck_reasons(mint))
        reasons.extend(self._gmgn_smart_money_reasons(mint))

        return reasons

    def _rugcheck_reasons(self, mint: str) -> list[str]:
        """Checks RugCheck.xyz's free public report for LP-lock status and
        danger-level risk flags — this catches liquidity-pull rug pulls that
        Jupiter's own token audit does not (organic score is unreliable on
        very fresh tokens, see min_organic_score comments).
        If RugCheck itself is unreachable/rate-limited, we do NOT block
        trading on that alone (mirrors how a GeckoTerminal outage doesn't
        stop discovery) — we just skip this specific check for this token.
        """
        try:
            report = self.http.json(
                "GET",
                f"{RUGCHECK_BASE_URL}/v1/tokens/{mint}/report/summary",
                headers={"Accept": "application/json"},
            )
        except LiveBotError as error:
            print(f"RUGCHECK UNAVAILABLE {mint}: {error}", flush=True)
            return []

        if not isinstance(report, dict):
            return []

        reasons: list[str] = []

        if report.get("rugged") is True:
            reasons.append("rugcheck_rugged")

        risks = report.get("risks")
        if isinstance(risks, list):
            danger_names = [
                str(r.get("name", "risk"))
                for r in risks
                if isinstance(r, dict) and str(r.get("level", "")).lower() == "danger"
            ]
            for name in danger_names[:3]:  # cap how many we stuff into the reason list
                reasons.append(f"rugcheck_danger_{name.replace(' ', '_')}")

        return reasons

    def _gmgn_smart_money_reasons(self, mint: str) -> list[str]:
        """Uses GMGN's OpenAPI holder-tagging data as an additional
        quality/safety signal: a high concentration of "rat trader" wallets
        (churn/wash-style flippers), bundler-bot wallets (coordinated sniper
        bundles at launch), or suspected-insider holdings is a strong tell
        of a coordinated pump-and-dump — a dimension neither Jupiter's audit
        nor RugCheck directly measures.

        GMGN-tagged "smart money" wallet presence is NOT made a hard buy
        requirement — too few fresh tokens have any smart-money holders yet,
        and demanding it starved trade flow the same way the old
        organic-score requirement did (learned the hard way earlier in this
        bot's history). Instead it is used as a tie-breaker on tokens that
        are already borderline-risky: zero smart-money wallets combined with
        a rat-trader/bundler/insider ratio that has already crossed
        config.gmgn_soft_risk_ratio of its hard cap (below the cap, so it
        would otherwise pass) is rejected. A token with genuinely low risk
        ratios still passes with zero smart-money wallets, exactly as
        before — this only tightens the borderline cases.

        If GMGN_API_KEY is not configured, or GMGN is unreachable/rate
        limited, this check is skipped entirely — fail-open, same pattern
        as RugCheck — so a GMGN outage never stops the bot from trading.
        """
        if not self._gmgn_api_key:
            return []

        def _gmgn_get(subpath: str) -> dict | None:
            # Confirmed from GMGN's own gmgn-cli source (dist/client/OpenApiClient.js
            # + signer.js on npm): read-only "Exist Auth" endpoints require the
            # api key under the header "X-APIKEY", plus two mandatory query
            # parameters — timestamp (unix seconds) and client_id (a fresh
            # random UUID per request, NOT the api key itself).
            auth_timestamp = int(time.time())
            auth_client_id = str(uuid.uuid4())
            try:
                resp = self.http.json(
                    "GET",
                    f"{GMGN_BASE_URL}{subpath}"
                    f"?chain=sol&address={mint}"
                    f"&timestamp={auth_timestamp}"
                    f"&client_id={auth_client_id}",
                    headers={
                        "X-APIKEY": self._gmgn_api_key,
                        "Accept": "application/json",
                    },
                )
            except LiveBotError as error:
                # Never let the API key reach the logs, even inside an error
                # message that echoes the request URL back.
                safe_error = str(error).replace(self._gmgn_api_key, "***REDACTED***")
                print(f"GMGN UNAVAILABLE {mint} {subpath}: {safe_error}", flush=True)
                return None
            if not isinstance(resp, dict):
                return None
            if self._gmgn_debug_logs_left > 0:
                self._gmgn_debug_logs_left -= 1
                print(f"GMGN RAW RESPONSE {mint} {subpath}: {json.dumps(resp)[:800]}", flush=True)
            payload = resp.get("data") if isinstance(resp.get("data"), dict) else resp
            return payload if isinstance(payload, dict) else None

        # /v1/token/security: confirmed via GMGN's own published skill docs
        # (github.com/GMGNAI/gmgn-skills, skills/gmgn-token/SKILL.md) to return
        # these risk ratios directly at the top level of the response for a
        # single mint — rat_trader_amount_rate, bundler_trader_amount_rate and
        # suspected_insider_hold_rate as 0..1 fractions (this is the endpoint
        # our code was missing; /v1/market/token_top_holders only returns a
        # raw per-holder list and never had these fields).
        security = _gmgn_get("/v1/token/security")

        reasons: list[str] = []
        rat_pct = bundler_pct = insider_pct = 0.0

        if security is not None:

            def _pct(key: str) -> float:
                value = security.get(key)
                if value in (None, ""):
                    return 0.0
                try:
                    parsed = float(value)
                except (TypeError, ValueError):
                    return 0.0
                # Documented as 0..1 ratios, but normalize defensively in case
                # a chain/version ever reports a 0..100 percentage instead.
                return parsed * 100 if parsed <= 1.0 else parsed

            rat_pct = _pct("rat_trader_amount_rate")
            if rat_pct > self.config.max_gmgn_rat_trader_pct:
                reasons.append(f"gmgn_rat_traders_{rat_pct:.1f}pct")

            bundler_pct = _pct("bundler_trader_amount_rate")
            if bundler_pct > self.config.max_gmgn_bundler_pct:
                reasons.append(f"gmgn_bundlers_{bundler_pct:.1f}pct")

            insider_pct = _pct("suspected_insider_hold_rate")
            if insider_pct > self.config.max_gmgn_insider_pct:
                reasons.append(f"gmgn_insiders_{insider_pct:.1f}pct")

        # /v1/token/info: confirmed via the same GMGN skill docs to carry a
        # wallet_tags_stat object with wallet-tag counts, including
        # wallet_tags_stat.smart_wallets — the count of GMGN-tagged "smart
        # money" wallets currently holding this token.
        info = _gmgn_get("/v1/token/info")
        smart_count = 0
        if info is not None:
            tags_stat = info.get("wallet_tags_stat")
            if isinstance(tags_stat, dict):
                try:
                    smart_count = int(tags_stat.get("smart_wallets") or 0)
                except (TypeError, ValueError):
                    smart_count = 0
                if smart_count > 0:
                    print(
                        f"GMGN SMART MONEY PRESENT {mint}: {smart_count} smart wallet(s) holding",
                        flush=True,
                    )

        # Tie-breaker: only applies to tokens that didn't already trip a hard
        # cap above. Zero smart-money wallets plus any risk ratio already
        # past its soft fraction of the hard cap is rejected — no smart
        # money AND meaningfully elevated risk together is worse than either
        # alone. security must actually have loaded (rat_pct etc. all being
        # 0.0 from a failed fetch must never look like "0% risk, reject").
        if security is not None and smart_count == 0:
            soft_ratio = self.config.gmgn_soft_risk_ratio
            soft_hits = []
            if rat_pct > self.config.max_gmgn_rat_trader_pct * soft_ratio:
                soft_hits.append(f"rat_{rat_pct:.1f}pct")
            if bundler_pct > self.config.max_gmgn_bundler_pct * soft_ratio:
                soft_hits.append(f"bundlers_{bundler_pct:.1f}pct")
            if insider_pct > self.config.max_gmgn_insider_pct * soft_ratio:
                soft_hits.append(f"insiders_{insider_pct:.1f}pct")
            if soft_hits and not reasons:
                reasons.append(f"gmgn_no_smart_money_elevated_risk_{'_'.join(soft_hits)}")

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

        # Confirmed via CoinGecko's own docs (docs.coingecko.com/demo/
        # reference/latest-pools-network): this is the same on-chain pool
        # data as api.geckoterminal.com's endpoint, same JSON:API response
        # shape (attributes.pool_created_at, relationships.base_token.data.id
        # = "solana_<mint>"), just reachable under CoinGecko's own domain
        # with an optional Demo API key for a dedicated (non-IP-shared)
        # rate limit instead of the keyless endpoint's shared one.
        gecko_headers = (
            {"x-cg-demo-api-key": self.config.coingecko_api_key}
            if self.config.coingecko_api_key
            else None
        )
        for page in range(1, self.config.discovery_pages + 1):
            url = (
                "https://api.coingecko.com/api/v3/onchain/"
                "networks/solana/new_pools"
                f"?page={page}&include=base_token"
            )
            try:
                payload = self.http.json("GET", url, headers=gecko_headers)
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

        position_lamports = self._resolve_position_lamports()

        balance = self.sol_balance_lamports()
        required = position_lamports + self.config.reserve_lamports

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
            position_lamports,
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
            f"{position_lamports / 1e9:.6f} SOL (~${self.config.position_usd:.2f})",
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
            f"{position_lamports / 1e9:.6f} SOL 🟢🟢🟢\033[0m",
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
        is_rug_protection = reason == "RUG_PROTECTION"
        # STOP_LOSS and TIME_STOP are already "cut the loss" decisions, not
        # "wait for a better price" ones — real data from live trading
        # showed the old behavior (retry up to exit_force_after_skips times
        # whenever the fresh exit quote's slippage exceeded
        # exit_max_price_impact_pct, holding the position between retries)
        # let already-losing, still-falling positions keep falling while
        # the bot waited for slippage to improve, turning a nominal -5%
        # stop-loss into realized exits of -16% to -19% in several trades
        # (e.g. MO -19.42%, MOMMY -16.03%). Waiting for a bad quote to
        # improve during an active downtrend doesn't work any better than
        # it does during a rug — see the RUG_PROTECTION case this mirrors —
        # so these two exit reasons now bypass the retry-and-hold gate the
        # same way RUG_PROTECTION already did. TAKE_PROFIT keeps the retry
        # gate: holding a winning position briefly for a cleaner fill is
        # low-urgency compared to holding a losing one hoping it stops
        # falling.
        # TRAILING_STOP is included too: it fires while a position is
        # actively giving back gains from its peak, which is the same
        # "don't wait for a better price during a decline" situation as a
        # stop-loss, just starting from positive territory instead of
        # negative.
        is_loss_cutting_exit = reason in (
            "RUG_PROTECTION",
            "STOP_LOSS",
            "TIME_STOP",
            "TRAILING_STOP",
        )

        if (
            not is_loss_cutting_exit
            and abs(exit_impact) > self.config.exit_max_price_impact_pct
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

        if is_rug_protection:
            print(
                f"🚨 RUG PROTECTION {position['symbol']}: catastrophic drop detected "
                f"({pnl_pct:+.2f}%), selling immediately at best available price "
                f"(impact={exit_impact:.2f}%), skipping normal slippage retry.",
                flush=True,
            )
        elif is_loss_cutting_exit and abs(exit_impact) > self.config.exit_max_price_impact_pct:
            print(
                f"{reason} {position['symbol']}: exit slippage "
                f"{exit_impact:.2f}% exceeds {self.config.exit_max_price_impact_pct:.2f}%, "
                f"selling immediately anyway — price is actively moving against this "
                f"position, waiting for a better quote only risks losing more of it.",
                flush=True,
            )
        elif skip_count >= self.config.exit_force_after_skips and abs(exit_impact) > self.config.exit_max_price_impact_pct:
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

        # Best-effort: reclaim the SPL token account's locked rent now that the
        # position is fully closed. Never allowed to affect the sell already
        # recorded above — failures are logged and swallowed inside the method.
        self._close_token_account(mint)

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

            if pnl_pct <= self.config.rug_catastrophic_loss_pct:
                self._close_position(
                    mint,
                    position,
                    "RUG_PROTECTION",
                    pnl_pct,
                )
            elif pnl_pct >= self.config.take_profit_pct:
                self._close_position(
                    mint,
                    position,
                    "TAKE_PROFIT",
                    pnl_pct,
                )
            elif (
                peak_pnl_pct >= self.config.trailing_activation_pct
                and pnl_pct
                <= max(
                    peak_pnl_pct - self.config.trailing_distance_pct,
                    self.config.trailing_floor_pct,
                )
            ):
                # trailing_activation_pct / trailing_distance_pct /
                # trailing_floor_pct and peak_pnl_pct were already defined
                # and tracked (see above) but nothing ever read them — a
                # position that ran up to, say, +15% with no exit condition
                # for "give some of that back" would just keep being held
                # until it either hit the full +18% TAKE_PROFIT target or
                # round-tripped all the way down to the -5% STOP_LOSS,
                # turning a real gain into a loss. Now: once a position has
                # ever reached +8% (trailing_activation_pct), its exit
                # floor trails 4 points (trailing_distance_pct) below its
                # peak, never below +3% (trailing_floor_pct) once armed —
                # so a run-up gets locked in instead of given back.
                self._close_position(
                    mint,
                    position,
                    "TRAILING_STOP",
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
            "SOLANA LIVE BOT — V13 GMGN SMART-MONEY LAYER",
            flush=True,
        )
        print(f"Privy wallet: {self.wallet_address}", flush=True)
        print(f"Live armed: {self.config.live_enabled}", flush=True)
        if self.config.position_usd > 0:
            print(
                f"Position per entry: ~${self.config.position_usd:.2f} "
                f"(live SOL price, fallback {self.config.position_lamports / 1e9:.6f} SOL)",
                flush=True,
            )
        else:
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
            f"🚨 Rug protection: force-sell immediately if a position drops "
            f"to {self.config.rug_catastrophic_loss_pct:.1f}% (bypasses normal "
            f"exit slippage retries)",
            flush=True,
        )
        print(
            f"Token safety: holders>={self.config.min_holder_count}, "
            f"organic>={self.config.min_organic_score:.1f}, "
            f"top holders<={self.config.max_top_holders_pct:.1f}%",
            flush=True,
        )
        print(
            "RugCheck.xyz integration: LP-lock/rugged status + danger-level "
            "risk flags checked before every buy (catches liquidity-pull rugs)",
            flush=True,
        )
        print(
            "GMGN smart-money layer: "
            + (
                f"ACTIVE (rat traders<={self.config.max_gmgn_rat_trader_pct:.0f}%, "
                f"bundlers<={self.config.max_gmgn_bundler_pct:.0f}%, "
                f"insiders<={self.config.max_gmgn_insider_pct:.0f}%)"
                if self._gmgn_api_key
                else "DISABLED (GMGN_API_KEY not set)"
            ),
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


class _Tee:
    """Mirrors everything written to stdout into a log file as well.

    Every print() call in this bot already goes through stdout with
    flush=True, so this is the one place needed to make that output
    durable across container/session restarts instead of vanishing the
    moment the process exits. Opens in append mode so restarts keep
    history instead of clobbering it, and truncates the file back to its
    last ~5MB if it grows past ~20MB so it can't fill the disk unbounded.
    """

    def __init__(self, stream: Any, log_path: Path) -> None:
        self._stream = stream
        self._log_path = log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(log_path, "a", encoding="utf-8")

    def write(self, data: str) -> int:
        self._stream.write(data)
        try:
            if self._fh.tell() > 20_000_000:
                self._fh.close()
                raw = self._log_path.read_bytes()[-5_000_000:]
                self._log_path.write_bytes(raw)
                self._fh = open(self._log_path, "a", encoding="utf-8")
            self._fh.write(data)
            self._fh.flush()
        except OSError:
            pass
        return len(data)

    def flush(self) -> None:
        self._stream.flush()
        try:
            self._fh.flush()
        except OSError:
            pass


def main() -> int:
    config = Config()

    log_path = config.state_path.parent / "live_trader.log"
    try:
        import sys

        sys.stdout = _Tee(sys.stdout, log_path)
        sys.stderr = _Tee(sys.stderr, log_path)
        print(f"[LOGGING] mirroring console output to {log_path}", flush=True)
    except OSError as error:
        print(f"[LOGGING] could not open log file {log_path}: {error}", flush=True)

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

    # Render's "web service" type requires the process to answer HTTP
    # health checks on $PORT, or Render assumes it crashed and restarts it
    # in a loop — this bot has nothing to do with HTTP, it just needs to
    # not look dead to Render. Only starts when $PORT is actually set
    # (Render sets it; running locally/elsewhere leaves this off).
    port = os.getenv("PORT", "").strip()
    if port:
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class _HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args: Any) -> None:  # silence per-request logging
                pass

        server = HTTPServer(("0.0.0.0", int(port)), _HealthHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"[HEALTHCHECK] listening on 0.0.0.0:{port}", flush=True)

    trader = LiveTrader(config)
    trader.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
