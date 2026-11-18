"""Mock-only safety tests; run: python -m unittest discover -s tests -v."""
import contextlib
import io
import os
import socket
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import executor


def ok(rows=None, **extra):
    return {"retCode": 0, "result": {"list": rows if rows is not None else [], **extra}}


def instrument():
    return {"symbol": "BTCUSDT", "status": "Trading", "contractType": "LinearPerpetual",
            "quoteCoin": "USDT", "settleCoin": "USDT",
            "lotSizeFilter": {"minOrderQty": "0.1", "qtyStep": "0.1", "maxMktOrderQty": "20", "minNotionalValue": "5"},
            "priceFilter": {"tickSize": "0.5", "minPrice": "0.5", "maxPrice": "100000"}}


def ticker():
    return {"symbol": "BTCUSDT", "lastPrice": "100", "markPrice": "100",
            "bid1Price": "99.99", "ask1Price": "100.01"}


class ExecutorSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(socket.socket, "connect", side_effect=AssertionError("NETWORK FORBIDDEN")))
        self.stack.enter_context(mock.patch.dict(os.environ, {"MAX_ENTRY_DEVIATION_PCT": "0.2"}))
        path = Path(self.tmp.name)
        for name, value in {
            "ORDER_BLOCK_FILE": path / "block.json", "_ORDER_LOCK_FILE": path / "lock",
            "_PROCESS_BLOCKED": False, "_CONFIRMED_RECORD_TOKEN": None,
            "TRADING_ENABLED": True, "BYBIT_TESTNET": True, "ALLOW_MAINNET_TRADING": False,
            "API_KEY": "mock-key", "API_SECRET": "mock-secret", "_CONFIG_ERRORS": [],
            "_CONFIRM_TIMEOUT_SECONDS": 0.01, "_CONFIRM_POLL_SECONDS": 0,
            "_CONFIRM_MAX_ATTEMPTS": 3,
        }.items():
            self.stack.enter_context(mock.patch.object(executor, name, value))
        self.client = mock.MagicMock()
        self.stack.enter_context(mock.patch.object(executor, "session", self.client))
        self.client.get_instruments_info.return_value = ok([instrument()])
        self.client.get_tickers.return_value = ok([ticker()])
        self.client.set_leverage.return_value = {"retCode": 0, "result": {}}
        self.client.place_order.side_effect = self.place
        self.client.get_open_orders.side_effect = self.orders
        self.client.get_order_history.return_value = ok([])
        self.client.get_positions.side_effect = self.positions
        self.sent = None

    def place(self, **kwargs):
        if kwargs.get("reduceOnly"):
            return {"retCode": 0, "result": {"orderId": "close"}}
        # Crash-safe marker must already exist at the moment of network dispatch.
        self.assertTrue(executor.ORDER_BLOCK_FILE.is_file())
        self.assertEqual(executor.get_order_block_status()["state"], "submission_uncertain")
        self.sent = kwargs
        return {"retCode": 0, "result": {"orderId": "order-1", "orderLinkId": kwargs["orderLinkId"]}}

    def order_row(self):
        return {"orderId": "order-1", "symbol": self.sent["symbol"], "side": self.sent["side"],
                "positionIdx": 0, "orderStatus": "Filled", "cumExecQty": self.sent["qty"],
                "orderLinkId": self.sent["orderLinkId"], "orderType": "Market", "reduceOnly": False, "qty": self.sent["qty"],
                "avgPrice": "100", "takeProfit": self.sent["takeProfit"], "stopLoss": self.sent["stopLoss"],
                "tpTriggerBy": "LastPrice", "slTriggerBy": "LastPrice"}

    def position_row(self):
        return {"symbol": self.sent["symbol"], "side": self.sent["side"], "positionIdx": 0,
                "size": self.sent["qty"], "avgPrice": "100",
                "takeProfit": self.sent["takeProfit"], "stopLoss": self.sent["stopLoss"]}

    def orders(self, **kwargs):
        if kwargs.get("orderId"):
            return ok([self.order_row()] if self.sent else [])
        return ok([])

    def positions(self, **kwargs):
        return ok([self.position_row()] if self.sent else [])

    def trade(self, **changes):
        args = {"symbol": "BTCUSDT", "direction": "LONG", "qty": "2.99",
                "tp": "102.34", "sl": "98.66", "entry_price": "100"}
        args.update(changes)
        return executor.place_real_trade(**args)

    def test_disabled_never_touches_api(self):
        executor.TRADING_ENABLED = False
        self.assertEqual(self.trade(), (False, 0.0))
        self.client.assert_not_called()
        self.client.get_instruments_info.assert_not_called()
        self.client.place_order.assert_not_called()

    def test_mainnet_needs_separate_explicit_authorization(self):
        executor.BYBIT_TESTNET = False
        self.assertEqual(self.trade(), (False, 0.0))
        self.client.place_order.assert_not_called()
        executor.ALLOW_MAINNET_TRADING = True
        self.assertTrue(self.trade()[0])

    def test_invalid_booleans_fail_closed_and_testnet_defaults_safe(self):
        for name, value, safe in (("BYBIT_TESTNET", "flase", True),
                                   ("TRADING_ENABLED", "1", False),
                                   ("ALLOW_MAINNET_TRADING", "yes", False)):
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: value}):
                executor._CONFIG_ERRORS.clear()
                self.assertEqual(executor._env_bool(name, safe), safe)
                self.assertEqual(len(executor._CONFIG_ERRORS), 1)
                self.assertEqual(self.trade(), (False, 0.0))
        self.client.place_order.assert_not_called()

    def test_existing_position_blocks(self):
        self.client.get_positions.return_value = ok([{ "symbol": "BTCUSDT", "positionIdx": 0, "size": "1", "side": "Buy"}])
        self.client.get_positions.side_effect = None
        self.assertEqual(self.trade(), (False, 0.0))
        self.client.place_order.assert_not_called()
        self.assertFalse(executor.get_order_block_status()["blocked"])

    def test_hedge_mode_even_empty_position_blocks(self):
        self.client.get_positions.side_effect = None
        self.client.get_positions.return_value = ok([{"symbol": "BTCUSDT", "positionIdx": 1, "size": "0"}])
        self.assertFalse(self.trade()[0])
        self.client.place_order.assert_not_called()

    def test_existing_unfinished_order_blocks(self):
        self.client.get_open_orders.side_effect = None
        self.client.get_open_orders.return_value = ok([{"symbol": "BTCUSDT", "orderStatus": "New"}])
        self.assertFalse(self.trade()[0])
        self.client.place_order.assert_not_called()

    def test_pre_send_errors_fail_closed(self):
        for source, response in (("get_instruments_info", {"retCode": 1, "result": {}}),
                                 ("get_positions", {"result": {"list": []}}),
                                 ("get_open_orders", {"retCode": 0, "result": {}}),
                                 ("get_tickers", {"retCode": 0, "result": {"list": []}})):
            with self.subTest(source=source):
                method = getattr(self.client, source)
                old_side, old_value = method.side_effect, method.return_value
                method.side_effect = None
                method.return_value = response
                self.assertFalse(self.trade()[0])
                self.client.place_order.assert_not_called()
                method.side_effect, method.return_value = old_side, old_value

    def test_missing_or_inadequate_reference_rejected(self):
        self.assertFalse(self.trade(entry_price=None)[0])
        self.assertFalse(self.trade(entry_price="99")[0])
        self.client.place_order.assert_not_called()

    def test_quote_must_be_between_protection_and_close_to_reference(self):
        for field, value in (("lastPrice", "101"), ("markPrice", "100.21"), ("ask1Price", "102.5"), ("bid1Price", "0")):
            with self.subTest(field=field):
                quote = ticker()
                quote[field] = value
                self.client.get_tickers.return_value = ok([quote])
                self.assertFalse(self.trade()[0])
                self.client.place_order.assert_not_called()

    def test_long_rounding_attached_tpsl_and_sqlite_handshake(self):
        success, qty = self.trade()
        self.assertTrue(success)
        self.assertEqual(qty, 2.9)
        args = self.sent
        self.assertEqual(args["qty"], "2.9")
        self.assertEqual(args["stopLoss"], "99.0")  # rounded toward entry
        self.assertEqual(args["takeProfit"], "102.0")
        self.assertEqual(args["positionIdx"], 0)
        self.assertEqual((args["tpTriggerBy"], args["slTriggerBy"], args["tpslMode"]), ("LastPrice", "LastPrice", "Full"))
        self.assertEqual(args["slippageToleranceType"], "Percent")
        self.assertLessEqual(Decimal(args["qty"]), Decimal("2.99"))
        self.assertEqual(executor.get_order_block_status()["state"], "confirmed_awaiting_record")
        self.assertEqual(self.trade(symbol="ETHUSDT"), (False, 0.0))
        self.assertEqual(self.client.place_order.call_count, 1)
        self.assertTrue(executor.clear_order_block_after_record())
        self.assertFalse(executor.get_order_block_status()["blocked"])

    def test_short_rounding_never_widens_stop(self):
        success, qty = self.trade(direction="SHORT", tp="97.66", sl="101.34")
        self.assertTrue(success)
        self.assertLessEqual(qty, 2.99)
        self.assertEqual(self.sent["stopLoss"], "101.0")
        self.assertEqual(self.sent["takeProfit"], "98.0")

    def test_stop_rounding_invalidates_bracket(self):
        self.assertFalse(self.trade(sl="99.99")[0])
        self.client.place_order.assert_not_called()

    def test_exchange_limits_never_increase_size(self):
        info = instrument()
        info["lotSizeFilter"]["maxMktOrderQty"] = "0.6"
        self.client.get_instruments_info.return_value = ok([info])
        success, qty = self.trade()
        self.assertTrue(success)
        self.assertEqual(qty, 0.6)
        self.assertLessEqual(qty, 2.99)

    def test_quantity_and_notional_minimum_are_not_raised(self):
        self.assertFalse(self.trade(qty="0.09")[0])
        info = instrument()
        info["lotSizeFilter"]["minNotionalValue"] = "1000"
        self.client.get_instruments_info.return_value = ok([info])
        self.assertFalse(self.trade()[0])
        self.client.place_order.assert_not_called()

    def test_worse_market_price_reduces_qty_within_original_risk(self):
        # Round-away of original stop cannot enlarge the loss; even an allowed
        # adverse quote/slippage is budgeted against the requested original risk.
        success, qty = self.trade(qty="3", sl="99.0")
        self.assertTrue(success)
        self.assertLess(qty, 3)
        self.assertLessEqual(Decimal(str(qty)) * (Decimal("100.20") - Decimal(self.sent["stopLoss"])), Decimal("3"))

    def test_timeout_after_send_is_durable_and_blocks_all_symbols(self):
        self.client.place_order.side_effect = TimeoutError("unknown delivery")
        self.assertEqual(self.trade(), (False, 0.0))
        self.assertEqual(self.client.place_order.call_count, 1)
        self.assertTrue(executor.get_order_block_status()["blocked"])
        self.assertFalse(self.trade(symbol="ETHUSDT")[0])
        self.assertFalse(executor.clear_order_block_after_record())
        self.assertEqual(self.client.place_order.call_count, 1)
        # Simulate a restart by resetting only process memory.
        executor._CONFIRMED_RECORD_TOKEN = None
        executor._PROCESS_BLOCKED = False
        self.assertTrue(executor.get_order_block_status()["blocked"])
        self.assertFalse(self.trade()[0])
        # Removal is manual, not performed by the executor for uncertainty.
        executor.ORDER_BLOCK_FILE.unlink()
        self.assertFalse(executor.get_order_block_status()["blocked"])

    def test_crash_during_send_leaves_intent(self):
        self.client.place_order.side_effect = SystemExit("simulated crash")
        with self.assertRaises(SystemExit):
            self.trade()
        self.assertTrue(executor.get_order_block_status()["blocked"])
        self.assertFalse(executor.clear_order_block_after_record())

    def test_parallel_entry_requests_never_bypass_inflight_marker(self):
        entered = threading.Event()
        proceed = threading.Event()
        results = []
        def delayed_place(**kwargs):
            entered.set()
            self.assertTrue(proceed.wait(2))
            return self.place(**kwargs)
        self.client.place_order.side_effect = delayed_place
        first = threading.Thread(target=lambda: results.append(self.trade()))
        first.start()
        self.assertTrue(entered.wait(2))
        second = threading.Thread(target=lambda: results.append(self.trade()))
        second.start()
        proceed.set()
        first.join(2)
        second.join(2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(sorted(result[0] for result in results), [False, True])
        self.assertEqual(self.client.place_order.call_count, 1)

    def test_accepted_not_filled_never_succeeds(self):
        self.client.get_open_orders.side_effect = lambda **k: ok([] if not k.get("orderId") else [{"symbol": "BTCUSDT", "orderId": "order-1", "orderStatus": "New"}])
        self.client.get_positions.side_effect = lambda **k: ok([])
        self.assertEqual(self.trade(), (False, 0.0))
        self.assertTrue(executor.get_order_block_status()["blocked"])
        self.assertEqual(self.client.place_order.call_count, 1)

    def test_filled_order_can_be_confirmed_from_history(self):
        self.client.get_open_orders.side_effect = lambda **kwargs: ok([])
        self.client.get_order_history.side_effect = lambda **kwargs: ok([self.order_row()])
        self.assertTrue(self.trade()[0])
        self.assertTrue(self.client.get_order_history.called)

    def test_mismatched_order_identity_fails_closed(self):
        def wrong_link(**kwargs):
            if not kwargs.get("orderId"):
                return ok([])
            row = self.order_row()
            row["orderLinkId"] = "different-order"
            return ok([row])
        self.client.get_open_orders.side_effect = wrong_link
        self.assertFalse(self.trade()[0])
        self.assertTrue(executor.get_order_block_status()["blocked"])

    def test_mismatched_stop_trigger_cannot_be_accepted(self):
        def wrong_trigger(**kwargs):
            if not kwargs.get("orderId"):
                return ok([])
            row = self.order_row()
            row["slTriggerBy"] = "MarkPrice"
            return ok([row])
        self.client.get_open_orders.side_effect = wrong_trigger
        self.assertFalse(self.trade()[0])
        self.assertTrue(executor.get_order_block_status()["blocked"])
        self.assertTrue(self.client.place_order.call_args_list[-1].kwargs["reduceOnly"])

    def test_partial_fill_and_confirmation_api_error_stay_blocked(self):
        self.client.get_open_orders.side_effect = lambda **k: ok([] if not k.get("orderId") else [{"symbol": "BTCUSDT", "orderId": "order-1", "orderStatus": "PartiallyFilled"}])
        self.assertFalse(self.trade()[0])
        self.assertTrue(executor.get_order_block_status()["blocked"])
        executor.ORDER_BLOCK_FILE.unlink()  # manual test reset
        self.sent = None
        self.client.get_open_orders.side_effect = lambda **k: ok([]) if not k.get("orderId") else {"retCode": 10001, "result": {}}
        self.assertFalse(self.trade()[0])
        self.assertTrue(executor.get_order_block_status()["blocked"])

    def test_missing_position_protection_attempts_only_reduce_only_close(self):
        original = self.positions
        def no_stop(**kwargs):
            result = original(**kwargs)
            if self.sent:
                result["result"]["list"][0]["stopLoss"] = "0"
            return result
        self.client.get_positions.side_effect = no_stop
        self.assertFalse(self.trade()[0])
        self.assertTrue(executor.get_order_block_status()["blocked"])
        self.assertEqual(self.client.place_order.call_count, 2)
        close_args = self.client.place_order.call_args_list[1].kwargs
        self.assertTrue(close_args["reduceOnly"])
        self.assertEqual(close_args["positionIdx"], 0)
        self.assertEqual(close_args["side"], "Sell")

    def test_post_execution_outside_risk_attempts_reduce_only_close(self):
        old_order = self.orders
        old_positions = self.positions
        def adverse_order(**kwargs):
            result = old_order(**kwargs)
            if kwargs.get("orderId") and self.sent:
                result["result"]["list"][0]["avgPrice"] = "100.5"
            return result
        def adverse_position(**kwargs):
            result = old_positions(**kwargs)
            if self.sent:
                result["result"]["list"][0]["avgPrice"] = "100.5"
            return result
        self.client.get_open_orders.side_effect = adverse_order
        self.client.get_positions.side_effect = adverse_position
        self.assertFalse(self.trade()[0])
        self.assertTrue(executor.get_order_block_status()["blocked"])
        self.assertTrue(self.client.place_order.call_args_list[-1].kwargs["reduceOnly"])

    def test_manual_block_after_db_failure_cannot_be_auto_cleared(self):
        self.assertTrue(self.trade()[0])
        self.assertTrue(executor.block_new_orders("SQLite failure"))
        status = executor.get_order_block_status()
        self.assertEqual(status["state"], "manual_review_required")
        self.assertIn("SQLite failure", status["reason"])
        self.assertFalse(executor.clear_order_block_after_record())
        self.assertFalse(self.trade()[0])

    def test_confirmed_marker_survives_restart_before_sqlite(self):
        self.assertTrue(self.trade()[0])
        executor._CONFIRMED_RECORD_TOKEN = None  # restart
        self.assertTrue(executor.get_order_block_status()["blocked"])
        self.assertFalse(executor.clear_order_block_after_record())
        self.assertFalse(self.trade()[0])

    def test_marker_io_failure_prevents_entry(self):
        with mock.patch.object(executor, "_create_marker", side_effect=OSError("disk full")):
            self.assertFalse(self.trade()[0])
        self.client.place_order.assert_not_called()
        with mock.patch.object(executor, "_create_marker", side_effect=OSError("disk full")):
            self.assertFalse(executor.block_new_orders("db failure"))
        self.assertTrue(executor.get_order_block_status()["blocked"])

    def test_monitoring_paginates_independent_of_safety_flag(self):
        self.assertTrue(executor.block_new_orders("manual review"))
        self.client.get_positions.side_effect = [ok([{"symbol": "BTCUSDT"}], nextPageCursor="next"), ok([{"symbol": "ETHUSDT"}], nextPageCursor="")]
        self.assertEqual(len(executor.get_positions_snapshot()), 2)
        self.assertEqual(self.client.get_positions.call_args.kwargs["cursor"], "next")
        self.client.get_closed_pnl.side_effect = [ok([{"id": "a"}], nextPageCursor="b"), ok([{"id": "b"}])]
        self.assertEqual(len(executor.get_closed_pnl_snapshot()), 2)
        self.assertEqual(self.client.get_closed_pnl.call_args.kwargs["cursor"], "b")

    def test_monitoring_fails_instead_of_returning_partial_or_empty(self):
        self.client.get_positions.side_effect = [ok([{"symbol": "BTCUSDT"}], nextPageCursor="next"), {"retCode": 1, "result": {}}]
        with self.assertRaises(RuntimeError):
            executor.get_positions_snapshot()
        self.client.get_closed_pnl.side_effect = [ok([], nextPageCursor="loop"), ok([], nextPageCursor="loop")]
        with self.assertRaises(RuntimeError):
            executor.get_closed_pnl_snapshot()

    def test_response_must_explicitly_have_success_code_and_list(self):
        for response in ({}, {"retCode": None, "result": {"list": []}}, {"retCode": False, "result": {"list": []}}, {"retCode": 0, "result": {}}):
            self.client.get_positions.side_effect = None
            self.client.get_positions.return_value = response
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                executor.get_positions_snapshot()

    def test_pagination_on_preflight_is_not_treated_as_clear(self):
        self.client.get_open_orders.side_effect = None
        self.client.get_open_orders.return_value = ok([], nextPageCursor="more")
        self.assertFalse(self.trade()[0])
        self.client.place_order.assert_not_called()

    def test_unknown_emergency_side_never_places_an_order(self):
        self.client.get_positions.side_effect = None
        self.client.get_positions.return_value = ok([{"symbol": "BTCUSDT", "positionIdx": 0, "size": "1", "side": "UNKNOWN"}])
        self.assertFalse(executor.close_position("BTCUSDT"))
        self.client.place_order.assert_not_called()


class ClientConfigurationTests(unittest.TestCase):
    def test_real_pybit_configuration_has_no_retry_and_matches_environment(self):
        # Pure introspection; no key or request is needed.
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("NETWORK FORBIDDEN")):
            self.assertEqual(executor.session.testnet, executor.BYBIT_TESTNET)
            self.assertEqual(executor.session.max_retries, 1)
            self.assertFalse(executor.session.force_retry)
            self.assertEqual(executor.session.retry_codes, set())
            self.assertGreater(executor.session.timeout, 0)


if __name__ == "__main__":
    unittest.main()
