"""Ingesta híbrida de ofertas públicas de impresión 3D en México."""
from __future__ import annotations

import html
import hashlib
import gzip
import json
import logging
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .mercadolibre_browser import BrowserListingError, fetch_listing

DATA_DIR = Path(os.environ.get("COMPARA3D_DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
API_URL = "https://www.inovamarket.com/wp-json/wc/store/v1/products"
SCRAPE_URL = "https://www.3dcity.com.mx/search"
SHOP3D_URL = "https://shop3d.mx/wp-json/wc/store/v1/products"
AMAZON_URL = "https://www.amazon.com.mx/s"
MERCADOLIBRE_URL = "https://listado.mercadolibre.com.mx"
MARKET_PRINTERS = "https://www.3dmarket.mx/c/impresoras-3d-mexico/"
MARKET_FILAMENTS = "https://www.3dmarket.mx/c/filamentos/"
CREALITY_PRINTERS = "https://store.creality.com/mx/collections/3d-printers"
CREALITY_FILAMENTS = "https://store.creality.com/mx/collections/filamentos"
MARKET_SITEMAPS = tuple(f"https://www.3dmarket.mx/product-sitemap{suffix}.xml" for suffix in ("", "2", "3"))
_sitemap_cache: tuple[float, list[str]] | None = None
QUERIES = {"impresora": "impresora 3d", "filamento": "filamento"}
FILAMENT_MATERIALS = ("PETG-CF", "PLA-CF", "PAHT-CF", "PPA-CF", "PA-CF", "PA-GF",
                      "ABS-GF", "ABS-CF", "PPS-CF", "PC-CF", "PETG", "PLA", "ABS",
                      "ASA", "TPU", "TPE", "PVA", "HIPS", "PCTG", "PVB", "PPS",
                      "PEEK", "PEI", "CPE", "PEBA", "PVDF", "FLEX", "PPA", "PC",
                      "PP", "PA", "PET", "POM")
USER_AGENT = "Compare3D-Academic/1.0 (course project; respectful requests)"
LOG = logging.getLogger(__name__)


def session() -> requests.Session:
    client = requests.Session()
    client.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html, application/json"})
    retry = Retry(total=3, connect=3, read=3, status=3, backoff_factor=1,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"], respect_retry_after_header=True)
    client.mount("https://", HTTPAdapter(max_retries=retry))
    return client


def live_session() -> requests.Session:
    """Consultas interactivas acotadas; la ingesta programada sí usa reintentos largos."""
    client = requests.Session()
    client.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html, application/json"})
    return client


def material_from_name(name: str) -> str | None:
    value = html.unescape(name).upper().replace("_", "-")
    pa_reinforced = re.search(r"(?<![A-Z0-9])PA(?:6|12)[- ]?(CF|GF)(?:\d+)?(?![A-Z0-9])", value)
    if pa_reinforced:
        return "PA-" + pa_reinforced.group(1)
    if re.search(r"(?<![A-Z0-9])PPA[- ]?CF(?![A-Z0-9])", value):
        return "PPA-CF"
    for material in FILAMENT_MATERIALS:
        if re.search(rf"(?<![A-Z0-9]){re.escape(material)}(?:\d+)?(?![A-Z0-9])", value):
            return material
    if re.search(r"\bNYLON\b|\bPA(?:6|12)\b", value):
        return "PA"
    return None


def _filament_accessory(name: str) -> bool:
    if re.search(r"\b(?:bolígrafo|lápiz|lapiz)\s*(?:de impresora\s*)?3d\b|filamento de lápiz", name, re.I) and not re.search(r"\b(?:\d+(?:[.,]\d+)?\s*kg|\d{3,4}\s*g|carrete|bobina|spool)\b", name, re.I):
        return True
    return bool(re.search(r"\b(?:filamento de limpieza|filamento limpieza|cleaning filament|"
                          r"3doodler|pluma 3d|hot\s*end|hotend|boquilla|extrusor|"
                          r"secador de filamento|caja de almacenamiento de filamentos|"
                          r"contenedores? de filamentos)\b", name, re.I))


def category(name: str) -> str | None:
    name = html.unescape(name).lower()
    material = material_from_name(name)
    if ("filamento" in name or "filament" in name or
            material and re.match(r"^(?:" + "|".join(re.escape(m.lower()) for m in FILAMENT_MATERIALS) + r")\b", name)):
        return material or "OTRO"
    printer_name = (re.search(r"^impresora\s*3d\b", name) or
                    re.search(r"\bimpresora\s*3d$", name) or
                    re.search(r"^3d\s*printer\b", name))
    if printer_name:
        excluded = ("repuesto", "refacción", "refaccion", "boquilla", "kit", "placa", "plataforma", "pantalla", "carro", "rueda", "resina", "filamento", "accesorio", "para impresora", "garganta", "funda", "correa", "hotend", "resistencia", "grasa", "cable", "sensor", "ventilador", "extrusor", "cama", "calcetin")
        if not any(word in name for word in excluded):
            return "impresora"
    return None


def parse_api_product(item: dict, store: str = "Inovamarket") -> dict | None:
    name = html.unescape(item.get("name", "")).strip()
    kind = category(name)
    slugs = {c.get("slug", "").lower() for c in item.get("categories", [])}
    if not kind and "impresora-filamento" in slugs:
        kind = "impresora"
    if not kind and any("filament" in slug or slug in {"pla", "petg", "abs", "nylon", "tpu"} for slug in slugs):
        kind = material_from_name(name) or next((m.upper() for m in ("pla", "petg", "abs", "tpu") if m in slugs), "OTRO")
    if kind != "impresora" and _filament_accessory(name):
        return None
    prices = item.get("prices") or {}
    try:
        amount = Decimal(str(prices["price"])) / (10 ** int(prices.get("currency_minor_unit", 2)))
    except (KeyError, ValueError, InvalidOperation, TypeError):
        return None
    if not kind or amount <= 0 or (kind == "impresora" and amount < 2000) or prices.get("currency_code") != "MXN":
        return None
    images = item.get("images") or []
    return {
        "id": f"{store.lower()}:{item['id']}", "name": name, "category": kind,
        "price": float(amount), "currency": "MXN", "store": store,
        "method": "API", "url": item.get("permalink", ""),
        "image": images[0].get("src") if images else None,
        "price_note": "IVA incluido según tienda" if store == "Inovamarket" else "Precio publicado",
        "available": bool(item.get("is_in_stock")),
        "rating": float(item.get("average_rating") or 0) or None,
        "review_count": int(item.get("review_count") or 0),
    }


def parse_scrape_products(markup: str) -> list[dict]:
    soup = BeautifulSoup(markup, "html.parser")
    found = []
    for card in soup.select("li.product-grid__item"):
        link = card.select_one('a[href*="/products/"]')
        price_node = card.select_one(".price-item--sale.price-item--last") or card.select_one(".price__regular .price-item")
        if not link or not price_node:
            continue
        name = link.get_text(" ", strip=True)
        kind = category(name)
        match = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)", price_node.get_text(" ", strip=True))
        if not kind or kind != "impresora" and _filament_accessory(name) or not match:
            continue
        amount = Decimal(match.group(1).replace(",", ""))
        if amount <= 0 or (kind == "impresora" and amount < 2000):
            continue
        url = urljoin("https://www.3dcity.com.mx", link["href"].split("?")[0])
        found.append({
            "id": "3dcity:" + url.rstrip("/").split("/")[-1],
            "name": name, "category": kind, "price": float(amount),
            "currency": "MXN", "store": "3DCity", "method": "scraping",
            "url": url, "available": not bool(card.select_one(".badge--sold-out")),
            "image": urljoin("https://www.3dcity.com.mx", (card.select_one(".card__media img") or {}).get("src", "").split("?")[0]) if card.select_one(".card__media img") else None,
            "price_note": "Precio publicado",
            "rating": None, "review_count": None,
        })
    return found


def parse_amazon_search(markup: str, kind_hint: str | None = None) -> list[dict]:
    """Extrae solo listados con ASIN, precio e imagen de resultados públicos de Amazon MX."""
    soup = BeautifulSoup(markup, "html.parser")
    found = {}
    for card in soup.select('[data-component-type="s-search-result"][data-asin]'):
        asin = card.get("data-asin", "").strip()
        heading = card.select_one("h2")
        price_node = card.select_one(".a-price .a-offscreen")
        image = card.select_one("img.s-image")
        if not re.fullmatch(r"[A-Z0-9]{10}", asin) or not heading or not price_node or not image:
            continue
        name = heading.get_text(" ", strip=True)
        if _filament_accessory(name):
            continue
        if kind_hint == "filamento":
            kind = category(name)
            if kind == "impresora" or not kind:
                continue
        elif kind_hint == "impresora":
            if not re.search(r"\b(?:impresora|impresión\s*3d|3d\s*printer)\b", name, re.I):
                continue
            if re.search(r"^(?:resina|filamento|filament|boquilla|hotend|secador)\b", name, re.I):
                continue
            kind = "impresora"
        else:
            kind = category(name)
            if not kind and re.search(r"\b(?:impresora|impresión\s*3d|3d\s*printer)\b", name, re.I):
                kind = "impresora"
        amount = _money(price_node.get_text(" ", strip=True))
        if not kind or amount is None or amount <= 0 or kind == "impresora" and amount < 2000:
            continue
        found[asin] = {
            "id": f"amazon:{asin}", "name": name, "category": kind,
            "price": float(amount), "currency": "MXN", "store": "Amazon México",
            "method": "scraping", "url": f"https://www.amazon.com.mx/dp/{asin}",
            "image": image.get("src"), "price_note": "Precio mostrado; verifica envío y variante",
            "available": None, "availability_note": "Existencia por confirmar",
            "rating": None, "review_count": None,
        }
    return list(found.values())


def mercadolibre_search_url(query: str) -> str:
    """Use the public listing URL without depending on authenticated APIs."""
    slug = quote(re.sub(r"\s+", "-", query.strip()), safe="-")
    return f"{MERCADOLIBRE_URL}/{slug}"


def parse_mercadolibre_search(markup: str, kind_hint: str | None = None) -> list[dict]:
    """Read title, current price, image and product URL from public result cards."""
    soup = BeautifulSoup(markup, "html.parser")
    found = {}
    for card in soup.select(".ui-search-layout__item"):
        link = card.select_one("a.poly-component__title, h2.poly-component__title a, h2.poly-box a")
        amount = card.select_one(".poly-price__current .andes-money-amount, .poly-price__current [role='img']")
        image = card.select_one("img.poly-component__picture")
        if not link or not amount or not image:
            continue
        name = html.unescape(link.get_text(" ", strip=True))
        if _filament_accessory(name):
            continue
        if kind_hint == "filamento":
            kind = category(name)
            if not kind or kind == "impresora":
                continue
        elif kind_hint == "impresora":
            printer_model = re.search(r"\b(?:bambu\s+lab|creality\s+ender|elegoo\s+(?:neptune|mars|saturn|centauri)|anycubic\s+(?:kobra|photon)|flashforge|snapmaker)\b", name, re.I)
            kind = "impresora" if (re.search(r"\b(?:impresora\s*3d|3d\s*printer)\b", name, re.I) or printer_model) and not _printer_accessory(name) else None
        else:
            kind = category(name)
        fraction = amount.select_one(".andes-money-amount__fraction")
        cents = amount.select_one(".andes-money-amount__cents")
        if not kind:
            continue
        try:
            if fraction:
                price = Decimal(fraction.get_text(strip=True).replace(",", "").replace(".", ""))
                if cents:
                    price += Decimal(cents.get_text(strip=True)) / 100
            else:
                price = _money(amount.get("aria-label", "") or amount.get_text(" ", strip=True))
        except InvalidOperation:
            continue
        if price is None or price <= 0 or kind == "impresora" and price < 2000:
            continue
        parts = urlsplit(link.get("href", ""))
        if parts.scheme != "https" or parts.hostname not in {"www.mercadolibre.com.mx", "articulo.mercadolibre.com.mx"}:
            continue  # Ignore promoted redirect links.
        offer_id = (parse_qs(parts.fragment).get("wid") or [None])[0]
        item_id = re.search(r"/MLM-(\d+)", parts.path)
        product_id = re.search(r"/p/(MLM\d+)", parts.path)
        identifier = offer_id if offer_id and re.fullmatch(r"MLM\d+", offer_id) else (
            f"MLM{item_id.group(1)}" if item_id else product_id.group(1) if product_id else None)
        if not identifier:
            continue
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", f"wid={offer_id}" if offer_id else ""))
        image_url = image.get("src") or image.get("data-src") or ""
        image_parts = urlsplit(image_url)
        if image_parts.scheme != "https" or image_parts.hostname != "http2.mlstatic.com":
            image_url = None
        found[identifier] = {
            "id": f"mercadolibre:{identifier}", "name": name, "category": kind,
            "price": float(price), "currency": "MXN", "store": "Mercado Libre",
            "method": "scraping", "url": url, "image": image_url,
            "price_note": "Precio mostrado; verifica envío y variante",
            "available": None, "availability_note": "Existencia por confirmar",
            "rating": None, "review_count": None,
        }
    return list(found.values())


class SourceBlockedError(ValueError):
    """A marketplace requested account verification or rejected the fetch."""


def _check_mercadolibre_response(response) -> None:
    url = str(getattr(response, "url", ""))
    if response.status_code in {403, 429}:
        raise SourceBlockedError(f"Mercado Libre respondió HTTP {response.status_code}; bloqueó la consulta pública")
    if "account-verification" in url or "suspicious-traffic-frontend" in response.text:
        raise SourceBlockedError("Mercado Libre solicitó verificación o bloqueó la consulta pública")


def _mercadolibre_response(client, url: str, params, timeout: tuple[int, int],
                           browser_fallback: bool = True):
    """Try ordinary HTTP first, then a browser only for an HTTP 403 response."""
    response = client.get(url, params=params, timeout=timeout)
    try:
        _check_mercadolibre_response(response)
    except SourceBlockedError as exc:
        if response.status_code != 403 or not browser_fallback:
            raise
        try:
            return fetch_listing(url)
        except BrowserListingError as browser_error:
            raise SourceBlockedError(f"{exc}; Selenium: {browser_error}") from browser_error
    return response


def _money(text: str) -> Decimal | None:
    match = re.search(r"(?:MX\s*)?\$\s*([\d,]+(?:\.\d{2})?)", text)
    return Decimal(match.group(1).replace(",", "")) if match else None


def _market_price(amount: Decimal) -> float:
    # Esta tienda indica "más IVA"; se usa la tasa general para comparar.
    return float((amount * Decimal("1.16")).quantize(Decimal("0.01")))


def _printer_accessory(name: str) -> bool:
    return any(term in name.casefold() for term in ("enclosure", "gabinete", "cubierta", "repuesto", "refacción", "refaccion", "hotend", "boquilla", "placa", "plataforma"))


def parse_market_listing(markup: str, section: str) -> list[dict]:
    soup = BeautifulSoup(markup, "html.parser")
    found = []
    for card in soup.select(".product.type-product"):
        link = next((a for a in card.select('a[href*="/p/"]') if a.get_text(" ", strip=True)), None)
        price_node = card.select_one(".price ins .amount") or card.select_one(".price .amount")
        if not link or not price_node:
            continue
        name = link.get_text(" ", strip=True)
        kind = "impresora" if section == "impresora" else (material_from_name(name) or ("OTRO" if re.search(r"filament", name, re.I) else None))
        amount = _money(price_node.get_text(" ", strip=True))
        if not kind or kind != "impresora" and _filament_accessory(name) or amount is None or amount <= 0 or (kind == "impresora" and (amount < 2000 or _printer_accessory(name))):
            continue
        img = card.select_one(".product-content-image img")
        image = (img.get("data-lazy-src") or img.get("src")) if img else None
        url = link.get("href", "").split("?")[0]
        found.append({
            "id": "3dmarket:" + url.rstrip("/").split("/")[-1], "name": name,
            "category": kind, "price": _market_price(amount), "listed_price": float(amount),
            "price_note": "Estimado con IVA (precio de tienda + 16%)",
            "currency": "MXN", "store": "3D Market", "method": "scraping",
            "url": url, "image": image, "available": "outofstock" not in card.get("class", []),
            "rating": None, "review_count": None,
        })
    return found


def parse_creality_listing(markup: str, section: str) -> list[dict]:
    soup = BeautifulSoup(markup, "html.parser")
    found = []
    for card in soup.select(".product-item"):
        link = card.select_one("a.item-img[href*='/products/']")
        img = link.select_one("img") if link else None
        price_node = card.select_one(".product-price .price")
        if not link or not price_node:
            continue
        name = (img.get("alt") if img else "") or (card.select_one("a.title") or link).get_text(" ", strip=True)
        kind = "impresora" if section == "impresora" else (material_from_name(name) or ("OTRO" if re.search(r"filament", name, re.I) else None))
        amount = _money(price_node.get_text(" ", strip=True))
        if not kind or kind != "impresora" and _filament_accessory(name) or amount is None or amount <= 0 or (kind == "impresora" and (amount < 2000 or _printer_accessory(name))):
            continue
        url = urljoin("https://store.creality.com", link["href"].split("?")[0])
        found.append({
            "id": "creality:" + url.rstrip("/").split("/")[-1], "name": name,
            "category": kind, "price": float(amount), "price_note": "IVA incluido según tienda",
            "currency": "MXN", "store": "Creality México", "method": "scraping",
            "url": url, "image": img.get("src") if img else None,
            "available": "agotado" not in card.get_text(" ", strip=True).lower(),
            "rating": None, "review_count": None,
        })
    return found


def parse_market_product(markup: str, url: str) -> dict | None:
    soup = BeautifulSoup(markup, "html.parser")
    product = None
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else data.get("@graph", [data])
        product = next((node for node in nodes if isinstance(node, dict) and "Product" in str(node.get("@type"))), None)
        if product:
            break
    if not product:
        return None
    category_urls = [a.get("href", "") for a in soup.select('.product_meta a[href*="/c/"]')]
    name = html.unescape(product.get("name", ""))
    if any("impresoras-3d-mexico" in u for u in category_urls):
        kind = "impresora"
    elif any("filamento" in u for u in category_urls):
        kind = material_from_name(name) or ("OTRO" if re.search(r"filament", name, re.I) else None)
    else:
        return None
    offers = product.get("offers") or []
    offer = offers[0] if isinstance(offers, list) and offers else offers if isinstance(offers, dict) else {}
    specs = offer.get("priceSpecification") or []
    spec = specs[0] if isinstance(specs, list) and specs else specs if isinstance(specs, dict) else {}
    try:
        amount = Decimal(str(spec.get("price") or offer["price"]))
    except (KeyError, InvalidOperation, TypeError):
        return None
    if not kind or kind != "impresora" and _filament_accessory(name) or amount <= 0 or (kind == "impresora" and (amount < 2000 or _printer_accessory(name))):
        return None
    tax_included = spec.get("valueAddedTaxIncluded", True)
    image = product.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    return {"id": "3dmarket:" + url.rstrip("/").split("/")[-1], "name": name,
            "category": kind, "price": float(amount) if tax_included else _market_price(amount),
            "listed_price": float(amount),
            "price_note": "Precio publicado" if tax_included else "Estimado con IVA (precio de tienda + 16%)",
            "currency": "MXN", "store": "3D Market", "method": "scraping",
            "url": url, "image": image, "available": "InStock" in offer.get("availability", ""),
            "rating": None, "review_count": None}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _publish_catalog(path: Path, value: dict) -> None:
    """Readers always see a complete old or new catalog."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".catalog-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _save_raw_response(data_dir: Path, key: str, method: str, response, raw: object | None = None) -> None:
    """Keep one compressed raw capture per source and page for course evidence."""
    extension = "json" if method == "API" else "html"
    path = data_dir / "landing" / "current" / f"{key}.{extension}.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = getattr(response, "content", None)
    if body is None:
        body = (json.dumps(raw, ensure_ascii=False) if method == "API" else response.text).encode("utf-8")
    path.write_bytes(gzip.compress(body, compresslevel=6))


def _ingest_store(store: str, tasks: list[tuple], data_dir: Path, previous: dict,
                  delay: float, client: requests.Session | None = None) -> tuple[dict, list, list]:
    """Process one store sequentially, including its extra API pages."""
    own_client = client is None
    client = client or session()
    products: dict[str, dict] = {}
    sources = []
    failures = []
    browser_fallback = True
    try:
        index = 0
        while index < len(tasks):
            _, kind, method, url, params = tasks[index]
            key = f"{store.replace(' ', '')}_{kind}_{(params or {}).get('page', (params or {}).get('k', 1))}"
            is_filament = kind in {"filamento", "filamentos"}
            allowed = lambda p: (p["category"] != "impresora") if is_filament else p["category"] == "impresora"
            try:
                response = (_mercadolibre_response(client, url, params, (5, 20), browser_fallback) if store == "Mercado Libre"
                            else client.get(url, params=params, timeout=(5, 20)))
                response.raise_for_status()
                if method == "API":
                    raw = response.json()
                    _save_raw_response(data_dir, key, method, response, raw)
                    if not isinstance(raw, list):
                        raise ValueError("La API no devolvió una lista")
                    else:
                        parsed = [p for item in raw if (p := parse_api_product(item, store)) and allowed(p)]
                    if is_filament and params and "category" in params:
                        page = int(params["page"])
                        total_pages = min(int(response.headers.get("X-WP-TotalPages", "1")), 10) if hasattr(response, "headers") else 1
                        if page < total_pages:
                            tasks.append((store, kind, method, url, {**params, "page": page + 1}))
                else:
                    _save_raw_response(data_dir, key, method, response)
                    if store == "Amazon México" and "captcha" in response.text.casefold():
                        raise ValueError("Amazon solicitó verificación; se conserva el catálogo anterior")
                    if store == "3DCity":
                        parsed = [p for p in parse_scrape_products(response.text) if allowed(p)]
                    elif store == "3D Market":
                        parsed = parse_market_listing(response.text, kind)
                    elif store == "Amazon México":
                        parsed = parse_amazon_search(response.text, "filamento" if is_filament else "impresora")
                    elif store == "Mercado Libre":
                        parsed = parse_mercadolibre_search(response.text, "filamento" if is_filament else "impresora")
                        if not parsed:
                            raise ValueError("El listado no devolvió productos reconocibles")
                    else:
                        parsed = parse_creality_listing(response.text, kind)
                if not parsed and any(p["store"] == store and allowed(p) for p in previous["products"]):
                    raise ValueError("La fuente devolvió cero productos reconocidos; se conserva la captura anterior")
                for product in parsed:
                    products[product["id"]] = product
                sources.append({"store": store, "category": kind, "status": "ok", "count": len(parsed),
                                "with_image": sum(bool(p.get("image")) for p in parsed), "http_status": response.status_code})
            except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
                if store == "Mercado Libre" and isinstance(exc, SourceBlockedError):
                    browser_fallback = False
                LOG.warning("Ingesta fallida %s: %s", key, exc)
                failures.append({"store": store, "category": kind, "error": str(exc)})
                cached = [p for p in previous["products"] if p["store"] == store and allowed(p)
                          and (store != "Mercado Libre" or p.get("method") == "scraping")]
                for product in cached:
                    products[product["id"]] = product
                sources.append({"store": store, "category": kind,
                                "status": "blocked" if isinstance(exc, SourceBlockedError) else "stale" if cached else "error",
                                "count": len(cached), "reason": str(exc)})
            index += 1
            if delay and index < len(tasks):
                time.sleep(delay)
    finally:
        if own_client:
            client.close()
    return products, sources, failures


def ingest(data_dir: Path = DATA_DIR, client: requests.Session | None = None, delay: float = 1.0,
           progress: Callable[[str, int, int], None] | None = None) -> dict:
    """Fetch stores in parallel, retain raw data, then publish one validated snapshot."""
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    previous = load_data(data_dir)
    products: dict[str, dict] = {}
    sources = []
    failures = []
    tasks = []
    tasks.extend([
        ("Inovamarket", "impresora", "API", API_URL, {"search": QUERIES["impresora"], "per_page": 24, "page": 1}),
        ("3DCity", "impresora", "scraping", SCRAPE_URL, {"q": QUERIES["impresora"], "type": "product", "page": 1}),
        ("Inovamarket", "filamento", "API", API_URL, {"category": 46, "per_page": 100, "page": 1}),
        ("Shop3D", "filamento", "API", SHOP3D_URL, {"category": 57, "per_page": 100, "page": 1}),
        ("3DCity", "filamento", "scraping", SCRAPE_URL, {"q": QUERIES["filamento"], "type": "product", "page": 1}),
        ("Amazon México", "impresora", "scraping", AMAZON_URL, {"k": "impresora 3d"}),
        ("Amazon México", "filamento", "scraping", AMAZON_URL, {"k": "filamento 3d"}),
        ("Amazon México", "filamento", "scraping", AMAZON_URL, {"k": "filamento ABS 3d"}),
        ("Amazon México", "filamento", "scraping", AMAZON_URL, {"k": "filamento TPU 3d"}),
        ("Amazon México", "filamento", "scraping", AMAZON_URL, {"k": "filamento ASA 3d"}),
    ])
    tasks.extend([
        ("3D Market", "impresora", "scraping", MARKET_PRINTERS, None),
        ("3D Market", "filamentos", "scraping", MARKET_FILAMENTS, None),
        ("Creality México", "impresora", "scraping", CREALITY_PRINTERS, None),
        ("Creality México", "filamentos", "scraping", CREALITY_FILAMENTS, None),
    ])
    tasks.extend([
        ("Mercado Libre", "impresora", "scraping", mercadolibre_search_url("impresora 3d"), None),
        ("Mercado Libre", "filamento", "scraping", mercadolibre_search_url("filamento 3d"), None),
    ])
    groups: dict[str, list[tuple]] = {}
    for task in tasks:
        groups.setdefault(task[0], []).append(task)
    results = {}
    if client is not None:
        for index, (store, store_tasks) in enumerate(groups.items(), 1):
            results[store] = _ingest_store(store, store_tasks, data_dir, previous, delay, client)
            if progress:
                progress(store, index, len(groups))
    else:
        with ThreadPoolExecutor(max_workers=min(7, len(groups))) as pool:
            futures = {pool.submit(_ingest_store, store, store_tasks, data_dir, previous,
                                   delay): store for store, store_tasks in groups.items()}
            for index, future in enumerate(as_completed(futures), 1):
                store = futures[future]
                results[store] = future.result()
                if progress:
                    progress(store, index, len(groups))
    for store in groups:
        store_products, store_sources, store_failures = results[store]
        products.update(store_products)
        sources.extend(store_sources)
        failures.extend(store_failures)
    if previous["products"] and sources and not any(source["status"] == "ok" for source in sources):
        raise RuntimeError("Ninguna fuente respondió; se conserva el catálogo publicado anteriormente")
    # A bulk snapshot does not cover every model found by an explicit search.
    for product in previous["products"]:
        if product.get("search_updated_at") and product["id"] not in products:
            products[product["id"]] = product
    records = sorted(products.values(), key=lambda p: (p["category"], p["price"], p["name"]))
    result = {"run_id": run_id, "updated_at": datetime.now(timezone.utc).isoformat(),
              "count": len(records), "sources": sources, "failures": failures, "products": records,
              "searches": previous.get("searches", {}),
              "last_search_at": previous.get("last_search_at")}
    _publish_catalog(data_dir / "products.json", result)
    _write_json(data_dir / "reports" / "latest.json", {k: v for k, v in result.items() if k != "products"})
    return result


def load_data(data_dir: Path = DATA_DIR) -> dict:
    path = data_dir / "products.json"
    if not path.exists():
        return {"updated_at": None, "count": 0, "sources": [], "failures": [], "products": []}
    stat = path.stat()
    return _read_catalog(str(path.resolve()), stat.st_mtime_ns, stat.st_size)


def save_search_results(query: str, context: str, search_result: dict, data_dir: Path = DATA_DIR) -> dict:
    """Upsert every valid fetched offer into the same durable catalog used by reads."""
    previous = load_data(data_dir)
    now = datetime.now(timezone.utc).isoformat()
    products = {product["id"]: product for product in previous["products"]}
    for product in search_result["products"]:
        products[product["id"]] = {**product, "search_updated_at": now}
    searches = dict(previous.get("searches", {}))
    searches[f"{context}:{query.casefold()}"] = {
        "query": query, "context": context, "searched_at": search_result["searched_at"],
        "count": len(search_result["products"]), "sources": search_result["sources"],
        "failures": search_result["failures"],
    }
    records = sorted(products.values(), key=lambda p: (p["category"], p["price"], p["name"]))
    result = {**previous, "updated_at": now if search_result["products"] else previous["updated_at"],
              "count": len(records), "products": records,
              "searches": searches, "last_search_at": now}
    _publish_catalog(data_dir / "products.json", result)
    return result


@lru_cache(maxsize=8)
def _read_catalog(path: str, modified_ns: int, size: int) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _market_urls(client: requests.Session) -> list[str]:
    global _sitemap_cache
    if _sitemap_cache and time.monotonic() - _sitemap_cache[0] < 3600:
        return _sitemap_cache[1]
    urls = []
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    for sitemap in MARKET_SITEMAPS:
        response = client.get(sitemap, timeout=(3, 8))
        response.raise_for_status()
        root = ET.fromstring(response.content)
        urls.extend(node.text for node in root.findall("s:url/s:loc", ns) if node.text)
    _sitemap_cache = (time.monotonic(), urls)
    return urls


def _market_matches(query: str, urls: list[str]) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", query.casefold())
    matches = [url for url in urls if all(token in url.split("/p/")[-1].casefold() for token in tokens)]
    return sorted(matches, key=lambda url: len(url.split("/p/")[-1]))[:4]


def search_live(query: str, stores: tuple[str, ...] = ("Inovamarket", "3DCity", "Shop3D", "3D Market", "Creality México", "Amazon México", "Mercado Libre"),
                client: requests.Session | None = None, delay: float = 1.0,
                context: str | None = None, capture_dir: Path | None = None) -> dict:
    """Search public sources for one query; optionally retain their raw responses."""
    own_client = client is None
    client = client or live_session()
    now = datetime.now(timezone.utc)
    search_key = hashlib.sha256(f"{context}:{query.casefold()}".encode("utf-8")).hexdigest()[:16]
    products: dict[str, dict] = {}
    failures = []
    sources = []
    for store, method, url, params in (
        ("Inovamarket", "API", API_URL, {"search": query, "per_page": 100, "page": 1}),
        ("3DCity", "scraping", SCRAPE_URL, {"q": query, "type": "product", "page": 1}),
        ("Shop3D", "API", SHOP3D_URL, {"search": query, "per_page": 100, "page": 1}),
        ("Amazon México", "scraping", AMAZON_URL, {"k": query}),
        ("Mercado Libre", "scraping", mercadolibre_search_url(query), None),
    ):
        if store not in stores:
            continue
        try:
            response = (_mercadolibre_response(client, url, params, (3, 8)) if store == "Mercado Libre"
                        else client.get(url, params=params, timeout=(3, 8)))
            response.raise_for_status()
            if method == "API":
                raw = response.json()
                if not isinstance(raw, list):
                    raise ValueError("La API no devolvió una lista")
                else:
                    parsed = [p for item in raw if (p := parse_api_product(item, store))]
                if capture_dir:
                    _save_raw_response(capture_dir, f"search_{search_key}_{store.replace(' ', '')}_1", method, response, raw)
                pages = min(int(getattr(response, "headers", {}).get("X-WP-TotalPages", "1")), 10)
                for page in range(2, pages + 1):
                    page_response = client.get(url, params={**params, "page": page}, timeout=(3, 8))
                    page_response.raise_for_status()
                    page_raw = page_response.json()
                    if not isinstance(page_raw, list):
                        raise ValueError("La API no devolvió una lista")
                    if capture_dir:
                        _save_raw_response(capture_dir, f"search_{search_key}_{store.replace(' ', '')}_{page}", method, page_response, page_raw)
                    parsed.extend(p for item in page_raw if (p := parse_api_product(item, store)))
            else:
                if store == "Amazon México" and "captcha" in response.text.casefold():
                    raise ValueError("Amazon solicitó verificación")
                parsed = (parse_scrape_products(response.text) if store == "3DCity"
                          else parse_mercadolibre_search(response.text, context) if store == "Mercado Libre"
                          else parse_amazon_search(response.text, context))
                if capture_dir:
                    _save_raw_response(capture_dir, f"search_{search_key}_{store.replace(' ', '')}_1", method, response)
            for product in parsed:
                products[product["id"]] = product
            sources.append({"store": store, "status": "ok", "count": len(parsed)})
        except (requests.RequestException, ValueError, KeyError) as exc:
            LOG.warning("Búsqueda fallida en %s: %s", store, exc)
            failures.append({"store": store, "error": str(exc)})
            sources.append({"store": store, "status": "blocked" if isinstance(exc, SourceBlockedError) else "error",
                            "count": 0, "reason": str(exc)})
        if delay:
            time.sleep(delay)
    if "3D Market" in stores and context == "filamento":
        try:
            response = client.get(MARKET_FILAMENTS, timeout=(3, 8))
            response.raise_for_status()
            if capture_dir:
                _save_raw_response(capture_dir, f"search_{search_key}_3DMarket_1", "scraping", response)
            parsed = parse_market_listing(response.text, "filamentos")
            terms = query.casefold().split()
            for product in parsed:
                if all(term in product["name"].casefold() for term in terms):
                    products[product["id"]] = product
            sources.append({"store": "3D Market", "status": "ok",
                            "count": sum(p["store"] == "3D Market" for p in products.values())})
        except (requests.RequestException, ValueError) as exc:
            failures.append({"store": "3D Market", "error": str(exc)})
            sources.append({"store": "3D Market", "status": "error", "count": 0})
    elif "3D Market" in stores and context == "impresora" and len(query.split()) >= 2:
        try:
            for index, url in enumerate(_market_matches(query, _market_urls(client)), 1):
                response = client.get(url, timeout=(3, 8))
                response.raise_for_status()
                if capture_dir:
                    _save_raw_response(capture_dir, f"search_{search_key}_3DMarket_{index}", "scraping", response)
                product = parse_market_product(response.text, url)
                if product:
                    products[product["id"]] = product
                if delay:
                    time.sleep(delay)
            sources.append({"store": "3D Market", "status": "ok",
                            "count": sum(p["store"] == "3D Market" for p in products.values())})
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            failures.append({"store": "3D Market", "error": str(exc)})
            sources.append({"store": "3D Market", "status": "error", "count": 0})
    elif "3D Market" in stores and context == "impresora":
        try:
            response = client.get(MARKET_PRINTERS, timeout=(3, 8))
            response.raise_for_status()
            if capture_dir:
                _save_raw_response(capture_dir, f"search_{search_key}_3DMarket_1", "scraping", response)
            terms = query.casefold().split()
            parsed = [product for product in parse_market_listing(response.text, "impresora")
                      if all(term in product["name"].casefold() for term in terms)]
            products.update({product["id"]: product for product in parsed})
            sources.append({"store": "3D Market", "status": "ok", "count": len(parsed)})
        except (requests.RequestException, ValueError) as exc:
            failures.append({"store": "3D Market", "error": str(exc)})
            sources.append({"store": "3D Market", "status": "error", "count": 0})
    if "Creality México" in stores:
        try:
            section = "impresora" if context == "impresora" else "filamentos"
            response = client.get(CREALITY_PRINTERS if section == "impresora" else CREALITY_FILAMENTS,
                                  timeout=(3, 8))
            response.raise_for_status()
            if capture_dir:
                _save_raw_response(capture_dir, f"search_{search_key}_Creality_1", "scraping", response)
            terms = query.casefold().split()
            parsed = [product for product in parse_creality_listing(response.text, section)
                      if all(term in product["name"].casefold() for term in terms)]
            products.update({product["id"]: product for product in parsed})
            sources.append({"store": "Creality México", "status": "ok", "count": len(parsed)})
        except (requests.RequestException, ValueError) as exc:
            failures.append({"store": "Creality México", "error": str(exc)})
            sources.append({"store": "Creality México", "status": "error", "count": 0})
    if own_client:
        client.close()
    matching_context = [product for product in products.values()
                        if context is None or (product["category"] == "impresora") == (context == "impresora")]
    return {"searched_at": now.isoformat(), "products": matching_context,
            "sources": sources, "failures": failures}
