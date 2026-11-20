"""test_binance.py — 币安合约模块单测（标准库 unittest，不用 pip）。

跑法（在平台目录里）：
  python3 test_binance.py              # 全部单测（不含实网）
  python3 test_binance.py --live-price # 再加一节实网：用 data-api.binance.vision
                                       # 的真实币安现货价跑一遍 dry-run 数学
                                       # （fapi.binance.com 在这台 VM 上 451，
                                       #  拿不到期货价；Mac 上用 binance_link.py
                                       #  --test 验 fapi）
覆盖：下单数学、风控上限、没凭证的行为、限流不重试、签名、dry-run 日志。
不下任何真单，不读真实 .env（没凭证的用例都用临时目录）。
"""
import hashlib
import hmac
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from quant_platform import binance_client as bc


def make_cfg(**over):
    cfg = {"live_trading": False, "capital": 200.0, "margin_per_trade": 10.0,
           "leverage": 10, "stop_pct": 0.05, "take_pct": 0.60,
           "long_only": True, "daily_loss_cap": 50.0, "max_total_loss": 500.0,
           "fee_rate": 0.0005, "symbols": ["BTCUSDT"]}
    cfg.update(over)
    return cfg


class TestComputeOrder(unittest.TestCase):
    def test_btc_default_params(self):
        # BTC 61710.99：10U 保证金 × 10x = 100U 名义，步长 0.001 → 只能买 0.001
        calc = bc.compute_order(61710.99, 10, 10, 0.05, 0.60, 0.0005,
                                dict(bc.DEFAULT_FILTERS))
        self.assertEqual(calc["qty"], 0.001)
        self.assertAlmostEqual(calc["notional"], 61.711, places=2)
        self.assertEqual(calc["stop_price"], 58625.4)
        self.assertEqual(calc["take_price"], 98737.5)
        # 止损时最大亏损 ≈ 3.09 + 0.06 手续费，远小于 10U 保证金
        self.assertLess(calc["max_loss_at_stop"], 10.0)
        self.assertGreater(calc["max_loss_at_stop"], 0)

    def test_eth_fills_notional_closer(self):
        calc = bc.compute_order(3000.0, 10, 10, 0.05, 0.60, 0.0005,
                                dict(bc.DEFAULT_FILTERS))
        self.assertEqual(calc["qty"], 0.033)
        self.assertAlmostEqual(calc["notional"], 99.0, places=2)

    def test_qty_floors_down_not_up(self):
        # 100/61710.99 = 0.00162…，向下取整到 0.001，绝不能进位到 0.002
        calc = bc.compute_order(61710.99, 10, 10, 0.05, 0.60, 0.0005,
                                dict(bc.DEFAULT_FILTERS))
        self.assertLessEqual(calc["qty"] * 61710.99, 100.0)

    def test_below_min_notional_refused(self):
        # 名义 3U（保证金 0.3U × 10x）小于最小名义 5U
        with self.assertRaises(bc.RiskRefusedError):
            bc.compute_order(61710.99, 0.3, 10, 0.05, 0.60, 0.0005,
                             dict(bc.DEFAULT_FILTERS))

    def test_bad_price_refused(self):
        with self.assertRaises(bc.RiskRefusedError):
            bc.compute_order(0, 10, 10, 0.05, 0.60, 0.0005,
                             dict(bc.DEFAULT_FILTERS))


class TestRisk(unittest.TestCase):
    def test_buy_ok(self):
        ok, _ = bc.check_risk(make_cfg(), "BUY", 10, 10, 100, False, 0.0)
        self.assertTrue(ok)

    def test_margin_over_cap_refused(self):
        ok, reason = bc.check_risk(make_cfg(), "BUY", 11, 10, 110, False, 0.0)
        self.assertFalse(ok)
        self.assertIn("保证金", reason)

    def test_leverage_over_cap_refused(self):
        ok, reason = bc.check_risk(make_cfg(), "BUY", 10, 11, 110, False, 0.0)
        self.assertFalse(ok)
        self.assertIn("杠杆", reason)

    def test_notional_over_cap_refused(self):
        ok, _ = bc.check_risk(make_cfg(), "BUY", 10, 10, 100.01, False, 0.0)
        self.assertFalse(ok)

    def test_sell_open_refused_long_only(self):
        ok, reason = bc.check_risk(make_cfg(), "SELL", 10, 10, 100, False, 0.0)
        self.assertFalse(ok)
        self.assertIn("只做多", reason)

    def test_sell_close_allowed(self):
        ok, _ = bc.check_risk(make_cfg(), "SELL", 10, 10, 100, True, 0.0)
        self.assertTrue(ok)

    def test_daily_loss_cap_refused(self):
        ok, _ = bc.check_risk(make_cfg(), "BUY", 10, 10, 100, False, 50.0)
        self.assertFalse(ok)

    def test_bad_side_refused(self):
        ok, _ = bc.check_risk(make_cfg(), "HOLD", 10, 10, 100, False, 0.0)
        self.assertFalse(ok)


class TestSigning(unittest.TestCase):
    def test_hmac_signature_matches_independent_calc(self):
        secret = "testsecret123"
        params = {"symbol": "BTCUSDT", "timestamp": 1700000000000,
                  "recvWindow": 5000}
        query, sig = bc._sign_params(secret, params)
        expect = hmac.new(secret.encode(), query.encode(),
                          hashlib.sha256).hexdigest()
        self.assertEqual(sig, expect)
        self.assertIn("symbol=BTCUSDT", query)
        self.assertIn("timestamp=1700000000000", query)


class TestNoRetryOnBan(unittest.TestCase):
    """限流/封 IP 时只发一次请求，绝不重试。"""

    def test_429_raises_apibanned_after_one_call(self):
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b'{"code":-1003}'))

        orig = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            with self.assertRaises(bc.ApiBanned):
                bc._http_json("GET", "https://fapi.binance.com/fapi/v1/ping")
        finally:
            urllib.request.urlopen = orig
        self.assertEqual(calls["n"], 1, "429 时必须只请求一次，不重试")

    def test_451_raises_geoblocked(self):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 451, "Unavailable", {},
                io.BytesIO(b'{"code":0,"msg":"Service unavailable from a restricted location"}'))

        orig = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            with self.assertRaises(bc.GeoBlocked):
                bc._http_json("GET", "https://fapi.binance.com/fapi/v1/ping")
        finally:
            urllib.request.urlopen = orig


class TestNoKeyBehavior(unittest.TestCase):
    """没凭证：报未连接，不崩；dry-run 不用凭证也能算（只取公开价）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.link = bc.BinanceLink(root=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_link_status_no_env(self):
        st = self.link.link_status()
        self.assertFalse(st["linked"])
        self.assertIn("未连接", st["error"])
        self.assertIsNone(st["balance_usdt"])

    def test_link_status_empty_env(self):
        with open(os.path.join(self.tmp.name, ".env"), "w") as f:
            f.write("# empty\n")
        st = bc.BinanceLink(root=self.tmp.name).link_status()
        self.assertFalse(st["linked"])
        self.assertIn("凭证缺失", st["error"])

    def test_dry_run_without_key_uses_public_price(self):
        # 模拟公开价 + 精度，不碰网络、不碰凭证
        orig_price = bc.public_price
        orig_filters = bc.public_filters
        bc.public_price = lambda symbol, base=None, path=None: 61710.99
        bc.public_filters = lambda symbol, base=None, path=None: dict(bc.DEFAULT_FILTERS)
        try:
            res = self.link.place_order("BTCUSDT", "BUY", dry_run=True)
        finally:
            bc.public_price = orig_price
            bc.public_filters = orig_filters
        self.assertTrue(res["dry_run"])
        self.assertTrue(res["would_send"])
        self.assertEqual(res["qty"], 0.001)
        # 日志里有这笔模拟单
        with open(os.path.join(self.tmp.name, "binance_orders.log")) as f:
            rec = json.loads(f.readlines()[-1])
        self.assertEqual(rec["kind"], "DRY_RUN")
        self.assertEqual(rec["outcome"], "would_send")

    def test_dry_run_refuses_over_cap_before_any_network(self):
        # 保证金超上限：风控先拦，根本不该发网络请求
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            raise AssertionError("风控拒绝后不该发请求")

        orig = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            with self.assertRaises(bc.RiskRefusedError):
                self.link.place_order("BTCUSDT", "BUY", margin=11, dry_run=True)
        finally:
            urllib.request.urlopen = orig
        self.assertEqual(calls["n"], 0)

    def test_live_refused_when_switch_off(self):
        orig_price = bc.public_price
        orig_filters = bc.public_filters
        bc.public_price = lambda symbol, base=None, path=None: 61710.99
        bc.public_filters = lambda symbol, base=None, path=None: dict(bc.DEFAULT_FILTERS)
        orig_cfg = bc.CONFIG
        bc.CONFIG = {"binance": make_cfg(live_trading=False)}
        try:
            with self.assertRaises(bc.LiveTradingDisabledError):
                self.link.place_order("BTCUSDT", "BUY", dry_run=False)
        finally:
            bc.public_price = orig_price
            bc.public_filters = orig_filters
            bc.CONFIG = orig_cfg

    def test_secrets_never_in_logs(self):
        # .env 里放个假 Secret，报错信息里不能出现它
        with open(os.path.join(self.tmp.name, ".env"), "w") as f:
            f.write("BINANCE_API_KEY=FAKEKEY12345678\n")
            f.write("BINANCE_API_SECRET=FAKESECRET87654321\n")
        link = bc.BinanceLink(root=self.tmp.name)
        st = link.link_status()
        blob = json.dumps(st, ensure_ascii=False)
        self.assertNotIn("FAKESECRET87654321", blob)
        self.assertNotIn("FAKEKEY12345678", blob)


class TestExchangeInfoParsing(unittest.TestCase):
    def test_parse_filters_from_canned_response(self):
        canned = {"symbols": [{"symbol": "BTCUSDT", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]}
        orig = urllib.request.urlopen

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(canned).encode()

        urllib.request.urlopen = lambda req, timeout=None: Resp()
        try:
            bc._exchange_info_cache.pop("BTCUSDT", None)
            out = bc.public_filters("BTCUSDT")
        finally:
            urllib.request.urlopen = orig
        self.assertEqual(out["step_size"], 0.001)
        self.assertEqual(out["tick_size"], 0.1)
        self.assertEqual(out["min_qty"], 0.001)
        self.assertEqual(out["min_notional"], 5.0)
        self.assertFalse(out["filters_from_defaults"])


class TestLivePriceDryRun(unittest.TestCase):
    """实网：用 data-api.binance.vision 的真实币安现货价跑一遍 dry-run 数学。
    只在 --live-price 时跑。注意这是现货价，只验证管线 + 数学，不代表期货价；
    期货价要在 Mac 上用 binance_link.py --test 验 fapi。"""

    def test_real_price_dry_run(self):
        px = bc.public_price("BTCUSDT", base="https://data-api.binance.vision",
                             path="/api/v3/ticker/price")
        self.assertGreater(px, 1000, "BTC 现货价应大于 1000U")
        calc = bc.compute_order(px, 10, 10, 0.05, 0.60, 0.0005,
                                dict(bc.DEFAULT_FILTERS))
        self.assertGreater(calc["qty"], 0)
        self.assertLess(calc["max_loss_at_stop"], 10.0)
        print(f"\n[实网] BTC 现货价 {px}，dry-run 数量 {calc['qty']}，"
              f"名义 {calc['notional']}U，止损 {calc['stop_price']}，"
              f"止盈 {calc['take_price']}，止损最大亏 {calc['max_loss_at_stop']}U")


if __name__ == "__main__":
    live = "--live-price" in sys.argv
    if live:
        sys.argv.remove("--live-price")
    else:
        # 默认不跑实网那节
        TestLivePriceDryRun.test_real_price_dry_run = unittest.skip(
            "默认不跑实网；加 --live-price 才跑")(TestLivePriceDryRun.test_real_price_dry_run)
    unittest.main(verbosity=2)
