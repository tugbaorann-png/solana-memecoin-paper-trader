import unittest
from unittest.mock import patch

from solana_paper_trader.engine import PaperTradingEngine
from solana_paper_trader.helius import HeliusReadOnlyClient
from solana_paper_trader.live_paper import LivePaperConfig, LivePaperLedger
from solana_paper_trader.market import SyntheticMarket
from solana_paper_trader.scanner import (
    ScannerConfig,
    TokenScan,
    TokenSnapshot,
    _filter_reasons,
)
from solana_paper_trader.strategy import MomentumStrategy, StrategyConfig


class PaperTradingTests(unittest.TestCase):
    def test_demo_is_deterministic_and_closes_positions(self) -> None:
        config = StrategyConfig(position_size_usd=250)
        first = PaperTradingEngine(10_000, MomentumStrategy(config)).run(
            SyntheticMarket(seed=7).ticks(24)
        )
        second = PaperTradingEngine(10_000, MomentumStrategy(config)).run(
            SyntheticMarket(seed=7).ticks(24)
        )

        self.assertEqual(first.portfolio.snapshot(first.last_prices), second.portfolio.snapshot(second.last_prices))
        self.assertEqual(first.portfolio.positions, {})
        self.assertGreater(len(first.portfolio.trades), 0)

    def test_max_positions_is_respected(self) -> None:
        config = StrategyConfig(
            momentum_window=1,
            entry_momentum_pct=0.01,
            max_positions=1,
            position_size_usd=100,
        )
        engine = PaperTradingEngine(1_000, MomentumStrategy(config))
        ticks = list(SyntheticMarket(seed=2).ticks(5))
        for tick in ticks:
            engine.process_tick(tick)
            self.assertLessEqual(len(engine.portfolio.positions), 1)

    def test_no_private_key_or_live_trade_surface(self) -> None:
        import solana_paper_trader.engine as engine_module

        self.assertFalse(hasattr(engine_module, "send_transaction"))
        self.assertNotIn("private_key", engine_module.__dict__)

    def test_helius_client_uses_read_only_block_height_rpc(self) -> None:
        response = unittest.mock.Mock()
        response.__enter__ = lambda self: self
        response.__exit__ = lambda self, *args: None
        response.read.return_value = b'{"jsonrpc":"2.0","id":"paper-trader-health-check","result":321}'

        with patch("solana_paper_trader.helius.urlopen", return_value=response) as mocked_urlopen:
            block_height = HeliusReadOnlyClient("test-key").get_latest_block_height()

        self.assertEqual(block_height, 321)
        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertIn("getBlockHeight", request.data.decode("utf-8"))
        self.assertNotIn("sendTransaction", request.data.decode("utf-8"))

    def test_scanner_rejects_low_liquidity_and_suspicious_activity(self) -> None:
        snapshot = TokenSnapshot(
            observed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            symbol="RISK",
            mint="RiskMint",
            price_usd=1,
            liquidity_usd=100,
            market_cap_usd=1_000_000,
            token_age_minutes=1,
            volume_5m_usd=100_000,
            volume_1h_usd=200_000,
            price_change_5m_pct=400,
            buys_5m=0,
            sells_5m=1,
            pair_address="Pair",
            dex_id="raydium",
            source_url="https://dexscreener.com/solana/Pair",
        )
        reasons = _filter_reasons(snapshot, ScannerConfig())
        self.assertIn("low_liquidity", reasons)
        self.assertIn("token_too_new", reasons)
        self.assertIn("extreme_5m_price_change", reasons)
        self.assertIn("no_recent_buys", reasons)
        self.assertIn("suspicious_volume_to_liquidity", reasons)

    def test_paper_ledger_records_entry_and_stop_loss(self) -> None:
        from datetime import datetime, timezone

        first = TokenSnapshot(
            observed_at=datetime.now(timezone.utc),
            symbol="GOOD",
            mint="GoodMint",
            price_usd=2,
            liquidity_usd=100_000,
            market_cap_usd=500_000,
            token_age_minutes=60,
            volume_5m_usd=2_000,
            volume_1h_usd=10_000,
            price_change_5m_pct=5,
            buys_5m=8,
            sells_5m=2,
            pair_address="Pair",
            dex_id="raydium",
            source_url="https://dexscreener.com/solana/Pair",
        )
        second = TokenSnapshot(
            **{**first.__dict__, "observed_at": datetime.now(timezone.utc), "price_usd": 1.7}
        )
        eligible = TokenScan(first, True, (), 10, 5, 12)
        update = TokenScan(second, True, (), 10, 5, 12)
        ledger = LivePaperLedger(LivePaperConfig(stop_loss_pct=-10))

        opened = ledger.update([eligible])[0]
        closed = ledger.update([update])[0]

        self.assertEqual(opened.entry_value_usd, 10)
        self.assertAlmostEqual(opened.quantity, 5)
        self.assertEqual(closed.status, "STOP_LOSS")
        self.assertAlmostEqual(closed.pnl_pct, -15)


if __name__ == "__main__":
    unittest.main()
