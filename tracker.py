"""Tek hesap için CS2 envanter takibi. Python 3.11+; veriler yalnızca JSON."""

import argparse
import bisect
import json
import logging
import os
import re
import statistics
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
PRICE_SOURCE = "CSROI.com · Steam son 24 saat fiyatı"  # Eski kayıtlarla karşılaştırma anahtarı; değiştirme.
CSGOTRADER_URL = "https://prices.csgotrader.app/latest/steam.json"
STEAM_PRICE_URL = "https://steamcommunity.com/market/priceoverview/"
# Öncelik (collect_prices): ≤12 saatlik doğrudan Steam → CSROI → bu çalıştırmada Steam (GitHub'da genelde 429)
# → ≤7 günlük Steam → Steam'e ayarlı Skinport → csgotrader. 2026-10-02 ölçümü (16 item, Steam en düşük ilana göre):
# CSROI çoğunlukla ±%5 (en kötü %15); ham Skinport ~%14 düşük, ayarlı Skinport ±%8; csgotrader %15–50 sapıyor.
PRICE_BASES = {  # anahtar: (etiket, tahmini mi)
    "steam": ("Steam Market (doğrudan)", False),
    "csroi": ("CSROI · Steam son 24 saat", False),
    "steam_last": ("Son bilinen Steam fiyatı", True),
    "skinport": ("Skinport (Steam'e ayarlı)", True),
    "csgotrader": ("csgotrader Steam ortalaması", True),
}
# Skinport/Steam oranı fiyat seviyesine göre değişir (ucuzlarda ~0,75, diğerlerinde ~0,84); bant başına medyan.
SKINPORT_BANDS = (0.15, 0.5, 2, 10, 50)
SKINPORT_MIN_SAMPLES = 200
STEAM_MIN_PRICE = Decimal("0.03")  # Steam'de en düşük ilan fiyatı.
CSGOTRADER_PERIODS = {"last_24h": "24 saat", "last_7d": "7 gün", "last_30d": "30 gün", "last_90d": "90 gün"}
STEAM_PRICE_FRESH_HOURS = 12
STEAM_PRICE_MAX_AGE_DAYS = 7
STEAM_PRICE_BUDGET_SECONDS = 300  # İş akışı adımının 8 dk sınırına sığsın.
PRICE_URL = "https://csroi.com/pricing.json"
WEB_API_INVENTORY_URL = "https://api.steampowered.com/IEconService/GetInventoryItemsWithDescriptions/v1/"
ROOT = Path(__file__).resolve().parent
HISTORY_PATH = ROOT / "data/history.json"
STEAM_PRICES_PATH = ROOT / "data/steam_prices.json"
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
        self.steam_delay = REQUEST_DELAY

    def request(self, method, url, *, service, attempts=None, **kwargs):
        max_attempts = attempts or (STEAM_MAX_ATTEMPTS if service == "Steam" else MAX_ATTEMPTS)
        endpoint = safe_endpoint(url)
        for attempt in range(max_attempts):
            if service == "Steam":
                time.sleep(max(0, self.steam_delay - (time.monotonic() - self.last_steam_request)))
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
                        raise SteamRateLimitError(f"Steam istek sınırı ({reason}, {endpoint}). Bu bağlantının IP'si geçici olarak sınırlanmış olabilir; sonraki çalıştırmada yeniden denenecek.")
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


def load_inventory(client, steam_id, api_key):
    """Önce Steam Web API (key varsa), olmazsa steamcommunity.com envanteri."""
    metadata = {}
    if api_key:
        try:
            return fetch_inventory(client, steam_id, metadata=metadata, api_key=api_key), metadata
        except TrackerError as error:
            LOG.warning("Steam Web API envanteri alınamadı (%s); steamcommunity.com deneniyor.", error)
            metadata = {}
    return fetch_inventory(client, steam_id, metadata=metadata), metadata


def cached_inventory(snapshot):
    """Steam envanteri vermezse son başarılı kayıttaki item listesi; fiyatlar yine güncel alınır."""
    keys = ("market_hash_name", "display_name", "quantity", "image_url")
    return [{key: item.get(key) for key in keys} for item in snapshot["items"]], snapshot.get("inventory_counts") or {}


def parse_usd_price(value):
    # currency=1 + country=US + language=english. Başka para birimini USD sanma.
    match = re.fullmatch(r"(?:US)?\$\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(?:\s*USD)?", value.strip())
    if not match:
        raise TrackerError("Steam beklenen USD fiyat biçimini döndürmedi.")
    return Decimal(match.group(1).replace(",", ""))


def fetch_market_price(client, market_hash_name):
    """Steam'in kendi fiyatı: en düşük güncel ilan ("Starting at"), USD. Medyan ve hacim bilgi içindir.

    Median satış fiyatı ayrı bir ölçüdür; lowest_price yoksa onunla doldurulmaz.
    Tek deneme: 429 sırasında yeniden denemek sınırı uzatır; çağıran taraf durur.
    """
    result = client.json("GET", STEAM_PRICE_URL, service="Steam", attempts=1,
                         params={"appid": 730, "currency": 1, "country": "US", "l": "english",
                                 "market_hash_name": market_hash_name})
    if not result.get("success") or not result.get("lowest_price"):
        raise TrackerError("Steam güncel satış ilanı fiyatı göndermedi.")
    volume = re.sub(r"\D", "", str(result.get("volume") or ""))
    return {"usd": parse_usd_price(result["lowest_price"]),
            "median": parse_usd_price(result["median_price"]) if result.get("median_price") else None,
            "volume": int(volume) if volume else None}


def fetch_steam_prices(client, names, budget=STEAM_PRICE_BUDGET_SECONDS):
    """Tek tek Steam sorgusu. IP sınırına takılınca durur; o ana kadar alınanlar döner."""
    found, failures, started = {}, 0, time.monotonic()
    for index, name in enumerate(names):
        if budget is not None and time.monotonic() - started > budget:
            LOG.warning("Steam fiyat sorguları süre sınırına ulaştı (%s/%s item).", index, len(names))
            break
        try:
            found[name] = {**fetch_market_price(client, name), "checked_at": datetime.now(timezone.utc).isoformat()}
            failures = 0
        except SteamRateLimitError as error:
            LOG.warning("Steam fiyat sorguları %s/%s itemda durdu (%s).", index, len(names), error)
            break
        except TrackerError as error:
            LOG.warning("Steam %s için fiyat vermedi (%s).", name, error)
            failures += 1
            if failures >= 3:
                break
    return found


def read_steam_prices():
    """Doğrudan Steam'den alınmış son fiyatlar: {ad: {usd, median, volume, checked_at}}."""
    try:
        data = json.loads(STEAM_PRICES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError:
        LOG.warning("data/steam_prices.json okunamadı; Steam fiyat önbelleği yok sayıldı.")
        return {}
    cache = {}
    for name, entry in data.items() if isinstance(data, dict) else ():
        try:
            if isinstance(entry["usd"], (int, float)) and entry["usd"] > 0 and datetime.fromisoformat(entry["checked_at"]).tzinfo:
                cache[name] = entry
        except (KeyError, TypeError, ValueError):
            pass
    return cache


def store_steam_prices(cache, found, now):
    for name, entry in found.items():
        cache[name] = {"usd": float(entry["usd"]), "median": float(entry["median"]) if entry["median"] is not None else None,
                       "volume": entry["volume"], "checked_at": entry["checked_at"]}
    cutoff = now - timedelta(days=HISTORY_DAYS)
    write_json(STEAM_PRICES_PATH, {name: cache[name] for name in sorted(cache)
                                   if datetime.fromisoformat(cache[name]["checked_at"]) >= cutoff})


def fetch_csgotrader_prices(client, names):
    """Son çare: en yeni dolu dönem (24 saat → 7 → 30 → 90 gün)."""
    data = client.json("GET", CSGOTRADER_URL, service="csgotrader")
    found = {}
    for name in names:
        for field in ("last_24h", "last_7d", "last_30d", "last_90d"):
            price = feed_price(data, name, field)
            if price:
                found[name] = (price, field)
                break
    return found


def collect_prices(client, inventory, now):
    """Her item için en doğru kaynaktan fiyat: {ad: (usd, kaynak, tarih/dönem)}. Sıra PRICE_BASES üstünde."""
    cache = read_steam_prices()

    def cached(name, max_age):
        entry = cache.get(name)
        if entry and now - datetime.fromisoformat(entry["checked_at"]) <= max_age:
            return Decimal(str(entry["usd"])), entry["checked_at"]
        return None

    quotes = {}
    for entry in inventory:
        hit = cached(entry["market_hash_name"], timedelta(hours=STEAM_PRICE_FRESH_HOURS))
        if hit:
            quotes[entry["market_hash_name"]] = (hit[0], "steam", hit[1])
    pending = lambda: [x["market_hash_name"] for x in inventory if x["market_hash_name"] not in quotes]
    skinport = {}
    if pending():
        try:
            names = set(pending())
            csroi = fetch_bulk_market_prices(client, [x for x in inventory if x["market_hash_name"] in names], estimates=skinport)
            quotes.update({name: (usd, "csroi", None) for name, usd in csroi.items()})
        except TrackerError as error:
            LOG.warning("CSROI fiyatları alınamadı (%s); diğer kaynaklar deneniyor.", error)
    if pending():
        LOG.info("%s item için güncel fiyat yok; Steam'e doğrudan soruluyor.", len(pending()))
        found = fetch_steam_prices(client, pending())
        quotes.update({name: (entry["usd"], "steam", entry["checked_at"]) for name, entry in found.items()})
        if found:
            store_steam_prices(cache, found, now)
    for name in pending():
        hit = cached(name, timedelta(days=STEAM_PRICE_MAX_AGE_DAYS))
        if hit:
            quotes[name] = (hit[0], "steam_last", hit[1])
        elif name in skinport:
            quotes[name] = (skinport[name], "skinport", None)
    if pending():
        try:
            quotes.update({name: (usd, "csgotrader", field) for name, (usd, field) in fetch_csgotrader_prices(client, pending()).items()})
        except TrackerError as error:
            LOG.warning("csgotrader fiyatları alınamadı (%s).", error)
    return quotes


def feed_price(entry, market, field):
    data = entry.get(market) if isinstance(entry, dict) else None
    value = data.get(field) if isinstance(data, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        price = Decimal(str(value))
        if price.is_finite() and price >= 0:
            return price
    return None


def skinport_ratios(data):
    """Akıştaki her iki fiyatı da olan itemlardan fiyat bandı başına medyan Skinport/Steam oranı."""
    samples = {}
    for entry in data.values():
        steam, skinport = feed_price(entry, "steam", "last_24h"), feed_price(entry, "skinport", "suggested_price")
        if steam and skinport:
            samples.setdefault(bisect.bisect(SKINPORT_BANDS, skinport), []).append(skinport / steam)
    return {band: statistics.median(values) for band, values in samples.items() if len(values) >= SKINPORT_MIN_SAMPLES}


def fetch_bulk_market_prices(client, inventory, *, estimates=None):
    """Steam'in tek tek fiyat sorgularındaki IP sınırına takılmamak için toplu veri al.

    estimates verilirse Steam fiyatı olmayan itemlar için Steam seviyesine ayarlanmış Skinport fiyatı yazılır.
    """
    data = client.json("GET", PRICE_URL, service="CSROI")
    prices, ratios = {}, None
    for item in inventory:
        name = item["market_hash_name"]
        price = feed_price(data.get(name), "steam", "last_24h")
        if price is not None:
            prices[name] = price
        elif estimates is not None:
            skinport = feed_price(data.get(name), "skinport", "suggested_price")
            if skinport:
                ratios = skinport_ratios(data) if ratios is None else ratios
                ratio = ratios.get(bisect.bisect(SKINPORT_BANDS, skinport))
                if ratio:
                    estimates[name] = max(STEAM_MIN_PRICE, (skinport / ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
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


def price_label(basis, as_of):
    label = PRICE_BASES[basis][0]
    if basis in ("steam", "steam_last") and as_of:
        return f"{label} · {datetime.fromisoformat(as_of).astimezone(TR_TIME):%d.%m %H:%M}"
    if basis == "csgotrader" and as_of:
        return f"{label} · {CSGOTRADER_PERIODS.get(as_of, as_of)}"
    return label


def price_basis(item):
    """Eski kayıtlarda price_basis yok: o zamanlar tek kaynak CSROI idi."""
    return item.get("price_basis") or ("skinport" if item.get("price_estimated") else "csroi")


def build_snapshot(inventory, prices, fx, previous, now, inventory_metadata=None,
                   price_source=PRICE_SOURCE):
    """prices: {ad: Decimal (CSROI) veya (usd, kaynak, tarih/dönem)}."""
    same_price_source = previous is not None and previous.get("price_source") == price_source
    all_old_items = {item["market_hash_name"]: item for item in (previous or {}).get("items", [])}
    old_items = all_old_items if same_price_source else {}
    items = []
    for entry in inventory:
        name = entry["market_hash_name"]
        quote = prices.get(name)
        usd, basis, as_of = (quote, "csroi", None) if quote is None or isinstance(quote, Decimal) else quote
        if usd is None:
            basis = None
        current = money(usd * Decimal(str(fx["usd_try"]))) if usd is not None else None
        old_item = old_items.get(name)
        # Farklı kaynakların fiyatları birbirine karşılaştırılmaz; kaynak değişimi sahte hareket üretir.
        old = old_item.get("current_price") if old_item and price_basis(old_item) == basis else None
        change = percent(current, old) if current is not None else None
        items.append({**entry, "price_usd": float(usd) if usd is not None else None,
                      "price_basis": basis, "price_label": price_label(basis, as_of) if basis else None,
                      "price_as_of": as_of, "price_estimated": bool(basis and PRICE_BASES[basis][1]),
                      "previous_price": old, "current_price": current,
                      "change_percent": change, "alarm": alarm(change),
                      "total_value": money(Decimal(str(current)) * entry["quantity"]) if current is not None else None})
    missing = sum(item["current_price"] is None for item in items)
    total = money(sum((Decimal(str(item["total_value"])) for item in items if item["total_value"] is not None), Decimal(0)))
    unavailable = (inventory_metadata or {}).get("unavailable_assets", 0)
    complete = missing == 0 and unavailable == 0
    old_total = previous.get("total_value") if same_price_source else None
    # Toplam değişim: iki kontrolde de aynı türden fiyatı olan itemlar (eklenen/çıkan itemlar dahil).
    # Bir tarafta fiyatı olmayan veya Steam ↔ tahmin arasında geçen itemlar iki toplamdan da çıkarılır.
    new_items = {item["market_hash_name"]: item for item in items}
    new_sum = old_sum = Decimal(0)
    excluded = 0
    for name in new_items.keys() | old_items.keys():
        new, old = new_items.get(name), old_items.get(name)
        if (new is not None and new["total_value"] is None) or (old is not None and old.get("total_value") is None) \
                or (new is not None and old is not None and new["price_basis"] != price_basis(old)):
            excluded += 1
            continue
        new_sum += Decimal(str(new["total_value"])) if new else 0
        old_sum += Decimal(str(old["total_value"])) if old else 0
    comparable = same_price_source and bool(new_sum or old_sum)
    difference = money(new_sum - old_sum) if comparable else None
    elapsed = (now - datetime.fromisoformat(previous["checked_at"])).total_seconds() / 3600 if previous else None
    bases = {basis: sum(item["price_basis"] == basis for item in items)
             for basis in PRICE_BASES if any(item["price_basis"] == basis for item in items)}
    return {"schema_version": 1, "status": "ok" if complete else "partial", "currency": "TRY",
            "checked_at": now.isoformat(), "previous_checked_at": previous["checked_at"] if same_price_source else None,
            "price_source": price_source,
            "interval_hours": round(elapsed, 2) if same_price_source and elapsed is not None else None,
            "fx": fx, "total_value": total, "previous_total_value": old_total,
            "change_value": difference, "change_percent": percent(float(new_sum), float(old_sum)) if comparable else None,
            "change_excluded_items": excluded if comparable else None,
            "complete": complete, "missing_prices": missing, "unavailable_assets": unavailable,
            "estimated_items": sum(item["price_estimated"] for item in items),
            "estimated_value": money(sum((Decimal(str(item["total_value"])) for item in items if item["price_estimated"]), Decimal(0))),
            "price_bases": bases,
            "price_summary": " · ".join(f"{PRICE_BASES[basis][0]} {count}" for basis, count in bases.items()),
            "inventory_counts": inventory_metadata or {}, "unique_items": len(items),
            "quantity": sum(item["quantity"] for item in items),
            "inventory_changed": previous is not None and {x["market_hash_name"]: x["quantity"] for x in items}
                                 != {key: value["quantity"] for key, value in all_old_items.items()},
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
    lines = ["📊 CS2 MARKET RAPORU", "",
             "💰 " + ("Fiyatı alınabilenlerin değeri" if not snapshot["complete"] else "Envanter Değeri"),
             tl(snapshot["total_value"]),
             "Fiyatlar: " + (snapshot.get("price_summary") or snapshot.get("price_source", "Steam Community Market"))]
    steam_times = [datetime.fromisoformat(x["price_as_of"]) for x in snapshot["items"] if x.get("price_basis") == "steam"]
    if steam_times:
        lines.append(f"🎯 Doğrudan Steam fiyatları: {min(steam_times).astimezone(TR_TIME):%d.%m %H:%M} – "
                     f"{max(steam_times).astimezone(TR_TIME):%d.%m %H:%M} arası (en düşük ilan)")
    if snapshot.get("estimated_items"):
        lines.append(f"≈ {tl(snapshot['estimated_value'])} kısmı tahmini ({snapshot['estimated_items']} item)")
    if snapshot["change_value"] is not None:
        lines.extend(["", f"⏱ Önceki kontrol → şimdi ({snapshot['interval_hours']:g} saat)",
                      ("+" if snapshot["change_value"] > 0 else "") + tl(snapshot["change_value"]),
                      f"{snapshot['change_percent']:+.2f}%" if snapshot["change_percent"] is not None else "Önceki toplam sıfır; yüzde hesaplanamaz."])
        if snapshot.get("change_excluded_items"):
            lines.append(f"({snapshot['change_excluded_items']} item iki kontrolde aynı kaynaktan fiyatlanamadığı için hariç)")
    else:
        lines.extend(["", "Önceki kontrolle aynı kaynaktan fiyatlanan item olmadığı için toplam değişim hesaplanamadı."
                      if snapshot.get("previous_checked_at") else "İlk ölçüm; toplam değişim sonraki kontrolde hesaplanır."])
    changed = [x for x in snapshot["items"] if x["change_percent"] is not None]
    for title, rows in [("🔥 En Çok Yükselenler", sorted([x for x in changed if x["change_percent"] > 0], key=lambda x: -x["change_percent"])[:5]),
                        ("🔻 En Çok Düşenler", sorted([x for x in changed if x["change_percent"] < 0], key=lambda x: x["change_percent"])[:5])]:
        lines.extend(["", title])
        if not rows:
            lines.append("Yok.")
        for index, item in enumerate(rows, 1):
            name = " ".join(item["display_name"].split())[:100] + (" (tahmini)" if item.get("price_estimated") else "")
            lines.extend([f"{index}. {name}", f"{tl(item['previous_price'])} → {tl(item['current_price'])}",
                          f"{item['change_percent']:+.2f}% · Adet: {item['quantity']}" + (f" · {item['alarm']}" if item["alarm"] else "")])
    significant = [x for x in changed if x["change_percent"] >= ALARM_UP or x["change_percent"] <= ALARM_DOWN]
    lines.extend(["", f"Önemli hareket: {len(significant)} item (eşikler {ALARM_UP:+}% / {ALARM_DOWN:+}%).",
                  f"📦 {snapshot['unique_items']} farklı item · {snapshot['quantity']} adet."])
    if snapshot["missing_prices"]:
        lines.append(f"⚠️ {snapshot['missing_prices']} itemın fiyatı hiçbir kaynakta yok; toplam eksik, bu itemlar sıfır sayılmadı.")
    if snapshot.get("unavailable_assets"):
        lines.append(f"⚠️ Steam {snapshot['unavailable_assets']} itemın ayrıntılarını göstermedi. Değer yalnızca erişilebilen itemları kapsar.")
    if snapshot.get("inventory_source") == "cache":
        lines.append("⚠️ Steam bu kontrolde envanteri vermedi; " + datetime.fromisoformat(snapshot["inventory_checked_at"]).astimezone(TR_TIME).strftime("%d.%m.%Y %H:%M")
                     + " tarihli son envanter kullanıldı. Fiyatlar günceldir.")
    if snapshot["inventory_changed"]:
        lines.append("📦 Envanter içeriği/adetleri değişti; toplam fark yalnızca fiyat hareketi değildir.")
    lines.extend([f"USD → TL: TCMB {snapshot['fx']['date']} · {snapshot['fx']['usd_try']:g}",
                  "TL değişimi döviz kuru etkisini de içerir. Fiyatlar alıcının ödediği tutardır (Steam: en düşük ilan, CSROI: son 24 saat satış); satışta Steam kesintisi (~%13) düşülür.",
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
    # Planlı çalıştırma saatlik; son başarılı rapordan bu kadar saat geçmeden iş yapılmaz.
    min_gap = float(os.getenv("MIN_HOURS_BETWEEN_REPORTS") or 0)
    notify = True
    try:
        now = datetime.now(timezone.utc)
        if args.check_price:
            quote = fetch_market_price(client, args.check_price)
            fx = fetch_exchange_rate(client, now)
            LOG.info("Steam en düşük ilan: %s USD · %s · 24 saat medyan: %s · 24 saatte satış: %s · Kur tarihi: %s",
                     quote["usd"], tl(money(quote["usd"] * Decimal(str(fx["usd_try"])))), quote["median"] or "—", quote["volume"] or "—", fx["date"])
            return 0
        steam_id = os.getenv("STEAM_ID", "").strip()
        if not steam_id:
            raise TrackerError("STEAM_ID eksik. GitHub Secrets veya .env dosyasına SteamID64 girin.")
        if not args.check_inventory and (not token or not chat_id):
            raise TrackerError("TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID zorunlu.")
        history = read_history(now) if not args.check_inventory else []
        if history and now - datetime.fromisoformat(history[-1]["checked_at"]) < timedelta(hours=min_gap):
            LOG.info("Son başarılı kontrolün üzerinden %s saat geçmedi; bu planlı çalıştırma atlandı.", f"{min_gap:g}")
            return 0
        inventory_source, inventory_checked_at = "steam", None
        try:
            inventory, inventory_metadata = load_inventory(client, steam_id, os.getenv("STEAM_API_KEY", "").strip() or None)
        except TrackerError as error:
            if args.check_inventory or not history:
                # İlk envanter alınana kadar saatlik denemeler Telegram'ı hata mesajıyla doldurmasın.
                notify = min_gap == 0
                raise
            LOG.warning("Steam envanteri alınamadı (%s); son bilinen envanter kullanılıyor.", error)
            inventory, inventory_metadata = cached_inventory(history[-1])
            inventory_source = "cache"
            inventory_checked_at = history[-1].get("inventory_checked_at") or history[-1]["checked_at"]
        LOG.info("%s envanter: %s farklı marketable item, %s adet.", "Gerçek Steam" if inventory_source == "steam" else "Son bilinen",
                 len(inventory), sum(x["quantity"] for x in inventory))
        if args.check_inventory:
            return 0
        fx = fetch_exchange_rate(client, now)
        LOG.info("Fiyatlar tek toplu istekte %s kaynağından alınıyor.", PRICE_SOURCE)
        now = datetime.now(timezone.utc)
        prices = collect_prices(client, inventory, now)
        if inventory and not prices:
            raise TrackerError("Toplu fiyat kaynağında envanter itemları bulunamadı; önceki JSON dosyaları korunuyor.")
        now = datetime.now(timezone.utc)
        history = [row for row in history if datetime.fromisoformat(row["checked_at"]) >= now - timedelta(days=HISTORY_DAYS)]
        previous = history[-1] if history else None
        snapshot = build_snapshot(inventory, prices, fx, previous, now, inventory_metadata)
        snapshot.update(inventory_source=inventory_source, inventory_checked_at=inventory_checked_at or snapshot["checked_at"])
        write_json(HISTORY_PATH, [*history, snapshot])
        write_json(LATEST_PATH, snapshot)
        LOG.info("JSON güncellendi. %s; tahmini fiyatlı: %s; fiyatı alınamayan: %s.", tl(snapshot["total_value"]),
                 snapshot["estimated_items"], snapshot["missing_prices"])
        send_telegram(client, token, chat_id, make_report(snapshot))
        LOG.info("Telegram raporu gönderildi.")
        return 0
    except (TrackerError, OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
        safe_message = str(error) if isinstance(error, TrackerError) else "Veri işlenemedi veya dosyaya yazılamadı; dosya izinlerini ve veri biçimini kontrol edin."
        LOG.error("%s", safe_message)
        if notify and token and chat_id and not args.check_inventory and not args.check_price:
            try:
                send_telegram(client, token, chat_id, "⚠️ CS2 takip kontrolü tamamlanamadı.\n" + safe_message + "\nSon başarılı veri tarihi dashboard'da gösterilir.")
            except TrackerError:
                LOG.error("Telegram hata bildirimi de gönderilemedi.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
