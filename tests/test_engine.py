import tempfile
import time
import unittest
from pathlib import Path

from engine import PaperEngine


class FakeMarket:
    def __init__(self, now):
        self.now = now
        self.price = 150.0

    def ticker(self, symbol):
        return {"price": self.price, "ts": self.now}

    def candles(self, symbol):
        start = self.now - 70 * 15 * 60_000
        bars = []
        for i in range(70):
            close = 100 + i * 0.7
            bars.append({"ts": start + i * 15 * 60_000, "open": close - 0.3,
                         "high": close + 1, "low": close - 1, "close": close})
        return bars


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = int(time.time() * 1000)
        self.market = FakeMarket(self.now)
        self.engine = PaperEngine(Path(self.temp.name) / "test.sqlite", self.market)

    def tearDown(self):
        self.temp.cleanup()

    def test_open_once_then_virtual_stop(self):
        self.engine.tick_symbol("BTC-USDT-SWAP", self.now)
        state = self.engine.snapshot()
        self.assertEqual(len(state["positions"]), 1)
        self.assertEqual(state["positions"][0]["side"], "LONG")
        self.assertEqual(len([e for e in state["events"] if e["action"] == "OPEN_LONG"]), 1)
        self.engine.tick_symbol("BTC-USDT-SWAP", self.now)
        self.assertEqual(len([e for e in self.engine.snapshot()["events"] if e["action"] == "OPEN_LONG"]), 1)
        self.market.price = 100.0
        self.engine.tick_symbol("BTC-USDT-SWAP", self.now)
        closed = self.engine.snapshot()
        self.assertEqual(len(closed["positions"]), 0)
        self.assertTrue(any(e["action"] == "CLOSE_LONG" for e in closed["events"]))

    def test_stale_market_fails_closed(self):
        self.market.now -= 10 * 60_000
        self.engine.tick_symbol("BTC-USDT-SWAP", self.now)
        state = self.engine.snapshot()
        self.assertFalse(state["positions"])
        self.assertEqual(state["events"][0]["kind"], "DATA")

    def test_paper_ledger_survives_restart(self):
        self.engine.tick_symbol("BTC-USDT-SWAP", self.now)
        restarted = PaperEngine(self.engine.db_path, self.market)
        state = restarted.snapshot()
        self.assertEqual(len(state["positions"]), 1)
        restarted.tick_symbol("BTC-USDT-SWAP", self.now)
        self.assertEqual(len([e for e in restarted.snapshot()["events"] if e["action"] == "OPEN_LONG"]), 1)


if __name__ == "__main__":
    unittest.main()
