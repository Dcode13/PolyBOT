from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest

from polybot.config import Config
from polybot.scan import scan_config


class ScanProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "scanner.toml"

    def test_profile_widens_analysis_without_mutating_trading_limits(self):
        source = Path(__file__).resolve().parents[1]
        trading = Config()
        before = asdict(trading)
        cfg, overrides = scan_config(trading, source / "scanner.toml")
        self.assertEqual(before, asdict(trading))
        self.assertLess(cfg.min_liquidity, trading.min_liquidity)
        self.assertLess(cfg.min_volume_24h, trading.min_volume_24h)
        self.assertLess(cfg.minimum_confidence, trading.minimum_confidence)
        self.assertGreater(cfg.max_candidates, trading.max_candidates)
        for key, value in before.items():
            if key not in overrides:
                self.assertEqual(value, getattr(cfg, key), key)
        self.assertEqual(1, cfg.entry_dollars)
        self.assertEqual(.5, cfg.minimum_profit)

    def test_position_size_and_safety_controls_cannot_be_overridden(self):
        for content in ('entry_dollars = 5', 'starting_cash = 100', 'max_book_age_seconds = 99999',
                        'state_dir = "other"', 'daily_loss_limit = 100', 'minimum_profit = .01'):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    scan_config(Config(), self.path)

    def test_profile_rejects_bad_types_and_missing_file_uses_original_rules(self):
        original = Config()
        self.assertEqual((original, {}), scan_config(original, self.path))
        for content in ('minimum_probability = 0', 'minimum_confidence = 1.5',
                        'max_candidates = true', 'max_scan_seconds = -5'):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    scan_config(original, self.path)


if __name__ == "__main__":
    unittest.main()
