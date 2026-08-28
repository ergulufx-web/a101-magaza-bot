"""
A101 "Mağazada Bul" istemcisi.

A101'in mağaza-stok servisi resmi olarak dokümante edilmiş bir API değil.
Bu yüzden istemci "aday endpoint" listesi deneyecek şekilde yazıldı:
sırayla dener, ilk anlamlı JSON cevabı verenle devam eder.

Gerçek endpoint'i yakalamak için README.md içindeki
"Endpoint nasıl bulunur" bölümüne bak; bulduğunda aşağıdaki
ENDPOINT CONFIG bloğunu güncelle, gerisi çalışır.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://www.a101.com.tr"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Referer": f"{BASE_URL}/",
    "X-Requested-With": "XMLHttpRequest",
}

# ============================================================
# ENDPOINT CONFIG  —  DevTools'ta yakaladığın gerçek isteği buraya yaz
# ============================================================
# Şablonlarda kullanılabilen değişkenler:
#   {query} {product_id} {city_id} {city_name} {city_slug}

PRODUCT_SEARCH_REQUESTS: list[dict[str, Any]] = [
    {"method": "GET", "path": "/api/v1/products/search", "params": {"q": "{query}"}},
    {"method": "GET", "path": "/api/search", "params": {"keyword": "{query}"}},
    {"method": "GET", "path": "/arama", "params": {"q": "{query}", "format": "json"}},
]

CITY_LIST_REQUESTS: list[dict[str, Any]] = [
    {"method": "GET", "path": "/api/v1/cities"},
    {"method": "GET", "path": "/api/v1/store/cities"},
    {"method": "GET", "path": "/magazalarimiz/sehirler"},
]

STORE_STOCK_REQUESTS: list[dict[str, Any]] = [
    {
        "method": "GET",
        "path": "/api/v1/products/{product_id}/stores",
        "params": {"cityId": "{city_id}"},
    },
    {
        "method": "GET",
        "path": "/magazada-bul",
        "params": {"productId": "{product_id}", "city": "{city_name}"},
    },
    {
        "method": "POST",
        "path": "/api/v1/stock/store-search",
        "json": {"productId": "{product_id}", "cityId": "{city_id}"},
    },
]

# ============================================================


# --- İl / ilçe veri seti (81 il, 973 ilçe) ---
_DATA_PATH = Path(__file__).with_name("turkiye.json")

try:
    TR_DATA: dict[str, dict[str, Any]] = json.loads(
        _DATA_PATH.read_text(encoding="utf-8")
    )
except (OSError, ValueError) as exc:  # pragma: no cover
    raise SystemExit(
        f"turkiye.json okunamadı ({exc}). Dosya bot.py ile aynı klasörde olmalı."
    ) from exc

TR_CITIES: list[str] = list(TR_DATA)
PLATE_OF: dict[str, int] = {c: TR_DATA[c]["plaka"] for c in TR_CITIES}
DISTRICTS_OF: dict[str, list[str]] = {c: TR_DATA[c]["ilceler"] for c in TR_CITIES}

_ALIASES = {
    "istanbulanadolu": "İstanbul",
    "istanbulavrupa": "İstanbul",
    "icel": "Mersin",
    "maras": "Kahramanmaraş",
    "urfa": "Şanlıurfa",
    "antep": "Gaziantep",
    "afyon": "Afyonkarahisar",
    "izmit": "Kocaeli",
    "adapazari": "Sakarya",
}

_TR_TRANSLATION = str.maketrans(
    {
        "ı": "i", "İ": "i", "I": "i",
        "ş": "s", "Ş": "s",
        "ğ": "g", "Ğ": "g",
        "ü": "u", "Ü": "u",
        "ö": "o", "Ö": "o",
        "ç": "c", "Ç": "c",
        "â": "a", "î": "i", "û": "u",
    }
)


def normalize(text: str) -> str:
    """Türkçe karakterleri sadeleştirip karşılaştırmaya uygun hale getirir."""
    return re.sub(r"[^a-z0-9]", "", text.translate(_TR_TRANSLATION).lower())


def resolve_city(user_input: str) -> str | None:
    """Kullanıcının yazdığı şehri resmi il adına eşler. Bulamazsa None."""
    key = normalize(user_input)
    if not key:
        return None
    if key in _ALIASES:
        return _ALIASES[key]
    for city in TR_CITIES:
        if normalize(city) == key:
            return city
    # Kısmi eşleşme (tek adaya düşerse kabul et)
    matches = [c for c in TR_CITIES if normalize(c).startswith(key)]
    if len(matches) == 1:
        return matches[0]
    return None


_DISTRICT_INDEX: dict[str, list[tuple[str, str]]] = {}
for _city, _list in DISTRICTS_OF.items():
    for _district in _list:
        _DISTRICT_INDEX.setdefault(normalize(_district), []).append((_city, _district))


def resolve_district(user_input: str, city: str | None = None) -> list[tuple[str, str]]:
    """İlçe adını (il, ilçe) çiftlerine eşler.

    'Merkez' gibi çok ilde geçen adlar birden fazla sonuç döndürür.
    city verilirse sonuç o ille sınırlanır.
    """
    key = normalize(user_input)
    matches = list(_DISTRICT_INDEX.get(key, []))
    if not matches:
        for norm_key, pairs in _DISTRICT_INDEX.items():
            if norm_key.startswith(key) and len(key) >= 3:
                matches.extend(pairs)
    if city:
        matches = [m for m in matches if normalize(m[0]) == normalize(city)]
    return matches


def districts_of(city: str) -> list[str]:
    resolved = resolve_city(city)
    return DISTRICTS_OF.get(resolved, []) if resolved else []


def match_district(store: Store, district: str) -> bool:
    """Mağaza kaydının verilen ilçeye ait olup olmadığına bakar."""
    target = normalize(district)
    if store.district and normalize(store.district) == target:
        return True
    # İlçe alanı boşsa adres metninde ara
    haystack = normalize(f"{store.address} {store.name}")
    return target in haystack


class A101Error(Exception):
    """İstemci hatası — kullanıcıya gösterilebilir mesaj taşır."""


@dataclass
class Store:
    name: str
    address: str = ""
    district: str = ""
    city: str = ""
    phone: str = ""
    latitude: float | None = None
    longitude: float | None = None
    stock: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def maps_url(self) -> str:
        if self.latitude is not None and self.longitude is not None:
            return f"https://maps.google.com/?q={self.latitude},{self.longitude}"
        query = ", ".join(p for p in (self.name, self.district, self.city) if p)
        return "https://maps.google.com/?q=" + httpx.QueryParams({"q": query})["q"]


@dataclass
class Product:
    id: str
    name: str = ""
    price: str = ""
    url: str = ""


# ---------- Genel amaçlı JSON gezgini ----------
# Cevabın şeması bilinmediği için alanları isim benzerliğiyle yakalıyoruz.

def _walk(obj: Any) -> Iterator[dict]:
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _pick(data: dict, candidates: Sequence[str], default: Any = None) -> Any:
    wanted = {normalize(c) for c in candidates}
    for key, value in data.items():
        if normalize(key) in wanted and value not in (None, "", []):
            return value
    return default


def _as_float(value: Any) -> float | None:
    try:
        result = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return result if result != 0 else None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return 1 if value else 0
    try:
        return int(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return None


def _render(template: Any, ctx: dict[str, str]) -> Any:
    """Şablondaki {degisken} yerlerini doldurur; boş kalanları atar."""
    if isinstance(template, str):
        try:
            return template.format(**ctx)
        except KeyError:
            return None
    if isinstance(template, dict):
        rendered = {k: _render(v, ctx) for k, v in template.items()}
        return {k: v for k, v in rendered.items() if v not in (None, "", "None")}
    return template


def parse_stores(payload: Any) -> list[Store]:
    """Şeması bilinmeyen JSON içinden mağaza kayıtlarını çıkarır."""
    stores: list[Store] = []
    seen: set[tuple[str, str]] = set()

    for node in _walk(payload):
        name = _pick(node, ["name", "storeName", "magazaAdi", "title", "storeTitle"])
        address = _pick(node, ["address", "adres", "fullAddress", "storeAddress"])
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(address, str):
            address = ""
        # Mağaza kaydı sayılması için adres ya da konum bilgisi olsun
        lat = _as_float(_pick(node, ["latitude", "lat", "enlem"]))
        lon = _as_float(_pick(node, ["longitude", "lng", "lon", "boylam"]))
        if not address and lat is None:
            continue

        key = (normalize(name), normalize(address)[:40])
        if key in seen:
            continue
        seen.add(key)

        stores.append(
            Store(
                name=name.strip(),
                address=address.strip(),
                district=str(_pick(node, ["district", "ilce", "town", "districtName"], "") or ""),
                city=str(_pick(node, ["city", "il", "cityName", "province"], "") or ""),
                phone=str(_pick(node, ["phone", "telefon", "phoneNumber", "gsm"], "") or ""),
                latitude=lat,
                longitude=lon,
                stock=_as_int(
                    _pick(node, ["stock", "stockQuantity", "quantity", "adet", "available", "hasStock"])
                ),
                raw=node,
            )
        )
    return stores


class A101Client:
    def __init__(self, timeout: float = 20.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers=DEFAULT_HEADERS,
            timeout=timeout,
            follow_redirects=True,
        )
        self._cities_cache: dict[str, str] | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _try_requests(
        self, requests: list[dict[str, Any]], ctx: dict[str, str]
    ) -> Any | None:
        """Aday istekleri sırayla dener, ilk geçerli JSON'u döndürür."""
        for spec in requests:
            path = _render(spec["path"], ctx)
            if path is None:
                continue
            kwargs: dict[str, Any] = {}
            if "params" in spec:
                kwargs["params"] = _render(spec["params"], ctx)
            if "json" in spec:
                kwargs["json"] = _render(spec["json"], ctx)
            try:
                response = await self._client.request(spec["method"], path, **kwargs)
            except httpx.HTTPError as exc:
                log.debug("İstek başarısız %s %s: %s", spec["method"], path, exc)
                continue
            if response.status_code >= 400:
                log.debug("HTTP %s → %s", response.status_code, response.url)
                continue
            try:
                data = response.json()
            except ValueError:
                log.debug("JSON değil: %s", response.url)
                continue
            log.info("Çalışan endpoint: %s %s", spec["method"], response.url)
            return data
        return None

    async def get_cities(self) -> dict[str, str]:
        """{normalize(il adı): il id} sözlüğü. Servis vermezse boş döner."""
        async with self._lock:
            if self._cities_cache is not None:
                return self._cities_cache
            data = await self._try_requests(CITY_LIST_REQUESTS, {})
            cities: dict[str, str] = {}
            if data is not None:
                for node in _walk(data):
                    name = _pick(node, ["name", "cityName", "il", "text", "title"])
                    ident = _pick(node, ["id", "cityId", "value", "code", "plateCode"])
                    if isinstance(name, str) and ident is not None:
                        if resolve_city(name):
                            cities[normalize(name)] = str(ident)
            self._cities_cache = cities
            return cities

    async def resolve_product(self, code: str) -> Product:
        """Ürün kodunu ürün kimliğine çevirir. Bulamazsa kodu id kabul eder."""
        data = await self._try_requests(PRODUCT_SEARCH_REQUESTS, {"query": code})
        if data is not None:
            for node in _walk(data):
                ident = _pick(node, ["id", "productId", "sku", "code", "barcode"])
                name = _pick(node, ["name", "productName", "title", "urunAdi"])
                if ident is not None and isinstance(name, str) and name.strip():
                    return Product(
                        id=str(ident),
                        name=name.strip(),
                        price=str(_pick(node, ["price", "salePrice", "fiyat"], "") or ""),
                        url=str(_pick(node, ["url", "link", "slug"], "") or ""),
                    )
        return Product(id=code)

    async def find_stores(
        self,
        product_code: str,
        city_input: str,
        district: str | None = None,
        product: Product | None = None,
    ) -> tuple[Product, list[Store]]:
        city = resolve_city(city_input)
        if city is None:
            raise A101Error(
                f"'{city_input}' bir il adına benzemiyor. Örnek: İstanbul, Ankara, İzmir."
            )

        if product is None:
            product = await self.resolve_product(product_code)
        cities = await self.get_cities()
        city_id = cities.get(normalize(city), "")

        ctx = {
            "query": product_code,
            "product_id": product.id,
            "city_id": city_id,
            "city_name": city,
            "city_slug": normalize(city),
        }

        data = await self._try_requests(STORE_STOCK_REQUESTS, ctx)
        if data is None:
            raise A101Error(
                "A101 mağaza servisine ulaşamadım. Endpoint değişmiş olabilir; "
                "README'deki adımlarla güncel adresi yakalayıp a101_client.py "
                "içindeki ENDPOINT CONFIG bloğuna yazman gerekiyor."
            )

        stores = parse_stores(data)

        # Servis ili filtrelemiyorsa biz filtreleyelim
        if any(s.city for s in stores):
            filtered = [s for s in stores if not s.city or normalize(s.city) == normalize(city)]
            if filtered:
                stores = filtered

        # Stok bilgisi varsa stoksuzları ele
        if any(s.stock is not None for s in stores):
            in_stock = [s for s in stores if s.stock is None or s.stock > 0]
            if in_stock:
                stores = in_stock

        if district:
            filtered = [s for s in stores if match_district(s, district)]
            stores = filtered

        # İl bilgisi boş gelen kayıtlara ili biz yazalım
        for store in stores:
            if not store.city:
                store.city = city

        stores.sort(key=lambda s: (normalize(s.district), normalize(s.name)))
        return product, stores

    async def scan_country(
        self,
        product_code: str,
        cities: Sequence[str] | None = None,
        concurrency: int = 4,
        delay: float = 0.35,
        on_progress: Callable[[int, int, str, int], None] | None = None,
    ) -> tuple[Product, dict[str, list[Store]]]:
        """Ürünü verilen illerin hepsinde arar (varsayılan: 81 il).

        on_progress(tamamlanan, toplam, il_adi, o_ildeki_magaza_sayisi)
        """
        target_cities = list(cities) if cities else list(TR_CITIES)
        product = await self.resolve_product(product_code)

        results: dict[str, list[Store]] = {}
        semaphore = asyncio.Semaphore(concurrency)
        done = 0
        lock = asyncio.Lock()

        async def scan_one(city: str) -> None:
            nonlocal done
            found: list[Store] = []
            async with semaphore:
                try:
                    _, found = await self.find_stores(product_code, city, product=product)
                except A101Error:
                    found = []
                except Exception:  # tek il patlarsa tarama devam etsin
                    log.exception("Tarama hatası: %s", city)
                    found = []
                await asyncio.sleep(delay)  # A101'e nazik davran
            async with lock:
                done += 1
                if found:
                    results[city] = found
                if on_progress:
                    on_progress(done, len(target_cities), city, len(found))

        await asyncio.gather(*(scan_one(c) for c in target_cities))
        return product, results
