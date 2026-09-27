import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from bot.store import Store
from bot.web import DashboardRuntime, DashboardServer


class WebTests(unittest.TestCase):
    def test_health_login_and_read_only_dashboard(self):
        runtime = DashboardRuntime()
        with patch.dict("os.environ", {"DASHBOARD_PASSWORD": "local-test-password-123"}):
            server = DashboardServer(("127.0.0.1", 0), runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            def request(method, path, body=None, headers=None):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                data = response.read()
                result = (response.status, dict(response.getheaders()), data)
                connection.close()
                return result
            code, _, payload = request("GET", "/health")
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(payload)["status"], "ok")
            code, _, _ = request("GET", "/api/status")
            self.assertEqual(code, 401)
            body = urlencode({"password": "local-test-password-123"})
            code, headers, _ = request("POST", "/login", body,
                                      {"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(code, 303)
            cookie = headers["Set-Cookie"].split(";", 1)[0]
            self.assertIn("HttpOnly", headers["Set-Cookie"])
            code, _, payload = request("GET", "/api/status", headers={"Cookie": cookie})
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(payload)["state"], "starting")
            code, _, payload = request("GET", "/", headers={"Cookie": cookie})
            self.assertEqual(code, 200)
            self.assertIn(b"Komuta Merkezi", payload)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_dashboard_and_monthly_report_from_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "bot.sqlite3")
            now = datetime.now(timezone.utc)
            earlier = (now - timedelta(hours=1)).isoformat(timespec="seconds")
            current = now.isoformat(timespec="seconds")
            with store.connect() as db:
                db.execute("INSERT INTO equity VALUES(?,?)", (earlier, 10000))
                db.execute("INSERT INTO equity VALUES(?,?)", (current, 10100))
                db.execute("INSERT INTO closed_positions VALUES(?,?,?,?,?)",
                           ("close-1", int(now.timestamp()*1000), 25, 60000, "{}"))
            store.audit("universe", {"count": 2, "symbols": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]})
            store.save_full_backtest("BTC-USDT-SWAP", {"symbol": "BTC-USDT-SWAP", "bars": 43200})
            snapshot = store.dashboard_snapshot()
            self.assertEqual(snapshot["universe_count"], 2)
            self.assertEqual(snapshot["equity_usd"], 10100)
            self.assertEqual(snapshot["full_backtests"]["DONE"], 1)
            report = store.performance(30)
            self.assertEqual(report["win_rate_pct"], 100)
            self.assertEqual(report["closed_positions"], 1)


if __name__ == "__main__":
    unittest.main()
