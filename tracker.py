"""Tek hesap için CS2 envanter takibi. Python 3.11+; veriler yalnızca JSON."""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

import requests
from dotenv import load_dotenv

ALARM_UP = 10
NOTICE_UP = 5
ALARM_DOWN = -10
NOTICE_DOWN = -5
REQUEST_DELAY = 3  # Steam'e her deneme dahil en az bu kadar saniye ara verilir.
MAX_ATTEMPTS = 3
STEAM_MAX_ATTEMPTS = 4  # Beklemeler 10, 20, 40 sn; GitHub Actions dakikalarca takılmaz.
STEAM_RETRY_BASE_SECONDS = 10
TIMEOUT = (10, 25)
MAX_RETRY_WAIT = 120  # Daha uzun Retry-After istenirse beklemeden hata verilir.
RETRYABLE_STATUS = (429, 500, 502, 503, 504)
HISTORY_DAYS = 30
PRICE_SOURCE = "CSROI.com · Steam son 24 saat fiyatı"
PRICE_URL = "https://csroi.com/pricing.json"
WEB_API_INVENTORY_URL = "https://api.steampowered.com/IEconService/GetInventoryItemsWithDescriptions/v1/"
ROOT = Path(__file__).resolve().parent
HISTORY_PATH = ROOT / "data/history.json"
LATEST_PATH = ROOT / "site/data/latest.json"
TR_TIME = timezone(timedelta(hours=3))
LOG = logging.getLogger("tracker")


class TrackerError(Exception):
    """Kullanıcıya gösterilebilecek, gizli bilgi içermeyen hata."""


class SteamRateLimitError(TrackerError):
    """Bu çalıştırmada başka Steam isteği gönderilmemeli."""


def safe_endpoint(url):
    """Log için host + yol. Sorgu yazılmaz; Telegram tokenı ve SteamID maskelenir."""
    parts = urlsplit(url)
    path = re.sub(r"/bot[^/]+/", "/bot***/", parts.path)
    return parts.netloc + re.sub(r"/7656119\d{10}(?=/|$)", "/<STEAM_ID>", path)


def short_error(response):
    """Yanıttaki kısa hata açıklaması (Steam error/Error, Telegram description)."""
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        for key in ("error", "Error", "description", "message"):
            if data.get(key):
                return " ".join(str(data[key]).split())[:150]
    return response.reason or ""


class HttpClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "PersonalCS2Tracker/1.0"})
        self.last_steam_request = 0.0

    def request(self, method, url, *, service, **kwargs):
        max_attempts = STEAM_MAX_ATTEMPTS if service == "Steam" else MAX_ATTEMPTS
        endpoint = safe_endpoint(url)
        for attempt in range(max_attempts):
            if service == "Steam":
                time.sleep(max(0, REQUEST_DELAY - (time.monotonic() - self.last_steam_request)))
                self.last_steam_request = time.monotonic()
            wait = (STEAM_RETRY_BASE_SECONDS if service == "Steam" else 5) * (2 ** attempt)
            try:
                response = self.session.request(method, url, timeout=TIMEOUT, **kwargs)
            except requests.RequestException as error:
                # İstisna metni URL içindeki Telegram tokenını içerebilir; yalnızca türü yazılır.
                reason = type(error).__name__
                if attempt == max_attempts - 1:
                    raise TrackerError(f"{service}: bağlantı kurulamadı veya zaman aşımı ({reason}, {endpoint}).") from None
            else:
                if response.ok and response.status_code not in RETRYABLE_STATUS:
                    return response
                retry = response.headers.get("Retry-After")
                reason = f"HTTP {response.status_code} {short_error(response)}".strip()
                if retry:
                    reason += f", Retry-After={retry}"
                if response.status_code not in RETRYABLE_STATUS:
                    if service == "Steam" and response.status_code in (401, 403):
                        raise TrackerError(f"Steam erişimi reddetti ({reason}, {endpoint}). Envanter Public olmalı; IP engeli de olabilir.")
                    raise TrackerError(f"{service}: {reason} ({endpoint}).")
                if retry:
                    try:
                        wait = max(wait, float(retry))
                    except ValueError:
                        try:
                            wait = max(wait, (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds())
                        except (ValueError, TypeError, OverflowError):
                            pass
                if service == "Telegram":
                    try:
                        wait = max(wait, float(response.json().get("parameters", {}).get("retry_after", 0)))
                    except (ValueError, TypeError, AttributeError):
                        pass
                if attempt == max_attempts - 1 or wait > MAX_RETRY_WAIT:
                    if service == "Steam" and response.status_code == 429:
                        raise SteamRateLimitError(f"Steam istek sınırı ({reason}, {endpoint}). GitHub Actions IP'si geçici olarak sınırlanmış olabilir; sonraki planlı çalıştırmada yeniden denenecek.")
                    raise TrackerError(f"{service}: istek sınırı veya geçici sunucu hatası ({reason}, {endpoint}).")
            LOG.warning("%s yanıt vermedi (%s, %s); %g saniye sonra yeniden deneniyor (deneme %s/%s).",
                        service, reason, endpoint, wait, attempt + 1, max_attempts)
            time.sleep(wait)
        raise TrackerError(f"{service}: istek tamamlanamadı.")

    def json(self, method, url, *, service, **kwargs):
        response = self.request(method, url, service=service, **kwargs)
        try:
            data = response.json()
        except ValueError:
            content_type = response.headers.get("Content-Type", "?").split(";")[0]
            raise TrackerError(f"{service}: geçerli JSON dönmedi (HTTP {response.status_code}, {content_type}, {safe_endpoint(url)}).") from None
        if not isinstance(data, dict):
            raise TrackerError(f"{service}: beklenmeyen yanıt biçimi.")
        return data


def fetch_inventory(client, steam_id, *, metadata=None, api_key=None):
    """api_key verilirse Steam Web API kullanılır; sınırı IP'ye değil key'e göredir."""
    if not re.fullmatch(r"7656119\d{10}", steam_id):
        raise TrackerError("STEAM_ID 17 haneli SteamID64 olmalı; profil adı veya bağlantısı değil.")
    grouped, seen_assets, cursors = {}, set(), set()
    params = {"count": 2000}
    expected = None
    while True:
        if api_key:
            # Key sorgu parametresinde kalır; loglar yalnızca host + yol yazar.
            page = client.json("GET", WEB_API_INVENTORY_URL, service="Steam",
                               params={"key": api_key, "steamid": steam_id, "appid": 730, "contextid": 2,
                                       "get_descriptions": "true", "language": "english", **params}).get("response")
            if not isinstance(page, dict):
                raise TrackerError("Steam Web API envanter yanıtı boş veya beklenmeyen biçimde.")
            page = {"success": 1, **page}
        else:
            page = client.json("GET", f"https://steamcommunity.com/inventory/{steam_id}/730/2",
                               service="Steam", params={"l": "english", **params})
        if page.get("success") != 1:
            detail = f" (Steam: {' '.join(str(page['error']).split())[:150]})" if page.get("error") else ""
            raise TrackerError(f"Steam envanteri alınamadı{detail}. SteamID64, envanter gizliliği ve Steam erişimini kontrol edin.")
        if "total_inventory_count" in page:
            count = int(page["total_inventory_count"])
            if expected is not None and count != expected:
                raise TrackerError("Envanter çekilirken değişti; sonraki çalıştırmada yeniden deneyin.")
            expected = count
        assets = page.get("assets", [])
        if not assets and expected != 0:
            raise TrackerError("Steam eksik envanter döndürdü; önceki kayıtlar korunuyor.")
        descriptions = {(str(d["classid"]), str(d.get("instanceid", "0"))): d
                        for d in page.get("descriptions", [])}
        for asset in assets:
            asset_id = str(asset["assetid"])
            if asset_id in seen_assets:
                continue
            seen_assets.add(asset_id)
            desc = descriptions.get((str(asset["classid"]), str(asset.get("instanceid", "0"))))
            if desc is None:
                raise TrackerError("Steam bazı item açıklamalarını göndermedi; eksik envanter kaydedilmedi.")
            if str(desc.get("marketable", 0)) != "1":
                continue
            name = desc.get("market_hash_name")
            if not name:
                raise TrackerError("Steam marketable item için market_hash_name göndermedi.")
            item = grouped.setdefault(name, {
                "market_hash_name": name,
                "display_name": desc.get("name") or name,
                "quantity": 0,
                "image_url": ("https://community.cloudflare.steamstatic.com/economy/image/" + desc["icon_url"]
                              if desc.get("icon_url") else None),
            })
            amount = int(asset.get("amount", 1))
            if amount < 1:
                raise TrackerError("Steam geçersiz item adedi döndürdü.")
            item["quantity"] += amount
        if not page.get("more_items"):
            break
        cursor = str(page.get("last_assetid", ""))
        if not cursor or cursor in cursors:
            raise TrackerError("Steam envanter sayfalaması tamamlanamadı.")
        cursors.add(cursor)
        params["start_assetid"] = cursor
    unavailable = max(0, expected - len(seen_assets)) if expected is not None else 0
    if expected is not None and len(seen_assets) > expected:
        raise TrackerError("Steam envanter sayıları tutarsız; önceki veriler korunuyor.")
    if metadata is not None:
        metadata.update(reported_assets=expected, returned_assets=len(seen_assets), unavailable_assets=unavailable)
    if unavailable:
        LOG.warning("Steam %s item bildirdi ancak tamamlanan sayfalarda %s item gösterdi. %s itemın ayrıntıları erişilemiyor; toplam kısmi olacak.", expected, len(seen_assets), unavailable)
    return sorted(grouped.values(), key=lambda item: item["market_hash_name"])


def parse_usd_price(value):
    # currency=1 + country=US + language=english. Başka para birimini USD sanma.
    match = re.fullmatch(r"(?:US)?\$\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(?:\s*USD)?", value.strip())
    if not match:
        raise TrackerError("Steam beklenen USD fiyat biçimini döndürmedi.")
    return Decimal(match.group(1).replace(",", ""))


def fetch_market_price(client, market_hash_name):
    """Değiştirilebilir fiyat kaynağı: en düşük güncel ilan fiyatı, USD.

    Median satış fiyatı ayrı bir ölçüdür; lowest_price yoksa onunla doldurulmaz.
    """
    result = client.json("GET", "https://steamcommunity.com/market/priceoverview/", service="Steam",
                         params={"appid": 730, "currency": 1, "country": "US", "l": "english",
                                 "market_hash_name": market_hash_name})
    if not result.get("success") or not result.get("lowest_price"):
        raise TrackerError("Steam güncel satış ilanı fiyatı göndermedi.")
    return parse_usd_price(result["lowest_price"])


def fetch_bulk_market_prices(client, inventory):
    """Steam'in tek tek fiyat sorgularındaki IP sınırına takılmamak için toplu veri al."""
    data = client.json("GET", PRICE_URL, service="CSROI")
    prices = {}
    for item in inventory:
        name = item["market_hash_name"]
        steam_data = data.get(name, {}).get("steam", {})
        value = steam_data.get("last_24h") if isinstance(steam_data, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            price = Decimal(str(value))
            if price.is_finite() and price >= 0:
                prices[name] = price
    return prices


def fetch_exchange_rate(client, now):
    response = client.request("GET", "https://www.tcmb.gov.tr/kurlar/today.xml", service="TCMB")
    try:
        root = ElementTree.fromstring(response.content)
        date = datetime.strptime(root.attrib["Date"], "%m/%d/%Y").date()
        rate = Decimal(root.findtext("./Currency[@Kod='USD']/ForexSelling"))
        age = (now.astimezone(TR_TIME).date() - date).days
        if not rate.is_finite() or rate <= 0 or not 0 <= age <= 10:
            raise ValueError("Geçersiz/eski kur")
    except (ElementTree.ParseError, KeyError, ValueError, TypeError, ArithmeticError):
        raise TrackerError("TCMB güncel USD/TRY kuru okunamadı; yanlış TL değerleri kaydedilmedi.") from None
    return {"source": "TCMB döviz satış", "date": date.isoformat(), "usd_try": float(rate)}


def money(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def percent(new, old):
    return round((new - old) / old * 100, 2) if old is not None and old > 0 else None


def alarm(change):
    if change is None:
        return None
    if change >= ALARM_UP:
        return "🔥 Fiyat alarmı"
    if change <= ALARM_DOWN:
        return "🚨 Büyük düşüş"
    if change >= NOTICE_UP:
        return "📈 Yükseliş"
    if change <= NOTICE_DOWN:
        return "📉 Düşüş"
    return None


def build_snapshot(inventory, prices, fx, previous, now, inventory_metadata=None,
                   price_source=PRICE_SOURCE):
    same_price_source = previous is not None and previous.get("price_source") == price_source
    old_items = {item["market_hash_name"]: item for item in (previous or {}).get("items", [])}
    items = []
    for entry in inventory:
        name = entry["market_hash_name"]
        usd = prices.get(name)
        current = money(usd * Decimal(str(fx["usd_try"]))) if usd is not None else None
        old = old_items.get(name, {}).get("current_price") if (previous or {}).get("price_source") == price_source else None
        change = percent(current, old) if current is not None else None
        items.append({**entry, "price_usd": float(usd) if usd is not None else None,
                      "previous_price": old, "current_price": current,
                      "change_percent": change, "alarm": alarm(change),
                      "total_value": money(Decimal(str(current)) * entry["quantity"]) if current is not None else None})
    missing = sum(item["current_price"] is None for item in items)
    total = money(sum((Decimal(str(item["total_value"])) for item in items if item["total_value"] is not None), Decimal(0)))
    unavailable = (inventory_metadata or {}).get("unavailable_assets", 0)
    complete = missing == 0 and unavailable == 0
    comparable = same_price_source and previous.get("complete") is True and complete
    old_total = previous.get("total_value") if same_price_source else None
    difference = money(Decimal(str(total)) - Decimal(str(old_total))) if comparable else None
    elapsed = (now - datetime.fromisoformat(previous["checked_at"])).total_seconds() / 3600 if previous else None
    return {"schema_version": 1, "status": "ok" if complete else "partial", "currency": "TRY",
            "checked_at": now.isoformat(), "previous_checked_at": previous["checked_at"] if same_price_source else None,
            "price_source": price_source,
            "interval_hours": round(elapsed, 2) if same_price_source and elapsed is not None else None,
            "fx": fx, "total_value": total, "previous_total_value": old_total,
            "change_value": difference, "change_percent": percent(total, old_total) if comparable else None,
            "complete": complete, "missing_prices": missing, "unavailable_assets": unavailable,
            "inventory_counts": inventory_metadata or {}, "unique_items": len(items),
            "quantity": sum(item["quantity"] for item in items),
            "inventory_changed": previous is not None and {x["market_hash_name"]: x["quantity"] for x in items}
                                 != {key: value["quantity"] for key, value in old_items.items()},
            "items": items}


def read_history(now):
    try:
        history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        if not isinstance(history, list):
            raise ValueError()
        cutoff = now - timedelta(days=HISTORY_DAYS)
        kept = []
        for snapshot in history:
            checked = datetime.fromisoformat(snapshot["checked_at"])
            if checked.tzinfo is None or checked > now:
                raise ValueError()
            if checked < cutoff:
                continue
            if snapshot.get("schema_version") != 1 or not isinstance(snapshot["items"], list):
                raise ValueError()
            for item in snapshot["items"]:
                if not isinstance(item["market_hash_name"], str) or item["quantity"] < 1:
                    raise ValueError()
                price = item["current_price"]
                if price is not None and (not isinstance(price, (float, int)) or not Decimal(str(price)).is_finite() or price < 0):
                    raise ValueError()
            kept.append(snapshot)
        return sorted(kept, key=lambda row: row["checked_at"])
    except FileNotFoundError:
        return []
    except (ValueError, TypeError, KeyError, ArithmeticError):
        raise TrackerError("data/history.json bozuk veya uyumsuz. Dosya üzerine yazılmadı; son sağlam sürümü geri yükleyin.") from None


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def tl(value):
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".") + " TL"


def make_report(snapshot):
    lines = ["📊 CS2 MARKET RAPORU", "", "Fiyat kaynağı: " + snapshot.get("price_source", "Steam Community Market"),
             "💰 " + ("Fiyatı alınabilenlerin değeri" if not snapshot["complete"] else "Envanter Değeri"),
             tl(snapshot["total_value"])]
    if snapshot["change_value"] is not None:
        lines.extend(["", f"⏱ Önceki kontrol → şimdi ({snapshot['interval_hours']:g} saat)",
                      ("+" if snapshot["change_value"] > 0 else "") + tl(snapshot["change_value"]),
                      f"{snapshot['change_percent']:+.2f}%" if snapshot["change_percent"] is not None else "Önceki toplam sıfır; yüzde hesaplanamaz."])
    else:
        lines.extend(["", "İlk ölçüm veya eksik veri nedeniyle toplam değişim hesaplanamadı."])
    changed = [x for x in snapshot["items"] if x["change_percent"] is not None]
    for title, rows in [("🔥 En Çok Yükselenler", sorted([x for x in changed if x["change_percent"] > 0], key=lambda x: -x["change_percent"])[:5]),
                        ("🔻 En Çok Düşenler", sorted([x for x in changed if x["change_percent"] < 0], key=lambda x: x["change_percent"])[:5])]:
        lines.extend(["", title])
        if not rows:
            lines.append("Yok.")
        for index, item in enumerate(rows, 1):
            name = " ".join(item["display_name"].split())[:100]
            lines.extend([f"{index}. {name}", f"{tl(item['previous_price'])} → {tl(item['current_price'])}",
                          f"{item['change_percent']:+.2f}% · Adet: {item['quantity']}" + (f" · {item['alarm']}" if item["alarm"] else "")])
    significant = [x for x in changed if x["change_percent"] >= ALARM_UP or x["change_percent"] <= ALARM_DOWN]
    lines.extend(["", f"Önemli hareket: {len(significant)} item (eşikler {ALARM_UP:+}% / {ALARM_DOWN:+}%).",
                  f"📦 {snapshot['unique_items']} farklı item · {snapshot['quantity']} adet."])
    if snapshot["missing_prices"]:
        lines.append(f"⚠️ {snapshot['missing_prices']} itemın fiyatı alınamadı; toplam eksik, bu itemlar sıfır sayılmadı.")
    if snapshot.get("unavailable_assets"):
        lines.append(f"⚠️ Steam {snapshot['unavailable_assets']} itemın ayrıntılarını göstermedi. Değer yalnızca erişilebilen itemları kapsar; toplam değişim hesaplanmadı.")
    if snapshot["inventory_changed"]:
        lines.append("📦 Envanter içeriği/adetleri değişti; toplam fark yalnızca fiyat hareketi değildir.")
    lines.extend([f"USD → TL: TCMB {snapshot['fx']['date']} · {snapshot['fx']['usd_try']:g}",
                  "TL değişimi döviz kuru etkisini de içerir. Son 24 saat fiyatı gösterge niteliğindedir; anlık ilan veya net satış tutarı değildir.",
                  "🕒 Son kontrol: " + datetime.fromisoformat(snapshot["checked_at"]).astimezone(TR_TIME).strftime("%d.%m.%Y %H:%M")])
    # Normal rapor kısa tutulur; ilk 5 listelerine girmeyen önemli hareketler de kaybolmaz.
    top_names = {x["market_hash_name"] for x in sorted([x for x in changed if x["change_percent"] > 0], key=lambda x: -x["change_percent"])[:5]}
    top_names |= {x["market_hash_name"] for x in sorted([x for x in changed if x["change_percent"] < 0], key=lambda x: x["change_percent"])[:5]}
    extra = [x for x in significant if x["market_hash_name"] not in top_names]
    if extra:
        lines.extend(["", "⚡ Diğer önemli hareketler"])
        lines.extend(f"{' '.join(x['display_name'].split())[:100]}: {x['change_percent']:+.2f}%" for x in extra)
    return "\n".join(lines)


def send_telegram(client, token, chat_id, message):
    # Emoji UTF-16'da iki birim olabilir; 3.500 birimlik parçalar Telegram sınırının altında kalır.
    chunks, chunk = [], ""
    for line in message.splitlines(keepends=True):
        if len((chunk + line).encode("utf-16-le")) // 2 > 3500:
            chunks.append(chunk)
            chunk = ""
        chunk += line
    if chunk:
        chunks.append(chunk)
    for index, text in enumerate(chunks):
        if index:
            time.sleep(1)
        result = client.json("POST", f"https://api.telegram.org/bot{token}/sendMessage", service="Telegram",
                             json={"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}})
        if not result.get("ok"):
            raise TrackerError("Telegram mesajı kabul etmedi. Bot tokenı, chat ID ve /start mesajını kontrol edin.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-inventory", action="store_true", help="Yalnızca gerçek envanteri kontrol et; JSON yazma ve mesaj gönderme.")
    parser.add_argument("--check-price", metavar="MARKET_HASH_NAME", help="Tek item için gerçek USD fiyatını ve TCMB dönüşümünü kontrol et.")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    client = HttpClient()
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()
    try:
        now = datetime.now(timezone.utc)
        if args.check_price:
            usd = fetch_market_price(client, args.check_price)
            fx = fetch_exchange_rate(client, now)
            LOG.info("Gerçek Steam ilan fiyatı: %s USD · %s · Kur tarihi: %s", usd, tl(money(usd * Decimal(str(fx["usd_try"])))), fx["date"])
            return 0
        steam_id = os.getenv("STEAM_ID", "").strip()
        if not steam_id:
            raise TrackerError("STEAM_ID eksik. GitHub Secrets veya .env dosyasına SteamID64 girin.")
        if not args.check_inventory and (not token or not chat_id):
            raise TrackerError("TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID zorunlu.")
        history = read_history(now) if not args.check_inventory else []
        inventory_metadata = {}
        api_key = os.getenv("STEAM_API_KEY", "").strip()
        try:
            inventory = fetch_inventory(client, steam_id, metadata=inventory_metadata, api_key=api_key or None)
        except TrackerError as error:
            if not api_key:
                raise
            LOG.warning("Steam Web API envanteri alınamadı (%s); steamcommunity.com deneniyor.", error)
            inventory_metadata = {}
            inventory = fetch_inventory(client, steam_id, metadata=inventory_metadata)
        LOG.info("Gerçek Steam envanteri: %s farklı marketable item, %s adet.", len(inventory), sum(x["quantity"] for x in inventory))
        if args.check_inventory:
            return 0
        fx = fetch_exchange_rate(client, now)
        LOG.info("Fiyatlar tek toplu istekte %s kaynağından alınıyor.", PRICE_SOURCE)
        prices = fetch_bulk_market_prices(client, inventory)
        if inventory and not prices:
            raise TrackerError("Toplu fiyat kaynağında envanter itemları bulunamadı; önceki JSON dosyaları korunuyor.")
        now = datetime.now(timezone.utc)
        history = [row for row in history if datetime.fromisoformat(row["checked_at"]) >= now - timedelta(days=HISTORY_DAYS)]
        previous = history[-1] if history else None
        snapshot = build_snapshot(inventory, prices, fx, previous, now, inventory_metadata)
        write_json(HISTORY_PATH, [*history, snapshot])
        write_json(LATEST_PATH, snapshot)
        LOG.info("JSON güncellendi. %s; fiyatı alınamayan: %s.", tl(snapshot["total_value"]), snapshot["missing_prices"])
        send_telegram(client, token, chat_id, make_report(snapshot))
        LOG.info("Telegram raporu gönderildi.")
        return 0
    except (TrackerError, OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
        safe_message = str(error) if isinstance(error, TrackerError) else "Veri işlenemedi veya dosyaya yazılamadı; dosya izinlerini ve veri biçimini kontrol edin."
        LOG.error("%s", safe_message)
        if token and chat_id and not args.check_inventory and not args.check_price:
            try:
                send_telegram(client, token, chat_id, "⚠️ CS2 takip kontrolü tamamlanamadı.\n" + safe_message + "\nSon başarılı veri tarihi dashboard'da gösterilir.")
            except TrackerError:
                LOG.error("Telegram hata bildirimi de gönderilemedi.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
