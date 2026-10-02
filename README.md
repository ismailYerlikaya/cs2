# CS2 Market Tracker

Tek Steam hesabı için Python + GitHub Actions + Telegram + statik dashboard. Bilgisayarının açık kalması gerekmez. Veriler yalnızca `data/history.json` ve `site/data/latest.json` içinde tutulur; database veya ayrı sunucu yoktur.

**Doğrulama durumu:** Canlı Steam envanteri çekme/gruplama ve TCMB kur servisi doğrulandı. Steam'in GitHub sunucusuna uyguladığı fiyat sınırı nedeniyle fiyatlar, CSROI.com'un ücretsiz ve toplu Steam fiyat JSON'undan alınır. Bu kaynak Steam son 24 saat fiyat göstergesini yaklaşık 6 saatte bir yeniler; anlık en düşük ilan değildir. Tam fiyat taraması ve Telegram rapor teslimatı henüz uçtan uca doğrulanmadı. Yerel testler hesaplama, sayfalama ve hata işleme mantığını sınar; canlı servislerin çalıştığının kanıtı değildir. Başlangıç JSON'ları boş; örnek/fake fiyat bulunmaz. Tam akışı aşağıdaki 7. adımda doğrula.

**TL ve ücretsiz yayın:** Fiyat kaynağındaki USD Steam fiyat göstergesi TCMB'nin son yayımlanan USD döviz satış kuruyla TL'ye çevrilir. Netlify'ın yeni Free planında 300 kredi/ay ve üretim yayını başına 15 kredi vardır; günde üç yayın ücretsiz kotaya sığmaz. 8. adımda verilen açık GitHub JSON bağlantısını kullanırsan her fiyat kontrolünde yeni Netlify yayını yapılmaz. Hosting trafiği ve servislerin ücretsiz kullanım limitleri yine geçerlidir.

## 1. SteamID64 değerini bul

1. Steam'e giriş yap ve [hesap bilgileri sayfanı](https://store.steampowered.com/account/) aç.
2. Hesap başlığının altında görünen **Steam ID** değerini kopyala. `7656119…` ile başlayan **17 haneli sayı** gerekir.
3. Profil bağlantın `steamcommunity.com/profiles/7656119…` biçimindeyse sondaki sayı da aynı değerdir. `/id/takma-ad` biçimindeki özel profil adı uygun değildir.
4. Bu değeri daha sonra `STEAM_ID` isimli Secret'a gireceksin.

## 2. Envanterini Public yap

1. Steam'de profilini aç → **Profili Düzenle / Edit Profile**.
2. **Gizlilik Ayarları / Privacy Settings** bölümüne git.
3. **Profilim / My profile** ve **Envanter / Inventory** alanlarını **Herkese Açık / Public** yap.
4. CS2 envanter bağlantını çıkış yapılmış veya gizli bir tarayıcı penceresinde açarak görünür olduğunu kontrol et.

Yalnızca marketable itemlar takip edilir. Satılamayan itemlar toplam değere dahil edilmez. Sticker, float, pattern gibi özel primler hesaplanmaz; aynı `market_hash_name` tek satırda gruplanır.

## 3. Telegram botunu oluştur

1. Telegram'da resmi [@BotFather](https://t.me/BotFather) hesabını aç.
2. `/newbot` gönder.
3. Botuna bir görünen ad, sonra `bot` ile biten uygun bir kullanıcı adı ver.
4. BotFather'ın verdiği **tokenı** güvenli biçimde sakla. Bu, `TELEGRAM_BOT_TOKEN` olacak.
5. Oluşturduğun botun sohbetini aç, **Start / Başlat** düğmesine bas ve bir mesaj yaz. Bot, sen sohbeti başlatmadan sana özel mesaj gönderemez.

Tokenı kod dosyalarına, ekran görüntülerine veya GitHub commitlerine ekleme. Sızarsa BotFather üzerinden yenile.

## 4. Telegram Chat ID değerini bul

1. Botuna az önce bir mesaj gönderdiğinden emin ol.
2. Tarayıcında aşağıdaki adresi aç; `TOKEN_BURAYA` yerine bot tokenını yaz:

   ```text
   https://api.telegram.org/botTOKEN_BURAYA/getUpdates
   ```

3. Dönen JSON içinde `result` → `message` → `chat` → **`id`** değerini bul. Botun kendi ID'sini değil, sohbetin ID'sini kullan.
4. Sayıyı `TELEGRAM_CHAT_ID` için sakla. Kişisel sohbetlerde genelde pozitiftir; grup ID'si negatif olabilir, eksi işaretini koru.
5. `result: []` görürsen botuna yeni mesaj gönderip sayfayı yenile. Aynı botu başka bir bot uygulamasında kullanma; onun webhook'u veya güncelleme okuyucusu bu adımı engelleyebilir.

Bu adres token içerdiğinden bağlantıyı paylaşma ve işin bitince tarayıcı geçmişindeki kaydı kaldır.

## 5. GitHub repository oluştur ve dosyaları yükle

1. GitHub hesabınla giriş yap → **New repository**.
2. İsim olarak örneğin `cs2-tracker` yaz.
3. 8. adımdaki ücretsiz veri akışı için **Public** seç. Bu seçimde item adları, adetleri ve değerleri herkese açık olur. JSON içinde SteamID64, bot tokenı ve chat ID bulunmaz.
4. Boş repository oluştur; bu klasördeki dosyaları repository köküne gönder. `tracker.py`, `site/` ve `.github/` aynı seviyede olmalı.

Git yüklüyse bu klasörde PowerShell aç ve aşağıdaki komutları sırayla çalıştır. `KULLANICI` ve repository adını kendi değerlerinle değiştir:

```powershell
git init
git add .
git commit -m "Create CS2 tracker"
git branch -M main
git remote add origin https://github.com/KULLANICI/cs2-tracker.git
git push -u origin main
```

İstenirse GitHub girişini tamamla. GitHub Desktop ile de mevcut klasörü repository olarak ekleyip **Publish repository** kullanabilirsin. `.env` dosyası varsa `.gitignore` nedeniyle yüklenmez. GitHub web arayüzünden dosya yüklersen `.github/workflows/tracker.yml` ve `.env.example` dahil noktayla başlayan dosyaların da yüklendiğini kontrol et.

**Private repository alternatifi:** Açık GitHub veri bağlantısı çalışmaz; 8. adımda `TRACKER_DATA_URL` ekleme. Her veri güncellemesinde Netlify yeniden yayın yapar ve plan kotası tüketir. Private GitHub depolarında Actions dakika kotası da vardır. Tarayıcıya erişim tokenı koyma.

## 6. GitHub Secrets değerlerini ekle

Repository → **Settings → Secrets and variables → Actions → New repository secret**.

Her birini ayrı secret olarak ekle; değerleri tırnak içine alma:

| Name | Secret değeri |
| --- | --- |
| `STEAM_ID` | 17 haneli SteamID64 |
| `TELEGRAM_BOT_TOKEN` | BotFather'ın verdiği token |
| `TELEGRAM_CHAT_ID` | 4. adımda bulduğun sohbet ID'si |
| `STEAM_API_KEY` | Önerilir: [steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey) adresinden alınan ücretsiz Steam Web API key'i. Steam, GitHub sunucularına `steamcommunity.com` envanterini sık sık HTTP 429 ile reddeder; key varsa envanter `api.steampowered.com` üzerinden çekilir, olmazsa eski yönteme geçilir. Domain alanına herhangi bir ad (ör. `localhost`) yazılabilir. Key'i kimseyle paylaşma. |

Sonra **Settings → Actions → General → Workflow permissions** bölümünde **Read and write permissions** seçip kaydet. İş akışı yalnızca kendi deposundaki JSON dosyalarını güncellemek için yazma izni ister. Branch protection/ruleset doğrudan push'u engelliyorsa bu kişisel depo için uygun izin verilmeli; workflow güvenlik kurallarını atlatmaz.

## 7. Run workflow ile ilk gerçek testi yap

1. Repository'de **Actions** sekmesine gir. İstenirse Actions'ı etkinleştir.
2. **CS2 Inventory Tracker** iş akışını seç.
3. **Run workflow → main → Run workflow** düğmelerine bas.
4. Çalıştırmayı aç ve adımları takip et. Envanter Steam'den, fiyatlar tek toplu istekte alınır.
5. Başarılı sonuçta:
   - Telegram botundan rapor gelir.
   - `site/data/latest.json` içinde gerçek itemlar ve kontrol zamanı görünür.
   - `data/history.json` içine ölçüm eklenir.
   - Repository'de `chore: update Steam market data` commit'i oluşur.
6. İlk raporda değişim boş olması normaldir. İkinci başarılı ölçümde önceki fiyatlarla kıyaslama başlar. İlkinden hemen sonra sürekli çalıştırmak Steam istek sınırına yol açabilir; sonraki planlı çalıştırmayı bekle.

Plan: İş akışı her saat (:07) uyanır; son başarılı rapordan 7,5 saat geçmediyse birkaç saniyede çıkar. Böylece rapor yaklaşık **8 saatte bir** gelir, başarısız bir deneme bir saat sonra kendiliğinden tekrarlanır. GitHub yoğunluğa bağlı geciktirebilir, bazı zamanlanmış çalıştırmalar atlanabilir. Dashboard ve Telegram bu nedenle sabit “son 8 saat” yerine gerçek ölçüm aralığını gösterir. Schedule varsayılan branch'teki dosyadan çalışır. Public depolarda 60 gün repository etkinliği olmazsa GitHub planlı iş akışını kapatabilir; kapanırsa Actions'tan yeniden etkinleştir.

Hata olduğunda kontrol et:

| Hata / belirti | Yapılacak işlem |
| --- | --- |
| Envanter alınamadı / 403 | SteamID64 ve Public ayarlarını doğrula. Steam'in GitHub sunucusu IP'sine geçici kısıt koyması da mümkündür. Sonra yeniden dene. |
| Steam 429 | Steam GitHub sunucu IP'sini sınırlamıştır. Daha önce en az bir başarılı kayıt varsa son bilinen envanter kullanılır, fiyatlar yine güncellenir ve rapor bunu belirtir; envanter Steam izin verdiğinde kendiliğinden yenilenir. Hiç kayıt yoksa planlı çalıştırmalar saatlik olarak sessizce tekrar dener (Telegram'a hata yalnızca elle çalıştırmada gider). İsteğe bağlı `STEAM_API_KEY` bu engeli tamamen aşabilir. |
| Hiç fiyat alınamadı | Toplu fiyat dosyası veya item adlarını kontrol et. Eski JSON korunur; sıfır değerli rapor kaydedilmez. |
| Bazı itemların CSROI'de Steam fiyatı yok (ör. Sticker Slab) | Sırayla: Steam'e doğrudan sorulur (GitHub'da genelde 429) → `data/steam_prices.json`'daki ≤7 günlük Steam fiyatı → Skinport → csgotrader. Steam dışındakiler "tahmini" etiketlidir. Önbelleği ev bağlantısından tazelemek için: `python tracker.py --refresh-steam-prices` (secret gerekmez). Toplam değişim yalnızca iki kontrolde de aynı kaynaktan fiyatı olan itemlar üzerinden hesaplanır. |
| TCMB hatası | Eski/uydurma kur kullanılmaz. Servis düzeldiğinde tekrar çalıştır. Tatilde son yayımlanan kur kullanılır; 10 günden eski kur kabul edilmez. |
| Telegram 400 / 401 / 403 | Token ve chat ID'yi kontrol et; bot sohbetinde `/start` gönder ve engeli kaldır. Alınmış fiyatlar Telegram hata verse de commit edilir, Actions kırmızı kalır. |
| JSON commit/push reddedildi | Workflow yazma iznini ve branch kurallarını kontrol et. |
| Dashboard 12 saatten eski | Son başarılı ölçüm korunmuştur; Actions'ın son çalıştırmasına bak. |

Fiyatlar [CSROI.com](https://csroi.com/) tarafından sağlanan [toplu JSON](https://csroi.com/pricing.json) içindeki Steam `last_24h` alanıdır. CSROI, bu ücretsiz veri dosyasının kaynak gösterilerek kullanılmasını ister ve yaklaşık 6 saatte bir yenilemeyi dener. Bu nedenle gösterge güncel en düşük satış ilanına eşit olmak zorunda değildir; satış fiyatı veya ele geçecek net tutar garantisi vermez. Envanter verisi yalnızca Steam'den çekilir; fiyat isteğine SteamID, envanter veya Telegram bilgisi gönderilmez.

## 8. Netlify'a bağla

Ücretsiz kotayı koruyan önerilen kurulum:

1. [Netlify](https://app.netlify.com/) hesabı aç/giriş yap.
2. **Add new project → Import an existing project → GitHub** seç (arayüzde “Add new site” olarak da görünebilir).
3. GitHub erişimini ver ve `cs2-tracker` deposunu seç.
4. Production branch: **main**. Base directory: **boş**. Publish directory: **site**. Build command `netlify.toml` dosyasından otomatik alınır; değiştirmen gerekmez.
5. İlk deploy'dan **önce**, proje environment variable alanına şu tek **gizli olmayan** ayarı ekle:

   ```text
   TRACKER_DATA_URL=https://raw.githubusercontent.com/KULLANICI/cs2-tracker/main/site/data/latest.json
   ```

   `KULLANICI`, repository adı ve branch sana ait olmalı. Adresi gizli tarayıcı penceresinde aç; JSON görünmeli. Repository Public olmalı. Bu ayar build sırasında kullanılabilmeli; Netlify scope seçeneği varsa **Builds** kapsamını etkinleştir.

6. **Deploy** düğmesine bas. Sonradan environment variable eklediysen/değiştirdiysen **Deploys → Trigger deploy → Clear cache and deploy site** ile bir kez yeniden yayınla.
7. Siteyi aç. Henüz veri yoksa “İlk kontrol bekleniyor” görünür. 7. adımdaki ilk başarılı çalıştırmadan sonra **Veriyi yenile** düğmesiyle JSON'u tekrar oku. Bu düğme Steam taramasını tetiklemez.
8. GitHub Actions JSON'u güncellediğinde dashboard aynı dosyanın GitHub Raw adresini okur. Netlify yalnızca site kodu değiştiğinde yeni yayın yapar. Sayfa açıkken 5 dakikada bir yeniler; GitHub Raw önbelleği nedeniyle birkaç dakika gecikme olabilir.

**Netlify'a STEAM_ID, TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID ekleme.** Bunlar yalnızca GitHub Secrets'ta kalır. Siteye aktarılan tek ayar açık JSON adresidir. Netlify build bu adresi `site/source.json` olarak üretir; bu üretilen dosya Git'e eklenmez.

Standart seçenek: `TRACKER_DATA_URL` boş bırakılırsa dashboard doğrudan kendi `site/data/latest.json` dosyasını okur. Netlify Git entegrasyonu her JSON commit'inde yeni yayın yapar. Yeni Free planında günde üç üretim yayını ayda yaklaşık 1.350 kredi tüketir; 300 kredilik ücretsiz kotaya uygun değildir. Eski planın farklı limitleri varsa hesap panelinden kontrol et.

## Bilgisayarda isteğe bağlı kontrol

Python 3.11 veya üstü gerekir. Normal kullanım için bilgisayarda çalıştırman gerekmez.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env` dosyasını bir metin düzenleyicide açıp üç değeri doldur. Ardından sırasıyla:

```powershell
# Gerçek Steam envanterini çeker ve gruplanmış sayıyı gösterir; kaydetmez, mesaj göndermez.
.\.venv\Scripts\python.exe tracker.py --check-inventory

# Bir gerçek fiyat isteği ve TCMB dönüşümünü doğrular; kaydetmez, mesaj göndermez.
.\.venv\Scripts\python.exe tracker.py --check-price "Revolution Case"

# Tam kontrol: JSON günceller ve Telegram raporu gönderir.
.\.venv\Scripts\python.exe tracker.py

# Hesaplama/hata senaryosu testleri; dış servislere bağlanmaz.
.\.venv\Scripts\python.exe -m unittest -v

# Yerel site önizlemesi; tarayıcıda http://localhost:8000 adresini aç.
.\.venv\Scripts\python.exe -m http.server 8000 --directory site
```

`index.html` dosyasına çift tıklayarak açma; tarayıcı JSON okumayı engelleyebilir. Yerel önizlemede `source.json` yoksa otomatik olarak yerel `data/latest.json` okunur.

## Küçük ayarlar ve davranışlar

- Alarm eşikleri `tracker.py` başında: `ALARM_UP = 10`, `NOTICE_UP = 5`, `ALARM_DOWN = -10`, `NOTICE_DOWN = -5`.
- USD fiyatı toplu fiyat kaynağındaki Steam “last_24h” alanından gelir; anlık en düşük ilan olarak yorumlanmamalıdır. Kaynak değiştiğinde eski kaynakla hatalı değişim karşılaştırması yapılmaz.
- Item yüzdesi `((yeni - eski) / eski) × 100`; eski fiyat yoksa veya sıfırsa yüzde boş kalır. Fiyat alınamayan itemlar sonraki ölçümde de yanlış karşılaştırma üretmez.
- Envanterde ekleme/çıkarma veya adet değişimi varsa toplam fark bunu da içerir; raporda belirtilir. TL yüzdesi kur hareketini de içerir. Değerler satıştan eline geçecek net tutar değildir.
- Steam tüm sayfaları döndürmesine rağmen bildirilen toplamdan daha az item gösterebilir. Bu durumda erişilebilen itemlar işlenir; eksik item sayısı Telegram ve dashboard'da belirtilir, toplam kısmi olarak gösterilir; toplam değişim görünen itemlar üzerinden hesaplanır. Hiç asset dönmeyen dolu envanter veya yarım kalan sayfalama hâlâ hata kabul edilir.
- Geçmiş her başarılı veri kaydında 30 güne kırpılır. Repository'nin Git geçmişindeki eski commitler ayrıca var olmaya devam eder. Başarısız veri alımında son başarılı dosyalar korunur.
- Telegram her normal çalıştırmada rapor gönderir. İlk 5 yükselen/düşene ek olarak diğer ±%10 önemli hareketler de gönderilir; uzun raporlar Telegram boyut sınırına göre bölünür. Telegram erişimi tamamen kesikse teslimat mümkün değildir; Actions hata verir. Telegram gönderim yanıtı kaybolursa yeniden deneme nedeniyle aynı mesaj iki kez gelebilir.
- Envanter veya tüm fiyatlar alınamazsa anlamlı hata bildirimi gönderilmeye çalışılır; job başarısız olur. Art arda 5 ayrı item fiyatı alınamazsa daha fazla istek yapılmaz.
- Başka bir Steam hesabına geçersen geçmişi karıştırmamak için `data/history.json` dosyasını `[]` yap, `site/data/latest.json` dosyasını başlangıç biçimine döndür ve sonra yeni Secret ile çalıştır.

## Resmî kaynaklar

- [CSROI ücretsiz fiyat dosyası ve kaynak bilgisi](https://csroi.com/pricing.json), [CSROI FAQ](https://csroi.com/faq/)

- [Steam: Türkiye'de USD'ye geçiş](https://help.steampowered.com/tr/faqs/view/2720-4EC7-B95A-1D2A)
- [TCMB: kullanılan günlük kur XML'i](https://www.tcmb.gov.tr/kurlar/today.xml)
- [Telegram: bot oluşturma](https://core.telegram.org/bots/tutorial#obtain-your-bot-token), [getUpdates / sendMessage](https://core.telegram.org/bots/api)
- [GitHub: schedule davranışı ve gecikmeler](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [Netlify: ücretsiz plan ve krediler](https://www.netlify.com/pricing/), [üretim yayını maliyeti](https://docs.netlify.com/manage/accounts-and-billing/billing/billing-for-credit-based-plans/how-credits-work/), [Git bağlantısı](https://docs.netlify.com/deploy/create-deploys/), [gereksiz build'leri atlama](https://docs.netlify.com/build/configure-builds/ignore-builds/)
