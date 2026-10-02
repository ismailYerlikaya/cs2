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
    def setUp(self):
        # Testler gerçek ağa çıkmasın; sahte oturum kullanan testler kendi Mock'unu atar.
        guard = patch.object(requests.Session, "request", side_effect=AssertionError("testte gerçek ağ isteği"))
        guard.start()
        self.addCleanup(guard.stop)

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

    def test_web_api_inventory_uses_key_and_response_wrapper(self):
        desc = {"classid": "10", "instanceid": "0", "marketable": 1, "market_hash_name": "Case"}
        client = Mock(json=Mock(return_value={"response": {"total_inventory_count": 1, "descriptions": [desc],
                                                           "assets": [{"assetid": "1", "classid": "10", "instanceid": "0", "amount": "3"}]}}))
        self.assertEqual(t.fetch_inventory(client, "76561197960287930", api_key="test-key"), [item()])
        args, kwargs = client.json.call_args
        self.assertEqual(args[1], t.WEB_API_INVENTORY_URL)
        self.assertEqual(kwargs["params"]["key"], "test-key")
        self.assertEqual(kwargs["params"]["steamid"], "76561197960287930")
        with self.assertRaises(t.TrackerError):
            t.fetch_inventory(Mock(json=Mock(return_value={})), "76561197960287930", api_key="test-key")

    def test_main_falls_back_to_community_inventory_when_web_api_fails(self):
        with tempfile.TemporaryDirectory() as folder,              patch.object(t, "HISTORY_PATH", Path(folder) / "history.json"), patch.object(t, "LATEST_PATH", Path(folder) / "latest.json"),              patch.dict(t.os.environ, {"STEAM_ID": "76561197960287930", "STEAM_API_KEY": "test-key", "TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "test"}),              patch.object(t.sys, "argv", ["tracker.py"]), patch.object(t, "load_dotenv"),              patch.object(t, "fetch_inventory", side_effect=[t.TrackerError("Steam: HTTP 403"), [item()]]) as inventory,              patch.object(t, "fetch_exchange_rate", return_value=FX),              patch.object(t, "collect_prices", return_value={"Case": Decimal("1")}),              patch.object(t, "send_telegram"):
            self.assertEqual(t.main(), 0)
            self.assertEqual(inventory.call_args_list[0].kwargs["api_key"], "test-key")
            self.assertNotIn("api_key", inventory.call_args_list[1].kwargs)

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

    def test_bulk_prices_use_steam_24h_values_only_for_inventory_items(self):
        client = Mock(json=Mock(return_value={
            "Case": {"steam": {"last_24h": 0.26}},
            "Other": {"steam": {"last_24h": "1.00"}},
            "Unused": {"steam": {"last_24h": 5}},
        }))
        self.assertEqual(t.fetch_bulk_market_prices(client, [item("Case"), item("Other")]),
                         {"Case": Decimal("0.26")})
        client.json.assert_called_once_with("GET", t.PRICE_URL, service="CSROI")

    def test_skinport_estimate_only_when_steam_price_missing(self):
        client = Mock(json=Mock(return_value={
            "Case": {"steam": {"last_24h": 0.26}, "skinport": {"suggested_price": 0.2}},
            "Slab": {"steam": {}, "skinport": {"suggested_price": 0.5}},
            "Zero": {"skinport": {"suggested_price": 0}},
        }))
        estimates = {}
        prices = t.fetch_bulk_market_prices(client, [item("Case"), item("Slab"), item("Zero"), item("Gone")], estimates=estimates)
        self.assertEqual(prices, {"Case": Decimal("0.26")})
        self.assertEqual(estimates, {"Slab": Decimal("0.5")})

    def test_change_compares_only_items_priced_the_same_way_in_both_runs(self):
        inventory = [item("Case"), item("Slab"), item("Sealed")]
        before = t.build_snapshot(inventory, {"Case": Decimal("1")}, FX, None, NOW - timedelta(hours=8))
        after = t.build_snapshot(inventory, {"Case": Decimal("1.1"), "Slab": (Decimal("2"), "skinport", None)}, FX, before, NOW)
        slab = after["items"][1]
        self.assertTrue(slab["price_estimated"])
        self.assertEqual(slab["price_label"], "Skinport önerilen fiyatı")
        self.assertEqual(slab["total_value"], 240)
        self.assertEqual(after["total_value"], 372)
        self.assertEqual((after["estimated_items"], after["estimated_value"]), (1, 240))
        self.assertEqual((after["change_value"], after["change_percent"], after["change_excluded_items"]), (12, 10, 2))
        self.assertFalse(after["complete"])
        report = t.make_report(after)
        self.assertIn("kısmı tahmini (1 item", report)
        self.assertIn("Fiyatlar: CSROI · Steam son 24 saat 1 · Skinport önerilen fiyatı 1", report)
        self.assertIn("2 item iki kontrolde", report)
        # Tahminden gerçek Steam fiyatına geçiş sahte hareket sayılmaz.
        later = t.build_snapshot(inventory, {"Case": Decimal("1.1"), "Slab": (Decimal("3"), "steam", NOW.isoformat())}, FX, after, NOW + timedelta(hours=8))
        self.assertFalse(later["items"][1]["price_estimated"])
        self.assertEqual(later["items"][1]["price_label"], "Steam Market (doğrudan) · 01.10 15:00")
        self.assertIn("🎯 Doğrudan Steam fiyatları: 01.10 15:00 – 01.10 15:00", t.make_report(later))
        self.assertIsNone(later["items"][1]["change_percent"])
        self.assertEqual(later["change_value"], 0)

    def test_market_price_is_lowest_listing_with_median_and_volume(self):
        client = Mock(json=Mock(return_value={"success": True, "lowest_price": "$1.30", "median_price": "$1.20", "volume": "1,698"}))
        self.assertEqual(t.fetch_market_price(client, "A"), {"usd": Decimal("1.30"), "median": Decimal("1.20"), "volume": 1698})
        self.assertEqual(client.json.call_args.kwargs["attempts"], 1)
        client.json.return_value = {"success": True, "lowest_price": "$0.96"}
        self.assertEqual(t.fetch_market_price(client, "B"), {"usd": Decimal("0.96"), "median": None, "volume": None})

    @patch.object(t.time, "sleep")
    def test_price_queries_stop_at_first_rate_limit_without_retrying(self, sleep):
        client = t.HttpClient()
        client.session.request = Mock(return_value=Mock(status_code=429, ok=False, headers={}, reason="Too Many Requests",
                                                        json=Mock(return_value=None)))
        with self.assertLogs(t.LOG, "WARNING") as logs:
            self.assertEqual(t.fetch_steam_prices(client, ["A", "B"]), {})
        self.assertEqual(client.session.request.call_count, 1)
        self.assertIn("0/2 itemda durdu", logs.output[0])

    def test_steam_prices_stop_at_rate_limit(self):
        quote = {"usd": Decimal("1"), "median": None, "volume": 3}
        with patch.object(t, "fetch_market_price", side_effect=[quote, t.SteamRateLimitError("429"), quote]) as fetch, \
             self.assertLogs(t.LOG, "WARNING"):
            found = t.fetch_steam_prices(Mock(), ["A", "B", "C"])
        self.assertEqual(list(found), ["A"])
        self.assertEqual(found["A"]["usd"], Decimal("1"))
        self.assertIn("checked_at", found["A"])
        self.assertEqual(fetch.call_count, 2)

    def test_collect_prices_uses_best_source_in_order(self):
        inventory = [item(name) for name in ["Fresh", "Csroi", "Live", "Cached", "Old", "Skin", "Last"]]
        def bulk(client, inventory, estimates):
            self.assertNotIn("Fresh", [x["market_hash_name"] for x in inventory])
            estimates.update({"Old": Decimal("0.4"), "Skin": Decimal("0.5")})
            return {"Csroi": Decimal("1")}
        stamp = lambda **age: (NOW - timedelta(**age)).isoformat()
        cache = {"Fresh": {"usd": 5.0, "median": None, "volume": 3, "checked_at": stamp(hours=2)},
                 "Csroi": {"usd": 9.0, "median": None, "volume": None, "checked_at": stamp(hours=13)},
                 "Cached": {"usd": 0.7, "median": None, "volume": None, "checked_at": stamp(days=2)},
                 "Old": {"usd": 0.9, "median": None, "volume": None, "checked_at": stamp(days=8)}}
        live = {"usd": Decimal("2"), "median": Decimal("1.9"), "volume": 4, "checked_at": NOW.isoformat()}
        with tempfile.TemporaryDirectory() as folder, patch.object(t, "STEAM_PRICES_PATH", Path(folder) / "steam_prices.json"):
            t.write_json(t.STEAM_PRICES_PATH, cache)
            with patch.object(t, "fetch_bulk_market_prices", side_effect=bulk), \
                 patch.object(t, "fetch_steam_prices", return_value={"Live": live}) as steam, \
                 patch.object(t, "fetch_csgotrader_prices", return_value={"Last": (Decimal("3"), "last_7d")}) as csgotrader:
                quotes = t.collect_prices(Mock(), inventory, NOW)
            self.assertEqual(steam.call_args.args[1], ["Live", "Cached", "Old", "Skin", "Last"])
            self.assertEqual(csgotrader.call_args.args[1], ["Last"])
            self.assertEqual({name: quote[:2] for name, quote in quotes.items()}, {
                "Fresh": (Decimal("5"), "steam"), "Csroi": (Decimal("1"), "csroi"), "Live": (Decimal("2"), "steam"),
                "Cached": (Decimal("0.7"), "steam_last"), "Old": (Decimal("0.4"), "skinport"),
                "Skin": (Decimal("0.5"), "skinport"), "Last": (Decimal("3"), "csgotrader")})
            saved = json.loads(t.STEAM_PRICES_PATH.read_text(encoding="utf-8"))
            self.assertEqual(saved["Live"], {"usd": 2.0, "median": 1.9, "volume": 4, "checked_at": NOW.isoformat()})
            self.assertIn("Old", saved)

    def test_collect_prices_survives_csroi_outage(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(t, "STEAM_PRICES_PATH", Path(folder) / "steam_prices.json"):
            with patch.object(t, "fetch_bulk_market_prices", side_effect=t.TrackerError("CSROI: HTTP 503")), \
                 patch.object(t, "fetch_steam_prices", return_value={}), \
                 patch.object(t, "fetch_csgotrader_prices", return_value={"Case": (Decimal("1.5"), "last_24h")}), \
                 self.assertLogs(t.LOG, "WARNING"):
                self.assertEqual(t.collect_prices(Mock(), [item()], NOW), {"Case": (Decimal("1.5"), "csgotrader", "last_24h")})
            self.assertFalse(t.STEAM_PRICES_PATH.exists())

    def test_home_refresh_asks_missing_then_oldest_and_skips_recent(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(t, "STEAM_PRICES_PATH", Path(folder) / "steam_prices.json"), \
             patch.object(t, "LATEST_PATH", Path(folder) / "latest.json"):
            t.write_json(t.LATEST_PATH, {"items": [item(name) for name in ["Recent", "Old", "Older", "New"]]})
            t.write_json(t.STEAM_PRICES_PATH, {name: {"usd": 1, "checked_at": (NOW - age).isoformat()} for name, age in
                                               [("Recent", timedelta(hours=1)), ("Old", timedelta(hours=5)), ("Older", timedelta(days=2))]})
            self.assertEqual(t.steam_refresh_order(NOW), ["New", "Older", "Old"])

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
        # Görünmeyen item iki tarafta da yok; görünen itemların değişimi yine hesaplanır.
        self.assertEqual(after["change_value"], 4)
        self.assertEqual(after["items"][0]["change_percent"], 10)
        self.assertIn("Steam 1 itemın ayrıntılarını göstermedi", t.make_report(after))

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

    def test_price_source_change_resets_comparison(self):
        before = t.build_snapshot([item()], {"Case": Decimal("1")}, FX, None, NOW - timedelta(hours=8),
                                  price_source="old source")
        after = t.build_snapshot([item()], {"Case": Decimal("2")}, FX, before, NOW,
                                 price_source="new source")
        self.assertIsNone(after["items"][0]["change_percent"])
        self.assertIsNone(after["change_value"])

    def test_missing_price_never_creates_false_crash(self):
        before = t.build_snapshot([item()], {"Case": Decimal("1")}, FX, None, NOW - timedelta(hours=8))
        partial = t.build_snapshot([item()], {}, FX, before, NOW)
        self.assertEqual(partial["status"], "partial")
        self.assertIsNone(partial["items"][0]["current_price"])
        self.assertIsNone(partial["change_percent"])
        recovered = t.build_snapshot([item()], {"Case": Decimal("1.1")}, FX, partial, NOW + timedelta(hours=8))
        self.assertIsNone(recovered["change_value"])
        self.assertNotIn("Önceki kontrol", t.make_report(recovered))
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
        throttled = Mock(status_code=429, ok=False, reason="Too Many Requests", headers={"Retry-After": "12"})
        good = Mock(status_code=200, ok=True)
        client.session.request = Mock(side_effect=[throttled, good])
        self.assertIs(client.request("GET", "https://example.invalid", service="TCMB"), good)
        sleep.assert_called_with(12)
        client.session.request = Mock(side_effect=requests.ConnectionError("secret-token-in-url"))
        with self.assertRaises(t.TrackerError) as error, self.assertLogs(t.LOG, "WARNING") as logs:
            client.request("POST", "https://api.telegram.org/botsecret-token/sendMessage", service="Telegram")
        self.assertNotIn("secret", str(error.exception) + "".join(logs.output))
        self.assertIn("ConnectionError", str(error.exception))
        self.assertIn("api.telegram.org/bot***/sendMessage", logs.output[0])
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
    def test_steam_rate_limit_uses_short_exponential_backoff_and_logs_cause(self, sleep):
        client = t.HttpClient()
        throttled = Mock(status_code=429, ok=False, headers={}, reason="Too Many Requests", json=Mock(side_effect=ValueError))
        success = Mock(status_code=200, ok=True)
        client.session.request = Mock(side_effect=[throttled, throttled, throttled, success])
        url = "https://steamcommunity.com/inventory/76561197960287930/730/2"
        with self.assertLogs(t.LOG, "WARNING") as logs:
            self.assertIs(client.request("GET", url, service="Steam"), success)
        self.assertEqual(client.session.request.call_count, 4)
        delays = [entry.args[0] for entry in sleep.call_args_list if entry.args and entry.args[0] in (10, 20, 40)]
        self.assertEqual(delays, [10, 20, 40])
        self.assertIn("HTTP 429 Too Many Requests", logs.output[0])
        self.assertIn("steamcommunity.com/inventory/<STEAM_ID>/730/2", logs.output[0])
        self.assertNotIn("76561197960287930", "".join(logs.output))

    @patch.object(t.time, "sleep")
    def test_steam_rate_limit_gives_up_with_status(self, sleep):
        client = t.HttpClient()
        client.session.request = Mock(return_value=Mock(status_code=429, ok=False, headers={}, reason="Too Many Requests",
                                                        json=Mock(return_value=None)))
        with self.assertRaises(t.SteamRateLimitError) as error, self.assertLogs(t.LOG, "WARNING"):
            client.request("GET", "https://steamcommunity.com/inventory/76561197960287930/730/2", service="Steam")
        self.assertIn("HTTP 429", str(error.exception))
        self.assertEqual(client.session.request.call_count, t.STEAM_MAX_ATTEMPTS)
        self.assertLess(sum(entry.args[0] for entry in sleep.call_args_list if entry.args), 120)

    @patch.object(t.time, "sleep")
    def test_steam_forbidden_fails_without_retry(self, sleep):
        client = t.HttpClient()
        client.session.request = Mock(return_value=Mock(status_code=403, ok=False, headers={}, reason="Forbidden",
                                                        json=Mock(return_value=None)))
        with self.assertRaises(t.TrackerError) as error:
            client.request("GET", "https://steamcommunity.com/inventory/76561197960287930/730/2", service="Steam")
        self.assertIn("HTTP 403 Forbidden", str(error.exception))
        self.assertEqual(client.session.request.call_count, 1)

    @patch.object(t.time, "sleep")
    def test_steam_timeout_retries_every_attempt(self, sleep):
        client = t.HttpClient()
        client.session.request = Mock(side_effect=requests.ReadTimeout("x"))
        with self.assertRaises(t.TrackerError) as error, self.assertLogs(t.LOG, "WARNING") as logs:
            client.request("GET", "https://steamcommunity.com/inventory/76561197960287930/730/2", service="Steam")
        self.assertIn("ReadTimeout", str(error.exception))
        self.assertEqual(client.session.request.call_count, t.STEAM_MAX_ATTEMPTS)
        self.assertEqual(len(logs.output), t.STEAM_MAX_ATTEMPTS - 1)

    def test_steam_error_text_and_non_json_are_reported(self):
        page = {"success": 0, "error": "EYldRefreshAppIfNecessary failed with EResult 55"}
        with self.assertRaises(t.TrackerError) as error:
            t.fetch_inventory(Mock(json=Mock(return_value=page)), "76561197960287930")
        self.assertIn("EResult 55", str(error.exception))
        client = t.HttpClient()
        html = Mock(status_code=200, ok=True, headers={"Content-Type": "text/html; charset=utf-8"}, json=Mock(side_effect=ValueError))
        client.request = Mock(return_value=html)
        with self.assertRaises(t.TrackerError) as error:
            client.json("GET", "https://steamcommunity.com/market/priceoverview/", service="Steam")
        self.assertIn("text/html", str(error.exception))

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
                 patch.object(t, "collect_prices", return_value={"Case": Decimal("2")}) as price, \
                 patch.object(t, "send_telegram") as send:
                self.assertEqual(t.main(), 0)
                self.assertEqual(price.call_count, 1)
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
                 patch.object(t, "collect_prices", return_value={}), \
                 patch.object(t, "send_telegram") as send:
                self.assertEqual(t.main(), 1)
                self.assertEqual(history.read_text(), "[]")
                self.assertEqual(latest.read_text(), '{"unchanged":true}')
                self.assertIn("Toplu fiyat kaynağında", send.call_args.args[-1])


    def run_main(self, folder, history, env=None, **patches):
        paths = Path(folder) / "history.json", Path(folder) / "latest.json"
        t.write_json(paths[0], history)
        environ = {"STEAM_ID": "76561197960287930", "TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "test", **(env or {})}
        with patch.object(t, "HISTORY_PATH", paths[0]), patch.object(t, "LATEST_PATH", paths[1]),              patch.dict(t.os.environ, environ), patch.object(t.sys, "argv", ["tracker.py"]), patch.object(t, "load_dotenv"),              patch.object(t, "fetch_exchange_rate", return_value=FX),              patch.object(t, "collect_prices", return_value={"Case": Decimal("1.1")}),              patch.object(t, "fetch_inventory", **patches) as inventory, patch.object(t, "send_telegram") as send:
            return t.main(), inventory, send, paths

    def test_steam_block_uses_last_known_inventory_with_fresh_prices(self):
        before = t.build_snapshot([item()], {"Case": Decimal("1")}, FX, None, datetime.now(timezone.utc) - timedelta(hours=8))
        with tempfile.TemporaryDirectory() as folder:
            code, _, send, paths = self.run_main(folder, [before], side_effect=t.SteamRateLimitError("Steam istek sınırı (HTTP 429)"))
            self.assertEqual(code, 0)
            latest = json.loads(paths[1].read_text(encoding="utf-8"))
            self.assertEqual(latest["inventory_source"], "cache")
            self.assertEqual(latest["inventory_checked_at"], before["checked_at"])
            self.assertEqual(latest["items"][0]["change_percent"], 10)
            self.assertEqual(latest["change_value"], 12)
            self.assertIn("son envanter kullanıldı", send.call_args.args[-1])

    def test_scheduled_run_skips_when_recent_report_exists(self):
        recent = t.build_snapshot([item()], {"Case": Decimal("1")}, FX, None, datetime.now(timezone.utc) - timedelta(hours=2))
        with tempfile.TemporaryDirectory() as folder:
            code, inventory, send, _ = self.run_main(folder, [recent], {"MIN_HOURS_BETWEEN_REPORTS": "7.5"}, return_value=[item()])
            self.assertEqual(code, 0)
            inventory.assert_not_called()
            send.assert_not_called()

    def test_first_inventory_failure_is_quiet_on_schedule_but_reported_manually(self):
        with tempfile.TemporaryDirectory() as folder:
            code, _, send, _ = self.run_main(folder, [], {"MIN_HOURS_BETWEEN_REPORTS": "7.5"}, side_effect=t.SteamRateLimitError("429"))
            self.assertEqual(code, 1)
            send.assert_not_called()
            code, _, send, _ = self.run_main(folder, [], {"MIN_HOURS_BETWEEN_REPORTS": "0"}, side_effect=t.SteamRateLimitError("429"))
            self.assertEqual(code, 1)
            send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
