import unittest
from unittest import mock

import bybit_guard


class LimitError(Exception):
    status_code = 10006


class Http429Error(Exception):
    status_code = 429


class BybitGuardTests(unittest.TestCase):
    def setUp(self):
        bybit_guard._reset_for_tests()
        self.spacing = mock.patch.object(bybit_guard, "_MIN_REQUEST_INTERVAL", 0.0)
        self.spacing.start()
        self.addCleanup(self.spacing.stop)
        self.addCleanup(bybit_guard._reset_for_tests)

    def test_api_error_10006_is_not_retried_and_starts_cooldown(self):
        request = mock.Mock(side_effect=LimitError("Too many visits"))
        with self.assertRaises(bybit_guard.RateLimitCooldown) as caught:
            bybit_guard.call_bybit(request, action="set_leverage")
        self.assertIn("no retry", str(caught.exception))
        request.assert_called_once()
        self.assertGreaterEqual(bybit_guard.cooldown_remaining(), 59)

    def test_json_10006_response_is_not_retried_and_blocks_following_calls(self):
        request = mock.Mock(return_value={"retCode": 10006, "retMsg": "Too many visits"})
        with self.assertRaises(bybit_guard.RateLimitCooldown):
            bybit_guard.call_bybit(request, action="get_positions")
        request.assert_called_once()

        another = mock.Mock(return_value={"retCode": 0})
        with self.assertRaises(bybit_guard.RateLimitCooldown):
            bybit_guard.call_bybit(another, action="place_order")
        another.assert_not_called()

    def test_http_429_response_starts_cooldown(self):
        response = mock.Mock(status_code=429)
        with self.assertRaises(bybit_guard.RateLimitCooldown):
            bybit_guard.call_bybit(lambda: response, action="get_klines")
        self.assertGreater(bybit_guard.cooldown_remaining(), 0)

    def test_sdk_http_429_exception_starts_cooldown(self):
        def fail_request():
            raise Http429Error("Too many requests")

        with self.assertRaises(bybit_guard.RateLimitCooldown):
            bybit_guard.call_bybit(fail_request, action="get_positions")
        self.assertGreater(bybit_guard.cooldown_remaining(), 0)

    def test_success_calls_once(self):
        request = mock.Mock(return_value={"retCode": 0, "result": {}})
        result = bybit_guard.call_bybit(request, action="get_positions")
        self.assertEqual(result["retCode"], 0)
        request.assert_called_once()
        self.assertEqual(bybit_guard.cooldown_remaining(), 0)

    def test_repeated_limit_extends_backoff(self):
        with self.assertRaises(bybit_guard.RateLimitCooldown):
            bybit_guard.call_bybit(lambda: {"retCode": 10006}, action="first")
        first = bybit_guard.cooldown_remaining()
        bybit_guard._COOLDOWN_UNTIL = 0.0  # simulate cooldown elapsed without waiting
        with self.assertRaises(bybit_guard.RateLimitCooldown):
            bybit_guard.call_bybit(lambda: {"retCode": 10006}, action="second")
        second = bybit_guard.cooldown_remaining()
        self.assertGreater(second, first)


if __name__ == "__main__":
    unittest.main()
