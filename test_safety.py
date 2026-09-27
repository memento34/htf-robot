import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.api import OkxClient
from bot.__main__ import Config, Runner, order_body
from bot.logic import data_agent, final_decision, make_signal, risk_agent
from bot.store import Store


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(db="unused")

    def test_demo_header_is_present_on_public_and_private_calls(self):
        seen = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"code":"0","data":[]}'
        def fake_open(req, timeout):
            seen.append((req.full_url, dict(req.header_items())))
            return Response()
        with patch("bot.api.urlopen", side_effect=fake_open):
            api = OkxClient("demo", "secret", "pass", min_gap=0)
            api.request("GET", "/api/v5/public/time")
            api.request("GET", "/api/v5/account/balance", private=True)
        self.assertEqual(len(seen), 2)
        for url, headers in seen:
            self.assertEqual(headers["X-simulated-trading"], "1")
            self.assertTrue(url.startswith("https://openapi.okx.com/api/v5/"))

    def test_stale_data_vetoes_entry(self):
        risk = risk_agent(10000, 10000, [], [], self.cfg, "STALE", 0)
        decision = final_decision([make_signal("trend_momentum", "long", 1)],
                                  {"trend_momentum": 1}, {"direction": "neutral", "confidence_0_1": 0,
                                  "severity_1_5": 1}, risk,
                                  {"multiplier": 1}, 100, 10000, self.cfg)
        self.assertEqual(decision["decision"], "hold")
        self.assertTrue(decision["veto_applied"])

    def test_daily_loss_and_exposure_veto(self):
        risk = risk_agent(9700, 10000, [], [], self.cfg, "OK", 0)
        self.assertTrue(risk["trading_halted"])
        self.assertIn("DAILY_LOSS", risk["veto_reason"])
        unknown = risk_agent(10000, 10000, [{"instId": "BTC-USDT-SWAP"}], [], self.cfg, "OK", 0)
        self.assertIn("EXPOSURE_UNKNOWN", unknown["veto_reason"])
        correlated = risk_agent(10000, 10000,
                                [{"instId":"ETH-USDT-SWAP", "posSide":"net", "pos":"2", "notionalUsd":"1000"}],
                                [], self.cfg, "OK", 0,
                                candidate_symbol="BTC-USDT-SWAP", candidate_direction="long")
        self.assertIn("BTC_ETH_CORRELATION", correlated["veto_reason"])

    def test_order_sizing_and_stop(self):
        inst = {"ctVal": "0.01", "ctValCcy": "BTC", "lotSz": "1", "minSz": "1", "tickSz": "0.1"}
        decision = {"reference_price": 100000, "size_usd": 3000, "decision": "long",
                    "stop_loss_price": 99500, "take_profit_price": 100750}
        key, body = order_body("BTC-USDT-SWAP", inst, decision, 123, self.cfg)
        self.assertEqual(body["sz"], "3")
        self.assertEqual(body["attachAlgoOrds"][0]["slOrdPx"], "-1")
        self.assertEqual(len(body["clOrdId"]), 31)
        self.assertEqual(key, "BTC-USDT-SWAP:123:long")

    def test_audit_chain_and_idempotent_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite3")
            store.audit("decision", {"decision": "hold"})
            store.audit("order_intent", {"symbol": "BTC-USDT-SWAP"})
            self.assertTrue(store.verify_audit())
            self.assertTrue(store.reserve("key", "BTC-USDT-SWAP", "id"))
            self.assertFalse(store.reserve("key", "BTC-USDT-SWAP", "id"))
            self.assertEqual(len(store.unresolved()), 1)
            with store.connect() as db:
                with self.assertRaises(Exception):
                    db.execute("DELETE FROM audit")

    def test_complete_cycle_holds_without_walk_forward_approval(self):
        import time
        now = int(time.time()*1000)
        base = now - 60*60000
        candles = [{"ts": base+i*60000, "o": 100+i*0.1, "h": 100.2+i*0.1,
                    "l": 99.8+i*0.1, "c": 100+i*0.1, "v": 1000, "confirm": "1"}
                   for i in range(60)]
        class FakeApi:
            def instruments(self): return {"BTC-USDT-SWAP": {"instId":"BTC-USDT-SWAP"}}
            def server_ms(self): return int(time.time()*1000)
            def positions_history(self, after=None): return []
            def balance(self): return {"totalEq": "10000"}
            def positions(self): return []
            def pending(self): return []
            def tickers(self): return {"BTC-USDT-SWAP": {"volCcy24h":"100000", "last":"100"}}
            def candles(self, symbol): return candles
            def book(self, symbol): return {"ts": str(int(time.time()*1000)),
                       "bids": [["106","10"]], "asks": [["106.1","1"]]}
            def place(self, body): raise AssertionError("Unexpected order")
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/"bot.sqlite3")
            runner = Runner(Config(db=str(Path(tmp)/"bot.sqlite3"), enabled=False), FakeApi(), store)
            runner.start_validation = lambda symbol: None
            runner.cycle()
            with store.connect() as db:
                decisions = db.execute("SELECT payload FROM audit WHERE kind='decision'").fetchall()
            self.assertEqual(len(decisions), 1)
            self.assertIn('"decision":"hold"', decisions[0][0])

    def test_fast_path_halts_near_liquidation(self):
        import time
        class FakeApi:
            def server_ms(self): return int(time.time()*1000)
            def balance(self): return {"totalEq": "10000"}
            def positions(self): return [{"instId":"BTC-USDT-SWAP", "pos":"1", "liqPx":"98"}]
            def ticker(self, symbol): return {"last":"100"}
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/"db.sqlite3")
            cfg = Config(db=str(Path(tmp)/"db.sqlite3"), enabled=True)
            runner = Runner(cfg, FakeApi(), store)
            runner.fast_check()
            self.assertEqual(runner.fast_halt, "LIQUIDATION_PROXIMITY")


if __name__ == "__main__":
    unittest.main()
