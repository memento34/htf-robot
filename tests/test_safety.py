import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.api import OkxClient
from bot.__main__ import Config, Runner
from bot.logic import final_decision, make_signal, risk_agent
from bot.paper import PaperBroker
from bot.store import Store


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(db="unused")

    def test_public_api_cannot_call_private_or_post_and_has_no_demo_header(self):
        seen = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"code":"0","data":[]}'
        def fake_open(req, timeout):
            seen.append((req.full_url, dict(req.header_items())))
            return Response()
        with patch("bot.api.urlopen", side_effect=fake_open):
            api = OkxClient(min_gap=0)
            api.request("GET", "/api/v5/public/time")
            with self.assertRaises(ValueError): api.request("GET", "/api/v5/account/balance")
            with self.assertRaises(ValueError): api.request("POST", "/api/v5/trade/order")
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0][0].startswith("https://openapi.okx.com/api/v5/public/"))
        self.assertFalse(any("simulated" in k.lower() or "access" in k.lower() for k in seen[0][1]))

    def test_stale_data_and_daily_loss_veto_entries(self):
        risk = risk_agent(10000, 10000, [], [], self.cfg, "STALE", 0)
        decision = final_decision([make_signal("trend_momentum", "long", 1)],
                                  {"trend_momentum": 1}, {"direction": "neutral", "confidence_0_1": 0,
                                  "severity_1_5": 1}, risk, {"multiplier": 1}, 100, 10000, self.cfg)
        self.assertEqual(decision["decision"], "hold")
        self.assertTrue(decision["veto_applied"])
        self.assertIn("DAILY_LOSS", risk_agent(9700, 10000, [], [], self.cfg, "OK", 0)["veto_reason"])

    def test_paper_open_close_fee_pnl_idempotency_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "paper.sqlite3")
            broker = PaperBroker(store, 10000, fee_bps=5, slippage_bps=0)
            symbol = "BTC-USDT-SWAP"
            inst = {"ctVal": "0.01", "ctValCcy": "BTC", "lotSz": "1", "minSz": "1"}
            decision = {"decision": "long", "reference_price": 100, "size_usd": 100,
                        "stop_loss_price": 99, "take_profit_price": 102}
            def book(bid, ask):
                return {"ts": str(int(time.time()*1000)), "bids": [[str(bid), "10"]], "asks": [[str(ask), "10"]]}
            opened = broker.open(symbol, inst, decision, 123, book(99.99, 100), self.cfg)
            self.assertEqual(opened["contracts"], "100")
            self.assertEqual(len(broker.positions()), 1)
            self.assertIsNone(broker.open(symbol, inst, decision, 123, book(99.99, 100), self.cfg))
            self.assertAlmostEqual(broker.account()["cash_usdt"], 9999.95)
            _, closed = broker.mark_and_close(symbol, book(102, 102.01))
            self.assertEqual(closed["reason"], "take")
            self.assertAlmostEqual(closed["net_pnl_usdt"], 1.899)
            self.assertAlmostEqual(broker.account()["cash_usdt"], 10001.899)
            again = PaperBroker(store, 99999)
            self.assertEqual(again.account()["initial_usdt"], 10000)
            self.assertEqual(again.positions(), [])
            self.assertTrue(store.verify_audit())
            snapshot = store.dashboard_snapshot()
            self.assertAlmostEqual(snapshot["paper_cash_usdt"], 10001.899)
            self.assertEqual(snapshot["closed_positions"], 1)

    def test_complete_cycle_holds_without_walk_forward_approval(self):
        now = int(time.time()*1000)
        base = now - 300*60000
        candles = [{"ts": base+i*60000, "o": 100+i*0.1, "h": 100.2+i*0.1,
                    "l": 99.8+i*0.1, "c": 100+i*0.1, "v": 1000, "confirm": "1"}
                   for i in range(300)]
        class FakeApi:
            def instruments(self): return {"BTC-USDT-SWAP": {"instId": "BTC-USDT-SWAP"}}
            def server_ms(self): return int(time.time()*1000)
            def tickers(self): return {"BTC-USDT-SWAP": {"volCcy24h": "100000", "last": "100"}}
            def candles(self, symbol, limit=300): return candles
            def book(self, symbol): return {"ts": str(int(time.time()*1000)),
                       "bids": [["106", "10"]], "asks": [["106.1", "1"]]}
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/"paper.sqlite3")
            runner = Runner(Config(db=str(Path(tmp)/"paper.sqlite3"), enabled=False,
                                   allow_pending_wfa=False), FakeApi(), store)
            runner.start_validation = lambda symbol: None
            runner.cycle()
            with store.connect() as db:
                decisions = db.execute("SELECT payload FROM audit WHERE kind='decision'").fetchall()
            self.assertEqual(len(decisions), 1)
            self.assertEqual(json.loads(decisions[0][0])["final"]["decision"], "hold")
            self.assertEqual(runner.paper.positions(), [])

    def test_pending_wfa_can_trade_paper_but_rejected_cannot(self):
        now = int(time.time()*1000)
        base = now - 300*60000
        candles = [{"ts": base+i*60000, "o": 100+i*0.1, "h": 100.2+i*0.1,
                    "l": 99.8+i*0.1, "c": 100+i*0.1, "v": 1000, "confirm": "1"}
                   for i in range(300)]
        last = candles[-1]["c"]
        class FakeApi:
            def instruments(self): return {"BTC-USDT-SWAP": {"instId": "BTC-USDT-SWAP", "ctVal":"1",
                                                              "ctValCcy":"BTC", "lotSz":"1", "minSz":"1"}}
            def server_ms(self): return int(time.time()*1000)
            def tickers(self): return {"BTC-USDT-SWAP": {"volCcy24h": "100000", "last": str(last)}}
            def candles(self, symbol, limit=300): return candles
            def book(self, symbol): return {"ts": str(int(time.time()*1000)),
                                           "bids": [[str(last-0.01), "10"]],
                                           "asks": [[str(last), "10"]]}
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/"paper.sqlite3")
            runner = Runner(Config(db=str(Path(tmp)/"paper.sqlite3"), enabled=True), FakeApi(), store)
            runner.start_validation = lambda symbol: None
            runner.cycle()
            self.assertEqual(len(runner.paper.positions()), 1)
            self.assertEqual(store.dashboard_snapshot()["quick_backtests"]["PASS"], 1)
            with store.connect() as db:
                payload = json.loads(db.execute("SELECT payload FROM audit WHERE kind='decision' ORDER BY id DESC LIMIT 1").fetchone()[0])
            self.assertEqual(payload["final"]["validation_status"], "PENDING_PAPER")
            weights, status = runner.strategy_weights({"status": "REJECTED", "weights": {}},
                                                      {"weights": {"trend_momentum": 1.0}})
            self.assertEqual(weights, {})
            self.assertEqual(status, "REJECTED")


if __name__ == "__main__":
    unittest.main()
