import unittest
from unittest.mock import patch

from solana_paper_trader.engine import PaperTradingEngine
from solana_paper_trader.helius import HeliusReadOnlyClient
from solana_paper_trader.market import SyntheticMarket
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


if __name__ == "__main__":
    unittest.main()
