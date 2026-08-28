"""
A101 Mağazada Bul — Telegram botu.

Kullanım:
    /bul 12345678 İstanbul
    veya düz mesaj: "12345678 İstanbul"

Token'ı .env dosyasından okur (BOT_TOKEN). Koda token yazma.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from collections import defaultdict

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import discover as discovery
from a101_client import (
    A101Client,
    A101Error,
    DISTRICTS_OF,
    PLATE_OF,
    Store,
    TR_CITIES,
    districts_of,
    resolve_city,
    resolve_district,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("a101bot")

MAX_STORES_SHOWN = 40
MESSAGE_LIMIT = 3800
COOLDOWN_SECONDS = 3

_last_request: dict[int, float] = defaultdict(float)

WELCOME = (
    "👋 <b>A101 Mağazada Bul</b>\n\n"
    "81 il ve 973 ilçe tanımlı. Ürün kodunu yaz, nerede olduğunu bulayım.\n\n"
    "<b>Örnekler</b>\n"
    "<code>/tara 12345678</code> — tüm Türkiye'yi tara\n"
    "<code>/bul 12345678 İstanbul</code> — tek il\n"
    "<code>/bul 12345678 İstanbul Kadıköy</code> — il + ilçe\n"
    "<code>12345678 Kadıköy</code> — ilçe tek başına da yeter\n"
    "<code>/tara 12345678 Bursa İzmir</code> — sadece belirli iller\n\n"
    "<b>Komutlar</b>\n"
    "/tara — ürünü 81 ilde ara\n"
    "/bul — tek il veya ilçede ara\n"
    "/iller — 81 ilin listesi\n"
    "/ilceler &lt;il&gt; — o ilin ilçeleri\n"
    "/kesfet — A101 API adresini otomatik bul\n"
    "/yardim — bu mesaj"
)


def _rate_limited(user_id: int) -> bool:
    now = time.monotonic()
    if now - _last_request[user_id] < COOLDOWN_SECONDS:
        return True
    _last_request[user_id] = now
    return False


def parse_query(text: str) -> tuple[str, str, str | None] | None:
    """Serbest metinden (ürün kodu, il, ilçe) çıkarır.

    Desteklenen biçimler:
        '12345 İstanbul'
        '12345 İstanbul Kadıköy'
        '12345 Kadıköy'        ← ilçe tek başına da yeter
    """
    parts = text.split()
    if len(parts) < 2:
        return None

    # 1) Sondan il ara (1 veya 2 kelimelik il adları için)
    for take in (2, 1):
        if len(parts) <= take:
            continue
        city = resolve_city(" ".join(parts[-take:]))
        if city:
            rest = parts[:-take]
            if not rest:
                continue
            # İlin hemen öncesinde/sonrasında ilçe olabilir mi?
            for dtake in (2, 1):
                if len(rest) > dtake:
                    matches = resolve_district(" ".join(rest[-dtake:]), city)
                    if matches:
                        code = " ".join(rest[:-dtake]).strip()
                        if code:
                            return code, city, matches[0][1]
            return " ".join(rest).strip(), city, None

    # 2) İl yoksa sondan ilçe ara → ilini kendimiz buluruz
    for take in (2, 1):
        if len(parts) <= take:
            continue
        matches = resolve_district(" ".join(parts[-take:]))
        if len(matches) == 1:
            city, district = matches[0]
            rest = parts[:-take]
            # "12345 İstanbul Kadıköy" gibi: kalanın sonundaki il adını at
            for ctake in (2, 1):
                if len(rest) > ctake and resolve_city(" ".join(rest[-ctake:])) == city:
                    rest = rest[:-ctake]
                    break
            code = " ".join(rest).strip()
            if code:
                return code, city, district
    return None


def format_store(index: int, store: Store) -> str:
    lines = [f"<b>{index}. {html.escape(store.name)}</b>"]
    location = " / ".join(p for p in (store.district, store.city) if p)
    if location:
        lines.append(f"📍 {html.escape(location)}")
    if store.address:
        address = store.address if len(store.address) <= 140 else store.address[:137] + "..."
        lines.append(html.escape(address))
    if store.phone:
        lines.append(f"☎️ {html.escape(store.phone)}")
    if store.stock is not None:
        lines.append(f"📦 Stok: {store.stock}")
    lines.append(f'<a href="{html.escape(store.maps_url, quote=True)}">Haritada aç</a>')
    return "\n".join(lines)


def build_messages(product, stores: list[Store], city: str) -> list[str]:
    title = product.name or f"Ürün {product.id}"
    header = (
        f"🔎 <b>{html.escape(title)}</b>\n"
        f"🏙 {html.escape(city)} — <b>{len(stores)}</b> mağazada bulundu"
    )
    if len(stores) > MAX_STORES_SHOWN:
        header += f" (ilk {MAX_STORES_SHOWN} tanesi gösteriliyor)"

    blocks = [format_store(i, s) for i, s in enumerate(stores[:MAX_STORES_SHOWN], 1)]

    messages: list[str] = []
    current = header
    for block in blocks:
        if len(current) + len(block) + 2 > MESSAGE_LIMIT:
            messages.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}"
    messages.append(current)
    return messages


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.HTML)


async def cmd_cities(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    toplam_ilce = sum(len(v) for v in DISTRICTS_OF.values())
    satirlar = [
        f"<b>{len(TR_CITIES)} il · {toplam_ilce} ilçe</b>",
        "İlçeleri görmek için: <code>/ilceler Bursa</code>\n",
    ]
    satirlar += [
        f"{PLATE_OF[c]:02d} {html.escape(c)} ({len(DISTRICTS_OF[c])})" for c in TR_CITIES
    ]
    await update.message.reply_text("\n".join(satirlar), parse_mode=ParseMode.HTML)


async def run_search(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str) -> None:
    user_id = update.effective_user.id
    if _rate_limited(user_id):
        await update.message.reply_text("Biraz yavaş 🙂 Birkaç saniye sonra tekrar dene.")
        return

    parsed = parse_query(raw)
    if not parsed:
        await update.message.reply_text(
            "Ürün kodu ve il adını birlikte yaz.\n"
            "Örnek: <code>/bul 12345678 İstanbul</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    code, city, district = parsed
    hedef = f"{city} / {district}" if district else city

    status = await update.message.reply_text(f"🔍 {hedef} mağazaları taranıyor...")
    client: A101Client = context.application.bot_data["a101"]

    try:
        product, stores = await asyncio.wait_for(
            client.find_stores(code, city, district=district), timeout=45
        )
    except asyncio.TimeoutError:
        await status.edit_text("⏱ A101 tarafı zaman aşımına uğradı, tekrar dener misin?")
        return
    except A101Error as exc:
        await status.edit_text(f"⚠️ {exc}")
        return
    except Exception:
        log.exception("Beklenmeyen hata")
        await status.edit_text("❌ Beklenmeyen bir hata oldu. Loglara bakmak gerekiyor.")
        return

    if not stores:
        await status.edit_text(
            f"😕 <b>{html.escape(code)}</b> kodlu ürün {html.escape(hedef)} "
            f"mağazalarında bulunamadı.\n\n"
            f"Tüm Türkiye'de aramak için: <code>/tara {html.escape(code)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    messages = build_messages(product, stores, hedef)
    await status.edit_text(
        messages[0], parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )
    for extra in messages[1:]:
        await update.message.reply_text(
            extra, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_search(update, context, " ".join(context.args))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_search(update, context, update.message.text or "")


async def cmd_districts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bir ilin ilçelerini listeler."""
    if not context.args:
        await update.message.reply_text(
            "Hangi ilin ilçeleri? Örnek: <code>/ilceler Bursa</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    city = resolve_city(" ".join(context.args))
    if not city:
        await update.message.reply_text("Bu adı bir ile eşleyemedim. /iller ile listeye bak.")
        return
    ilceler = districts_of(city)
    await update.message.reply_text(
        f"<b>{html.escape(city)}</b> ({PLATE_OF[city]:02d}) — {len(ilceler)} ilçe\n\n"
        + html.escape(", ".join(ilceler)),
        parse_mode=ParseMode.HTML,
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ürün kodunu 81 ilde tarar, bulunduğu il ve ilçeleri raporlar."""
    user_id = update.effective_user.id
    if _rate_limited(user_id):
        await update.message.reply_text("Biraz yavaş 🙂 Birkaç saniye sonra tekrar dene.")
        return

    if not context.args:
        await update.message.reply_text(
            "Ürün kodunu yaz: <code>/tara 12345678</code>\n"
            "Belirli illerle sınırlamak için: <code>/tara 12345678 Bursa İzmir</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    code = context.args[0]
    hedef_iller = [c for c in (resolve_city(a) for a in context.args[1:]) if c] or None
    toplam = len(hedef_iller or TR_CITIES)

    status = await update.message.reply_text(
        f"🇹🇷 <b>{html.escape(code)}</b> için {toplam} il taranıyor...\n"
        f"Bu 1-3 dakika sürebilir.",
        parse_mode=ParseMode.HTML,
    )

    client: A101Client = context.application.bot_data["a101"]
    state = {"done": 0, "hits": 0, "last_edit": 0.0}

    def on_progress(done: int, total: int, city: str, count: int) -> None:
        state["done"] = done
        if count:
            state["hits"] += 1

    async def progress_ticker() -> None:
        while state["done"] < toplam:
            await asyncio.sleep(8)
            try:
                await status.edit_text(
                    f"🇹🇷 Taranıyor: <b>{state['done']}/{toplam}</b> il · "
                    f"{state['hits']} ilde bulundu",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass  # "message is not modified" gibi hatalar önemsiz

    ticker = asyncio.create_task(progress_ticker())
    try:
        product, results = await asyncio.wait_for(
            client.scan_country(code, cities=hedef_iller, on_progress=on_progress),
            timeout=600,
        )
    except asyncio.TimeoutError:
        await status.edit_text("⏱ Tarama çok uzun sürdü, illeri daraltıp tekrar dene.")
        return
    except A101Error as exc:
        await status.edit_text(f"⚠️ {exc}")
        return
    except Exception:
        log.exception("Tarama hatası")
        await status.edit_text("❌ Tarama sırasında hata oldu.")
        return
    finally:
        ticker.cancel()

    if not results:
        await status.edit_text(
            f"😕 <b>{html.escape(code)}</b> taranan {toplam} ilin hiçbirinde bulunamadı.",
            parse_mode=ParseMode.HTML,
        )
        return

    title = product.name or f"Ürün {product.id}"
    toplam_magaza = sum(len(v) for v in results.values())
    lines = [
        f"✅ <b>{html.escape(title)}</b>",
        f"{len(results)} ilde, {toplam_magaza} mağazada bulundu\n",
    ]

    for city in sorted(results, key=lambda c: -len(results[c])):
        stores = results[city]
        ilceler: dict[str, int] = {}
        for store in stores:
            key = store.district or "?"
            ilceler[key] = ilceler.get(key, 0) + 1
        ozet = ", ".join(
            f"{html.escape(d)} ({n})" for d, n in sorted(ilceler.items(), key=lambda kv: -kv[1])
        )
        lines.append(f"📍 <b>{html.escape(city)}</b> — {len(stores)} mağaza\n   {ozet}")

    lines.append(
        f"\nBir ildeki adresler için: <code>/bul {html.escape(code)} "
        f"{html.escape(next(iter(results)))}</code>"
    )

    text = "\n".join(lines)
    await status.edit_text(text[:MESSAGE_LIMIT], parse_mode=ParseMode.HTML)
    if len(text) > MESSAGE_LIMIT:
        await update.message.reply_text(text[MESSAGE_LIMIT:][:MESSAGE_LIMIT], parse_mode=ParseMode.HTML)


async def cmd_discover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A101 JS paketlerini tarayıp aday API adreslerini listeler."""
    status = await update.message.reply_text(
        "🛠 A101 sayfaları ve JS paketleri taranıyor, 30-60 saniye sürebilir..."
    )
    try:
        candidates = await asyncio.wait_for(discovery.discover(), timeout=120)
    except asyncio.TimeoutError:
        await status.edit_text("⏱ Tarama zaman aşımına uğradı.")
        return
    except Exception:
        log.exception("Keşif hatası")
        await status.edit_text("❌ Tarama sırasında hata oldu, loglara bak.")
        return

    if not candidates:
        await status.edit_text(
            "Aday adres bulunamadı. Site JS'i gizlenmiş olabilir. "
            "Bu durumda masaüstü tarayıcıda DevTools ile yakalaman gerekir."
        )
        return

    top = candidates[:12]
    probes = await discovery.probe([c.path for c in top[:8]])
    notes = dict(probes)

    lines = ["🧭 <b>Aday endpoint'ler</b> (puan yüksek = daha olası)\n"]
    for candidate in top:
        note = notes.get(candidate.path, "")
        lines.append(
            f"<code>{html.escape(candidate.path)}</code>\n"
            f"  puan {candidate.score} · {html.escape(note or candidate.seen_in)}"
        )
    lines.append(
        "\n✅ JSON dönen bir adres varsa onu <code>a101_client.py</code> "
        "içindeki <code>STORE_STOCK_REQUESTS</code> listesine yaz."
    )
    await status.edit_text("\n".join(lines)[:MESSAGE_LIMIT], parse_mode=ParseMode.HTML)


async def on_startup(application: Application) -> None:
    application.bot_data["a101"] = A101Client()
    log.info("Bot hazır.")


async def on_shutdown(application: Application) -> None:
    client: A101Client | None = application.bot_data.get("a101")
    if client:
        await client.aclose()


def main() -> None:
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit(
            "BOT_TOKEN tanımlı değil. .env dosyası oluşturup içine\n"
            "BOT_TOKEN=... satırını ekle."
        )

    application = (
        Application.builder()
        .token(token)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    application.add_handler(CommandHandler(["start", "yardim", "help"], cmd_start))
    application.add_handler(CommandHandler(["sehirler", "iller"], cmd_cities))
    application.add_handler(CommandHandler(["bul", "ara"], cmd_find))
    application.add_handler(CommandHandler(["tara", "turkiye"], cmd_scan))
    application.add_handler(CommandHandler(["ilceler", "ilce"], cmd_districts))
    application.add_handler(CommandHandler("kesfet", cmd_discover))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_text)
    )

    # Sunucuda (Render vb.) WEBHOOK_URL varsa webhook, yoksa polling.
    webhook_url = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if webhook_url:
        port = int(os.getenv("PORT", "10000"))
        secret = token.split(":")[-1][:24]  # tahmin edilemeyen yol parçası
        log.info("Webhook modu: %s (port %s)", webhook_url, port)
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=secret,
            webhook_url=f"{webhook_url.rstrip('/')}/{secret}",
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    else:
        log.info("Polling modu (yerel çalışma).")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
