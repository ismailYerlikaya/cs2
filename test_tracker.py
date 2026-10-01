"""Yerel mantık/hata senaryosu testleri; canlı Steam/Telegram doğrulaması değildir."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, call, patch

import requests
import tracker as t


NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
FX = {"source": "test", "date": "2026-10-01", "usd_try": 40}


def item(name="Case", quantity=3):
    return {"market_hash_name": name, "display_name": name, "quantity": quantity, "image_url": None}


class TrackerTests(unittest.TestCase):
    def test_inventory_pages_group_amount_and_deduplicate(self):
        desc = {"classid": "10", "instanceid": "0", "marketable": 1, "market_hash_name": "Case"}
        a = {"assetid": "1", "classid": "10", "amount": "2"}
        b = {"assetid": "2", "classid": "10", "amount": "1"}
        c = {"assetid": "3", "classid": "20", "amount": "1"}
        client = Mock()
        client.json.side_effect = [
            {"success": 1, "total_inventory_count": 3, "assets": [a], "descriptions": [desc], "more_items": 1, "last_assetid": "1"},
            {"success": 1, "total_inventory_count": 3, "assets": [a, b, c], "descriptions": [desc, {"classid": "20", "marketable": 0}]}]
        self.assertEqual(t.fetch_inventory(client, "76561197960287930"), [item()])
        self.assertEqual(client.json.call_count, 2)

    def test_private_and_incomplete_inventory_rejected(self):
        for page in [{"success": 0}, {"success": 1}, {"success": 1, "total_inventory_count": 2, "assets": []}]:
            with self.subTest(page=page), self.assertRaises(t.TrackerError):
                t.fetch_inventory(Mock(json=Mock(return_value=page)), "76561197960287930")
        self.assertEqual(t.fetch_inventory(Mock(json=Mock(return_value={"success": 1, "total_inventory_count": 0})), "76561197960287930"), [])

    def test_price_currency_and_no_median_fallback(self):
        self.assertEqual(t.parse_usd_price("$1,234.56"), Decimal("1234.56"))
        self.assertEqual(t.parse_usd_price("$0.03 USD"), Decimal("0.03"))
        for value in ["12,34 TL", "€12.34", "$1.234,56", "NaN", "$-1"]:
            with self.assertRaises(t.TrackerError):
                t.parse_usd_price(value)
        with self.assertRaises(t.TrackerError):
            t.fetch_market_price(Mock(json=Mock(return_value={"success": True, "median_price": "$1.00"})), "Case")

    def test_finished_inventory_with_unavailable_asset_is_explicitly_partial(self):
        page = {"success": 1, "total_inventory_count": 2,
                "assets": [{"assetid": "1", "classid": "10", "amount": "1"}],
                "descriptions": [{"classid": "10", "marketable": 1, "market_hash_name": "Case"}]}
        metadata = {}
        inventory = t.fetch_inventory(Mock(json=Mock(return_value=page)), "76561197960287930", metadata=metadata)
        self.assertEqual(metadata, {"reported_assets": 2, "returned_assets": 1, "unavailable_assets": 1})
        before = t.build_snapshot([item(quantity=1)], {"Case": Decimal("1")}, FX, None, NOW - timedelta(hours=8))
        after = t.build_snapshot(inventory, {"Case": Decimal("1.1")}, FX, before, NOW, metadata)
        self.assertEqual(after["total_value"], 44)
        self.assertFalse(after["complete"])
        self.assertEqual(after["status"], "partial")
        self.assertIsNone(after["change_value"])
        self.assertEqual(after["items"][0]["change_percent"], 10)
        self.assertIn("Steam 1 itemın ayrıntılarını göstermedi", t.make_report(after))
        recovered = t.build_snapshot(inventory, {"Case": Decimal("1.1")}, FX, after, NOW + timedelta(hours=8))
        self.assertIsNone(recovered["change_value"])

    def test_totals_fx_and_thresholds(self):
        before = t.build_snapshot([item()], {"Case": Decimal("1")}, FX, None, NOW - timedelta(hours=8))
        after = t.build_snapshot([item()], {"Case": Decimal("1.1")}, FX, before, NOW)
        self.assertEqual(after["total_value"], 132)
        self.assertEqual(after["change_value"], 12)
        self.assertEqual(after["change_percent"], 10)
        self.assertEqual(after["interval_hours"], 8)
        self.assertEqual(after["items"][0]["alarm"], "🔥 Fiyat alarmı")
        self.assertFalse(after["inventory_changed"])
        self.assertEqual(t.alarm(-10), "🚨 Büyük düşüş")
        self.assertEqual(t.alarm(-5), "📉 Düşüş")
        self.assertEqual(t.alarm(5), "📈 Yükseliş")
        self.assertIsNone(t.alarm(4.99))
        self.assertIsNone(t.percent(10, 0))

    def test_missing_price_never_creates_false_crash(self):
        before = t.build_snapshot([item()], {"Case": Decimal("1")}, FX, None, NOW - timedelta(hours=8))
        partial = t.build_snapshot([item()], {}, FX, before, NOW)
        self.assertEqual(partial["status"], "partial")
        self.assertIsNone(partial["items"][0]["current_price"])
        self.assertIsNone(partial["change_percent"])
        recovered = t.build_snapshot([item()], {"Case": Decimal("1.1")}, FX, partial, NOW + timedelta(hours=8))
        self.assertIsNone(recovered["change_value"])
        self.assertIsNone(recovered["items"][0]["change_percent"])

    def test_quantity_change_is_disclosed(self):
        before = t.build_snapshot([item()], {"Case": Decimal("1")}, FX, None, NOW - timedelta(hours=8))
        after = t.build_snapshot([item(quantity=4)], {"Case": Decimal("1")}, FX, before, NOW)
        self.assertTrue(after["inventory_changed"])
        self.assertEqual(after["items"][0]["change_percent"], 0)
        self.assertIn("yalnızca fiyat hareketi değildir", t.make_report(after))

    def test_retention_and_corrupt_history(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(t, "HISTORY_PATH", Path(folder) / "history.json"):
            recent = t.build_snapshot([], {}, FX, None, NOW)
            old = t.build_snapshot([], {}, FX, None, NOW - timedelta(days=31))
            t.write_json(t.HISTORY_PATH, [old, recent])
            self.assertEqual(t.read_history(NOW), [recent])
            t.HISTORY_PATH.write_text("{broken", encoding="utf-8")
            with self.assertRaises(t.TrackerError):
                t.read_history(NOW)
            self.assertEqual(t.HISTORY_PATH.read_text(), "{broken")

    @patch.object(t.time, "sleep")
    def test_retry_after_and_secret_redaction(self, sleep):
        client = t.HttpClient()
        throttled = Mock(status_code=429, headers={"Retry-After": "12"})
        good = Mock(status_code=200, ok=True)
        client.session.request = Mock(side_effect=[throttled, good])
        self.assertIs(client.request("GET", "https://example.invalid", service="TCMB"), good)
        sleep.assert_called_with(12)
        client.session.request = Mock(side_effect=requests.ConnectionError("secret-token-in-url"))
        with self.assertRaises(t.TrackerError) as error:
            client.request("POST", "https://example.invalid/secret", service="Telegram")
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(client.session.request.call_count, 3)

    @patch.object(t.time, "sleep")
    def test_long_rate_limit_does_not_retry_early(self, sleep):
        client = t.HttpClient()
        client.session.request = Mock(return_value=Mock(status_code=429, headers={"Retry-After": "601"}))
        with self.assertRaises(t.TrackerError):
            client.request("GET", "https://example.invalid", service="Steam")
        self.assertEqual(client.session.request.call_count, 1)
        self.assertFalse(any(entry.args and entry.args[0] > 0 for entry in sleep.call_args_list))

    @patch.object(t.time, "sleep")
    def test_steam_rate_limit_uses_long_exponential_backoff(self, sleep):
        client = t.HttpClient()
        throttled = Mock(status_code=429, headers={})
        success = Mock(status_code=200, ok=True)
        client.session.request = Mock(side_effect=[throttled, throttled, throttled, throttled, success])
        self.assertIs(client.request("GET", "https://example.invalid", service="Steam"), success)
        self.assertEqual(client.session.request.call_count, 5)
        delays = [entry.args[0] for entry in sleep.call_args_list if entry.args and entry.args[0] in (30, 60, 120, 240)]
        self.assertEqual(delays, [30, 60, 120, 240])

    @patch.object(t.time, "sleep")
    def test_all_major_alerts_and_telegram_size(self, sleep):
        inventory = [item("🔥" * 80 + str(i)) for i in range(40)]
        before = t.build_snapshot(inventory, {x["market_hash_name"]: Decimal("1") for x in inventory}, FX, None, NOW - timedelta(hours=8))
        after = t.build_snapshot(inventory, {x["market_hash_name"]: Decimal("2") for x in inventory}, FX, before, NOW)
        report = t.make_report(after)
        for entry in inventory:
            self.assertIn(entry["market_hash_name"], report)
        client = Mock(json=Mock(return_value={"ok": True}))
        t.send_telegram(client, "unused-test-token", "unused-test-chat", report)
        self.assertGreater(client.json.call_count, 1)
        for call in client.json.call_args_list:
            self.assertLessEqual(len(call.kwargs["json"]["text"].encode("utf-16-le")) // 2, 3500)

    def test_fx_stale_rejected(self):
        response = Mock(content=b'<Tarih_Date Date="09/01/2026"><Currency Kod="USD"><ForexSelling>40</ForexSelling></Currency></Tarih_Date>')
        with self.assertRaises(t.TrackerError):
            t.fetch_exchange_rate(Mock(request=Mock(return_value=response)), NOW)

    def test_main_prices_each_unique_item_once_and_writes_real_structure(self):
        with tempfile.TemporaryDirectory() as folder:
            history, latest = Path(folder) / "history.json", Path(folder) / "latest.json"
            with patch.object(t, "HISTORY_PATH", history), patch.object(t, "LATEST_PATH", latest), \
                 patch.dict(t.os.environ, {"STEAM_ID": "76561197960287930", "TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "test"}), \
                 patch.object(t.sys, "argv", ["tracker.py"]), patch.object(t, "load_dotenv"), \
                 patch.object(t, "fetch_inventory", return_value=[item(quantity=20), item("Other")]), \
                 patch.object(t, "fetch_exchange_rate", return_value=FX), \
                 patch.object(t, "fetch_market_price", side_effect=[Decimal("2"), t.TrackerError("unavailable")]) as price, \
                 patch.object(t, "send_telegram") as send:
                self.assertEqual(t.main(), 0)
                self.assertEqual(price.call_count, 2)
                result = json.loads(latest.read_text(encoding="utf-8"))
                self.assertEqual(result["total_value"], 1600)
                self.assertEqual(result["missing_prices"], 1)
                self.assertEqual(json.loads(history.read_text(encoding="utf-8")), [result])
                self.assertEqual(send.call_count, 1)

    def test_all_prices_fail_preserves_files_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            history, latest = Path(folder) / "history.json", Path(folder) / "latest.json"
            history.write_text("[]", encoding="utf-8")
            latest.write_text('{"unchanged":true}', encoding="utf-8")
            with patch.object(t, "HISTORY_PATH", history), patch.object(t, "LATEST_PATH", latest), \
                 patch.dict(t.os.environ, {"STEAM_ID": "76561197960287930", "TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "test"}), \
                 patch.object(t.sys, "argv", ["tracker.py"]), patch.object(t, "load_dotenv"), \
                 patch.object(t, "fetch_inventory", return_value=[item()]), \
                 patch.object(t, "fetch_exchange_rate", return_value=FX), \
                 patch.object(t, "fetch_market_price", side_effect=t.SteamRateLimitError("limited")), \
                 patch.object(t, "send_telegram") as send:
                self.assertEqual(t.main(), 1)
                self.assertEqual(history.read_text(), "[]")
                self.assertEqual(latest.read_text(), '{"unchanged":true}')
                self.assertIn("Hiçbir item", send.call_args.args[-1])


if __name__ == "__main__":
    unittest.main()
