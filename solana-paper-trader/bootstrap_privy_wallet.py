from __future__ import annotations

import base64
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://api.privy.io"
EXTERNAL_ID = "solana_live_bot_v1"
DISPLAY_NAME = "Solana Live Bot"


def request_json(method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    merged = {**headers, "Accept": "application/json"}
    if body is not None:
        merged["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=merged, method=method)
    try:
        with urlopen(request, timeout=25) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw else {}
    except HTTPError as error:
        text = error.read().decode(errors="replace")
        raise RuntimeError(f"Privy HTTP {error.code}: {text[:800]}") from error
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Privy network/API error: {error}") from error


def main() -> int:
    app_id = os.getenv("PRIVY_APP_ID", "").strip()
    app_secret = os.getenv("PRIVY_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise SystemExit("Missing PRIVY_APP_ID or PRIVY_APP_SECRET")

    encoded = base64.b64encode(f"{app_id}:{app_secret}".encode()).decode()
    headers = {"Authorization": f"Basic {encoded}", "privy-app-id": app_id}

    # Reuse the same named wallet if this bootstrap command is run again.
    cursor: str | None = None
    for _ in range(10):
        suffix = f"?{urlencode({'cursor': cursor})}" if cursor else ""
        payload = request_json("GET", f"{BASE_URL}/v1/wallets{suffix}", headers)
        for wallet in payload.get("data", []) if isinstance(payload, dict) else []:
            if (
                isinstance(wallet, dict)
                and wallet.get("chain_type") == "solana"
                and wallet.get("external_id") == EXTERNAL_ID
            ):
                print("PRIVY WALLET READY")
                print(f"PRIVY_WALLET_ID={wallet.get('id')}")
                print(f"SOLANA_ADDRESS={wallet.get('address')}")
                print("No private key or seed was created/exported by this script.")
                return 0
        cursor = payload.get("next_cursor") if isinstance(payload, dict) else None
        if not cursor:
            break

    wallet = request_json(
        "POST",
        f"{BASE_URL}/v1/wallets",
        headers,
        body={
            "chain_type": "solana",
            "display_name": DISPLAY_NAME,
            "external_id": EXTERNAL_ID,
        },
    )
    if not isinstance(wallet, dict) or wallet.get("chain_type") != "solana":
        raise SystemExit(f"Unexpected Privy wallet response: {wallet}")
    print("PRIVY WALLET CREATED")
    print(f"PRIVY_WALLET_ID={wallet.get('id')}")
    print(f"SOLANA_ADDRESS={wallet.get('address')}")
    print("No private key or seed was created/exported by this script.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
