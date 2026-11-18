"""Offline integration tests: no Telegram or Bybit network calls."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# aiogram validates token format but does not call Telegram during import.
os.environ.setdefault("TELEGRAM_TOKEN", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
os.environ.setdefault("BYBIT_TESTNET", "true")
os.environ.setdefault("TRADING_ENABLED", "false")
os.environ.setdefault("RAILWAY_VOLUME_MOUNT_PATH", tempfile.mkdtemp(prefix="tradeai-tests-"))

import bot
import database


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = database.DB_PATH
        database.DB_PATH = Path(self.tmp.name) / "test.db"
        database.init_db()
        bot.PORTFOLIO_LOCK = asyncio.Lock()
        self.signal = {"direction": "LONG", "entry": 100, "stop_loss": 98,
                       "take_profit": 104, "signal_candle": "2026-01-01T00:00:00+00:00"}

    def tearDown(self):
        database.DB_PATH = self.old
        self.tmp.cleanup()

    def test_success_records_before_clearing_guard_and_no_repeat(self):
        events = []
        real_save = database.save_signal
        def save(*args, **kwargs):
            events.append("save")
            return real_save(*args, **kwargs)
        with patch.object(bot.executor, "place_real_trade", return_value=(True, 0.1)), \
             patch.object(bot.database, "save_signal", side_effect=save), \
             patch.object(bot.executor, "clear_order_block_after_record", side_effect=lambda: (events.append("clear") or True)):
            result = asyncio.run(bot._open_signal_if_clear("SOLUSDT", dict(self.signal)))
            self.assertTrue(result["success"])
            self.assertEqual(events, ["save", "clear"])
            self.assertEqual(len(database.get_open_signals()), 1)
            another = asyncio.run(bot._open_signal_if_clear("XRPUSDT", dict(self.signal)))
            self.assertFalse(another["success"])
            row = database.get_open_signals()[0]
            database.close_signal(row[0], "LOSS", -1)
            same_candle = asyncio.run(bot._open_signal_if_clear("SOLUSDT", dict(self.signal)))
            self.assertFalse(same_candle["success"])

    def test_db_failure_does_not_clear_guard(self):
        with patch.object(bot.executor, "place_real_trade", return_value=(True, 0.1)), \
             patch.object(bot.database, "save_signal", side_effect=OSError("disk failure")), \
             patch.object(bot.executor, "clear_order_block_after_record") as clear, \
             patch.object(bot.executor, "block_new_orders", return_value=True) as block:
            result = asyncio.run(bot._open_signal_if_clear("SOLUSDT", dict(self.signal)))
            self.assertTrue(result["success"])
            self.assertIn("ВНИМАНИЕ", result["reason"])
            clear.assert_not_called()
            block.assert_called_once()

    def test_clear_failure_keeps_manual_review(self):
        with patch.object(bot.executor, "place_real_trade", return_value=(True, 0.1)), \
             patch.object(bot.executor, "clear_order_block_after_record", return_value=False), \
             patch.object(bot.executor, "block_new_orders", return_value=True) as block:
            result = asyncio.run(bot._open_signal_if_clear("SOLUSDT", dict(self.signal)))
            self.assertTrue(result["success"])
            self.assertIn("заблокированы", result["reason"])
            block.assert_called_once()

    def test_unknown_order_result_is_not_reported_as_not_sent(self):
        with patch.object(bot.executor, "place_real_trade", return_value=(False, 0.0)), \
             patch.object(bot.executor, "get_order_block_status", return_value={"blocked": True}):
            result = asyncio.run(bot._open_signal_if_clear("SOLUSDT", dict(self.signal)))
            self.assertFalse(result["success"])
            self.assertIn("результат ордера может быть неизвестен", result["reason"])


if __name__ == "__main__":
    unittest.main()
