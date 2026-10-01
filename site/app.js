"use strict";

const $ = (id) => document.getElementById(id);
const currency = new Intl.NumberFormat("tr-TR", { style: "currency", currency: "TRY" });
const number = new Intl.NumberFormat("tr-TR", { maximumFractionDigits: 2 });
let snapshot = null;
let dataUrl = "./data/latest.json";
const money = (value) => value == null ? "—" : currency.format(value);
const percent = (value) => value == null ? "—" : `${value > 0 ? "+" : ""}${number.format(value)}%`;
const direction = (value) => value > 0 ? "positive" : value < 0 ? "negative" : "neutral";

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function itemImage(item) {
  const placeholder = () => element("span", "item-image item-placeholder", "◇");
  try {
    const url = new URL(item.image_url);
    if (url.protocol !== "https:" || !["community.cloudflare.steamstatic.com", "community.akamai.steamstatic.com"].includes(url.hostname)) return placeholder();
    const img = element("img", "item-image");
    img.src = url.href;
    img.alt = "";
    img.loading = "lazy";
    img.addEventListener("error", () => img.replaceWith(placeholder()), { once: true });
    return img;
  } catch { return placeholder(); }
}

function showNotice(text, error = false) {
  $("notice").textContent = text;
  $("notice").hidden = !text;
  $("notice").classList.toggle("error", error);
}

function renderMovers(id, items) {
  const target = $(id);
  target.replaceChildren();
  if (!items.length) {
    target.append(element("p", "empty-small", "Karşılaştırılabilir fiyat hareketi yok."));
    return;
  }
  for (const item of items) {
    const row = element("div", "mover");
    const detail = element("div", "mover-name");
    detail.append(element("strong", "", item.display_name), element("small", "", `${money(item.previous_price)} → ${money(item.current_price)} · ${item.quantity} adet`));
    const badge = element("span", `badge ${direction(item.change_percent)}`, percent(item.change_percent));
    if (item.alarm) badge.title = item.alarm;
    row.append(itemImage(item), detail, badge);
    target.append(row);
  }
}

function renderInventory() {
  if (!snapshot) return;
  const query = $("search").value.trim().toLocaleLowerCase("tr-TR");
  const key = $("sort").value;
  const items = snapshot.items.filter((item) => item.display_name.toLocaleLowerCase("tr-TR").includes(query));
  items.sort((a, b) => key === "display_name" ? a.display_name.localeCompare(b.display_name, "tr") : (b[key] ?? -Infinity) - (a[key] ?? -Infinity));
  const body = $("inventory");
  body.replaceChildren();
  for (const item of items) {
    const row = element("tr");
    const nameCell = element("td");
    const name = element("div", "item-cell");
    const link = element("a", "", item.display_name);
    link.href = `https://steamcommunity.com/market/listings/730/${encodeURIComponent(item.market_hash_name)}`;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    if (item.current_price == null) link.append(element("small", "", "Fiyat alınamadı"));
    name.append(itemImage(item), link);
    nameCell.append(name);
    const changeCell = element("td");
    const badge = element("span", `badge ${direction(item.change_percent)}`, percent(item.change_percent));
    badge.title = item.alarm || (item.change_percent == null ? "Karşılaştırılabilir önceki fiyat yok" : "Önceki kontrole göre");
    changeCell.append(badge);
    row.append(nameCell, element("td", "", number.format(item.quantity)), element("td", "", money(item.current_price)), changeCell, element("td", "", money(item.total_value)));
    body.append(row);
  }
  if (!items.length) {
    const row = element("tr");
    const cell = element("td", "empty-table", query ? "Aramanla eşleşen item yok." : "Bu envanterde marketable item bulunmuyor.");
    cell.colSpan = 5;
    row.append(cell);
    body.append(row);
  }
  $("result-count").textContent = `${items.length} / ${snapshot.items.length} item`;
}

function render(data) {
  if (data.status === "not_configured") {
    showNotice("İlk kontrol bekleniyor. GitHub Secrets bilgilerini ekleyip Actions → Run workflow ile takibi başlat.");
    return;
  }
  if (data.schema_version !== 1 || !["ok", "partial"].includes(data.status) || !Array.isArray(data.items) || !Number.isFinite(Date.parse(data.checked_at)) || !Number.isFinite(data.total_value)) {
    throw new Error("Veri dosyasının biçimi geçersiz.");
  }
  snapshot = data;
  $("total-label").textContent = data.complete ? "TOPLAM ENVANTER DEĞERİ" : "FİYATI ALINABİLENLERİN DEĞERİ";
  $("total").textContent = money(data.total_value);
  $("total-note").textContent = data.complete ? "Steam Market · Güncel TL karşılığı" : "Yalnızca erişilen ve fiyatı alınabilen itemlar";
  $("change").textContent = percent(data.change_percent);
  $("change").className = `metric-value ${direction(data.change_percent)}`;
  $("change-note").textContent = data.change_value != null ? `${data.change_value > 0 ? "+" : ""}${money(data.change_value)} · ${number.format(data.interval_hours)} saat önceye göre` : data.previous_checked_at ? "Eksik veri nedeniyle karşılaştırılamıyor." : "İlk ölçüm. Karşılaştırma için iki tam ölçüm gerekir.";
  $("item-count").textContent = number.format(data.unique_items);
  $("quantity").textContent = `${number.format(data.quantity)} adet · ${data.missing_prices} eksik fiyat`;
  const checked = new Date(data.checked_at);
  $("last-update").textContent = "Son kontrol: " + new Intl.DateTimeFormat("tr-TR", { dateStyle: "medium", timeStyle: "short", timeZone: "Europe/Istanbul" }).format(checked) + " · Türkiye saati";
  $("fx").textContent = `TCMB ${data.fx.date} · 1 USD = ${number.format(data.fx.usd_try)} TL`;
  const comparable = data.items.filter((item) => item.change_percent != null);
  renderMovers("gainers", comparable.filter((item) => item.change_percent > 0).sort((a, b) => b.change_percent - a.change_percent).slice(0, 5));
  renderMovers("losers", comparable.filter((item) => item.change_percent < 0).sort((a, b) => a.change_percent - b.change_percent).slice(0, 5));
  const notices = [];
  const stale = Date.now() - checked.getTime() > 12 * 3600000;
  if (stale) notices.push("Veri 12 saatten eski. Son iş akışının sonucunu GitHub Actions'tan kontrol et.");
  if (data.missing_prices) notices.push(`${data.missing_prices} itemın fiyatı alınamadı. Gösterilen toplam kısmi; toplam değişim hesaplanmadı.`);
  if (data.unavailable_assets) notices.push(`Steam ${data.unavailable_assets} itemın ayrıntılarını göstermedi. Toplam yalnızca erişilebilen itemları kapsar; toplam değişim hesaplanmadı.`);
  if (data.inventory_changed) notices.push("Envanter içeriği veya adetleri değişti. Toplam fark bu değişimi de içerir.");
  showNotice(notices.join(" "), stale);
  renderInventory();
}

async function refresh() {
  $("refresh").disabled = true;
  try {
    const response = await fetch(dataUrl, { cache: "no-store", signal: AbortSignal.timeout(15000) });
    if (!response.ok) throw new Error(`Veri alınamadı (HTTP ${response.status}).`);
    render(await response.json());
  } catch (error) {
    showNotice(`Veriler yüklenemedi. ${error.message} ${snapshot ? "Ekranda önceki veri gösteriliyor." : "Sayfayı bir web sunucusu üzerinden aç ve JSON adresini kontrol et."}`, true);
  } finally {
    $("refresh").disabled = false;
  }
}

async function start() {
  try {
    // Netlify build yalnızca açık veri adresini buraya yazar; sır içermez.
    const config = await fetch("./source.json", { cache: "no-store", signal: AbortSignal.timeout(5000) });
    if (config.ok) {
      const { url } = await config.json();
      if (url) {
        if (!/^https:\/\/raw\.githubusercontent\.com\/[^/]+\/[^/]+\/.+\/site\/data\/latest\.json$/.test(url)) throw new Error("Geçersiz açık veri adresi.");
        dataUrl = url;
      }
    } else if (config.status !== 404) {
      throw new Error("Veri kaynağı ayarı okunamadı.");
    }
  } catch (error) {
    showNotice(`Veri kaynağı okunamadı: ${error.message}. Sayfayı yeniden yükle.`, true);
    return;
  }
  $("refresh").addEventListener("click", refresh);
  $("search").addEventListener("input", renderInventory);
  $("sort").addEventListener("change", renderInventory);
  await refresh();
  setInterval(() => { if (!document.hidden && !$("refresh").disabled) refresh(); }, 5 * 60 * 1000);
}

start();
