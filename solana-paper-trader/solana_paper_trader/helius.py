from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class HeliusConfigurationError(RuntimeError):
    """Raised when the read-only Helius client is not configured."""


class HeliusConnectionError(RuntimeError):
    """Raised when Helius cannot complete a read-only JSON-RPC request."""


@dataclass(frozen=True)
class HeliusReadOnlyClient:
    """Minimal Helius mainnet client with no wallet or transaction methods."""

    api_key: str
    timeout_seconds: float = 10.0

    @classmethod
    def from_environment(cls) -> "HeliusReadOnlyClient":
        api_key = os.environ.get("HELIUS_API_KEY", "").strip()
        if not api_key:
            raise HeliusConfigurationError(
                "HELIUS_API_KEY is not set. Add it as an environment secret before running this check."
            )
        return cls(api_key=api_key)

    @property
    def endpoint(self) -> str:
        return "https://mainnet.helius-rpc.com/?" + urlencode({"api-key": self.api_key})

    def get_latest_block_height(self) -> int:
        """Fetch the latest confirmed Solana mainnet block height."""
        payload = {
            "jsonrpc": "2.0",
            "id": "paper-trader-health-check",
            "method": "getBlockHeight",
            "params": [{"commitment": "confirmed"}],
        }
        request = Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise HeliusConnectionError(
                f"Helius returned HTTP {error.code} for the block-height check."
            ) from error
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise HeliusConnectionError(
                "Unable to reach Helius for the Solana block-height check."
            ) from error

        if "error" in body:
            message = _rpc_error_message(body["error"])
            raise HeliusConnectionError(f"Helius JSON-RPC error: {message}")

        result = body.get("result")
        if isinstance(result, bool) or not isinstance(result, int) or result < 0:
            raise HeliusConnectionError("Helius returned an invalid block height.")
        return result


def _rpc_error_message(error: Any) -> str:
    if isinstance(error, dict):
        code = error.get("code", "unknown")
        message = error.get("message", "unknown error")
        return f"{message} (code {code})"
    return str(error)
