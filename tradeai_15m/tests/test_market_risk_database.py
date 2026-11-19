import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
import market
import bybit_guard
from risk import calculate_position_size


class MockResponse:
    def __init__(self, data):
        self.data = data
    def raise_for_status(self):
        pass
    def json(self):
        return self.data


def data(rows, now):
    return {"retCode": 0, "time": now, "result": {"list": rows}}


def candle(ms, p=10):
    return [str(ms), str(p), str(p+1), str(p-1), str(p+0.5), "5", "50"]


class MarketTests(unittest.TestCase):
    def setUp(self):
        bybit_guard._reset_for_tests()

    def tearDown(self):
        bybit_guard._reset_for_tests()

    @patch.dict(os.environ, {"BYBIT_TESTNET": "true"})
    @patch("market.time.time", return_value=1801)
    @patch("market.requests.get")
    def test_testnet_and_exclude_forming_candle(self, get, _clock):
        ms = 900000
        get.return_value = MockResponse(data([candle(2*ms), candle(ms), candle(0)], 2*ms + 1000))
        df = market.get_klines("SOLUSDT", "15", 3)
        self.assertEqual(len(df), 2)
        self.assertIn("api-testnet.bybit.com", get.call_args.args[0])
        self.assertEqual(df.iloc[-1]["close"], 10.5)

    @patch("market.time.time", return_value=2701)
    @patch("market.requests.get")
    def test_gap_is_rejected(self, get, _clock):
        ms = 900000
        get.return_value = MockResponse(data([candle(2*ms), candle(0)], 3*ms + 1000))
        self.assertIsNone(market.get_klines("SOLUSDT", "15", 3))

    @patch("market.time.time", return_value=3601)
    @patch("market.requests.get")
    def test_stale_is_rejected(self, get, _clock):
        ms = 900000
        get.return_value = MockResponse(data([candle(ms), candle(0)], 4*ms + 1000))
        self.assertIsNone(market.get_klines("SOLUSDT", "15", 3))

    @patch("market.time.time", return_value=1801)
    @patch("market.requests.get")
    def test_bad_ohlc_is_rejected(self, get, _clock):
        ms = 900000
        bad = candle(ms)
        bad[2] = "9"
        get.return_value = MockResponse(data([bad, candle(0)], 2*ms + 1000))
        self.assertIsNone(market.get_klines("SOLUSDT", "15", 3))

    @patch("market.requests.get")
    def test_public_10006_trips_shared_cooldown_without_retries(self, get):
        get.return_value = MockResponse({"retCode": 10006, "retMsg": "Too many visits"})
        self.assertIsNone(market.get_klines("SOLUSDT", "15", 3))
        get.assert_called_once()
        self.assertGreater(bybit_guard.cooldown_remaining(), 0)
        get.reset_mock()
        self.assertIsNone(market.get_klines("XRPUSDT", "15", 3))
        get.assert_not_called()


class RiskTests(unittest.TestCase):
    @patch.dict(os.environ, {"BYBIT_LEVERAGE": "3", "MAX_ENTRY_DEVIATION_PCT": "0.2"})
    def test_risk_and_margin_caps(self):
        sizing = calculate_position_size(100, 98, balance=1000, risk_percent=0.5)
        self.assertLessEqual(sizing["estimated_loss_usdt"], 5.000001)
        self.assertLessEqual(sizing["notional"], 2850)

    def test_invalid_inputs_rejected(self):
        for x in [float("nan"), float("inf"), -1, 0]:
            with self.assertRaises(ValueError):
                calculate_position_size(x, 10, balance=1000, risk_percent=0.5)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = Path(self.tmp.name) / "test.db"
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_candle_dedup_survives_close(self):
        candle_id = "2026-01-01T00:00:00+00:00"
        self.assertFalse(database.was_signal_candle_traded("SOLUSDT", candle_id))
        signal_id = database.save_signal("SOLUSDT", "LONG", 100, 98, 104, candle_id)
        database.close_signal(signal_id, "LOSS", -1)
        self.assertTrue(database.was_signal_candle_traded("SOLUSDT", candle_id))
        self.assertFalse(database.was_signal_candle_traded("XRPUSDT", candle_id))


if __name__ == "__main__":
    unittest.main()
