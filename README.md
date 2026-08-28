# A101 Mağazada Bul — Telegram Botu

Ürün kodu + il yazarsın, o ildeki hangi A101 mağazalarında ürünün olduğunu listeler.

## Kurulum

```bash
pip install -r requirements.txt
cp .env.example .env    # içine yeni token'ı yaz
python bot.py
```

## Önemli: token'ı yenile

Token'ın sohbette açıkta paylaşıldı. BotFather'a git:

```
/mybots → selecta101bot → API Token → Revoke current token
```

Yeni token'ı `.env` içine koy. Token'ı asla koda veya git'e yazma.

## Kullanım

```
/tara 12345678                     ← 81 ilin hepsini tarar
/tara 12345678 Bursa İzmir         ← sadece belirtilen iller
/bul 12345678 İstanbul             ← tek il
/bul 12345678 İstanbul Kadıköy     ← il + ilçe
12345678 Kadıköy                   ← ilçe tek başına yeter, ili bot bulur
/iller                             ← 81 il, plaka ve ilçe sayısıyla
/ilceler Bursa                     ← o ilin ilçeleri
/kesfet                            ← A101 API adresini otomatik bulmayı dene
```

`/tara` çıktısı ilçe kırılımlı gelir:

```
✅ Ürün adı
3 ilde, 12 mağazada bulundu

📍 İstanbul — 7 mağaza
   Kadıköy (3), Ümraniye (2), Kartal (2)
📍 Bursa — 3 mağaza
   Nilüfer (2), Osmangazi (1)
```

### İl / ilçe verisi

`turkiye.json` içinde **81 il ve 973 ilçe** var; her il için plaka kodu da tutuluyor. Veri `volkansenturk/turkiye-iller-ilceler` deposundan alınıp Türkçe yazım ve sıralamaya göre düzenlendi.

Türkçe karakter zorunlu değil: `istanbul`, `Istanbul`, `İSTANBUL`, `kadikoy`, `cankaya` hepsi çalışır. `antep`, `urfa`, `maraş`, `içel` gibi kısaltmalar da tanınır.

`Merkez` gibi 51 ilde geçen ilçe adları tek başına kullanılamaz — il adıyla birlikte yaz (`/bul 12345 Yozgat Merkez`).

### Tarama hızı

`/tara` 81 ili aynı anda 4 istekle, her istek arasında 0.35 sn bekleyerek tarar; yaklaşık 1-2 dakika sürer. A101'e yük bindirmemek için bu değerleri (`scan_country` içindeki `concurrency` ve `delay`) yükseltme.

## GitHub + Render ile yayına alma (telefondan)

Render'ın ücretsiz katmanı kredi kartı istemiyor. Bot, `WEBHOOK_URL` ortam değişkenini görürse webhook moduna geçer; görmezse yerelde polling ile çalışır. Kod değişikliği gerekmez.

**1. GitHub deposu**
- github.com → giriş yap → sağ üst `+` → **New repository**
- Ad: `a101-magaza-bot`, **Private** seç, **Create**
- **uploading an existing file** bağlantısına dokun
- Şu 7 dosyayı yükle: `bot.py`, `a101_client.py`, `discover.py`, `turkiye.json`, `requirements.txt`, `render.yaml`, `.gitignore`
- **Commit changes**

`.env` dosyasını **yükleme**. Token oraya değil, Render'ın panelinden girilir.

**2. Render**
- render.com → **Get Started** → **GitHub ile giriş yap**
- **New +** → **Web Service** → depoyu seç
- Ayarlar `render.yaml` dosyasından otomatik gelir. Plan: **Free**
- **Environment** bölümünde `BOT_TOKEN` = BotFather'dan aldığın yeni token
- **Create Web Service**

Log ekranında `Webhook modu:` satırını görünce bot ayakta. Telegram'a geç, `/start` yaz.

**Ücretsiz katmanın tek dezavantajı:** 15 dakika mesaj gelmezse servis uykuya geçer, sonraki ilk mesaj ~50 saniye gecikmeli cevaplanır. İkinci mesajdan itibaren normal hıza döner. Bu rahatsız ederse aylık $7'lık Starter planı uyku moduna girmiyor.

**Kod güncellemesi:** GitHub'da dosyayı düzenleyip commit'lediğinde Render otomatik yeniden dağıtır.

## Endpoint nasıl bulunur

A101'in mağaza-stok servisi resmi bir API değil, dokümante edilmemiş. `a101_client.py` bir aday endpoint listesi deniyor; A101 adresleri değiştirdiyse hiçbiri tutmaz ve bot "endpoint güncellenmeli" hatası verir. O zaman:

1. Chrome'da `a101.com.tr` aç, herhangi bir ürün sayfasına gir.
2. F12 → **Network** sekmesi → **Fetch/XHR** filtresi.
3. Sayfadaki **"Mağazada Bul"** butonuna bas, il/ilçe seç.
4. Listeyi getiren isteği bul (cevabında mağaza adı ve adres geçiyor olacak).
5. İsteğe sağ tık → **Copy → Copy as cURL**.

Sonra `a101_client.py` içindeki `ENDPOINT CONFIG` bloğunu düzenle:

```python
STORE_STOCK_REQUESTS = [
    {
        "method": "POST",                        # cURL'deki metot
        "path": "/gercek/endpoint/yolu",         # domainden sonraki kısım
        "json": {"productId": "{product_id}", "cityId": "{city_id}"},
    },
]
```

Kullanılabilir değişkenler: `{query}`, `{product_id}`, `{city_id}`, `{city_name}`, `{city_slug}`.

Cevabın JSON şemasını bilmek zorunda değilsin — `parse_stores()` alan adlarını benzerlikle yakalıyor (`name`/`storeName`/`magazaAdi`, `address`/`adres`, `district`/`ilce`, `stock`/`quantity`/`adet` vb.). Farklı bir isimlendirme varsa `_pick()` çağrılarındaki listelere ekle.

İsteğin gerektirdiği ekstra başlıklar varsa (`Authorization`, `X-Api-Key` gibi) `DEFAULT_HEADERS`'a ekle.

## Notlar

- Kullanıcı başına 3 saniyelik bekleme var; A101'e gereksiz yük bindirmemek için bırak.
- Bir ilde 40'tan fazla mağaza çıkarsa ilk 40'ı gösterir; `MAX_STORES_SHOWN` ile değiştirebilirsin.
- Şehir listesi servisten gelmezse bot il adını düz metin olarak gönderir, sorun olmaz.
- Site yapısı değişebilir. Botu ürünleştireceksen A101'in kullanım şartlarını kontrol et ve istek hızını düşük tut.

## Sunucuda çalıştırma (systemd)

```ini
[Unit]
Description=A101 Telegram Bot
After=network.target

[Service]
WorkingDirectory=/opt/a101bot
ExecStart=/opt/a101bot/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
