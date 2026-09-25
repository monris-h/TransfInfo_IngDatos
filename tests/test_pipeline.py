import gzip
from threading import Lock
from time import sleep
import pytest
import requests

from app import pipeline
from app.mercadolibre_browser import BrowserPage
from app.pipeline import (_publish_catalog, category, ingest, load_data, parse_api_product, parse_creality_listing,
                          parse_market_listing, parse_market_product, parse_scrape_products,
                          parse_amazon_search, parse_mercadolibre_search, search_live)


def test_api_price_uses_currency_minor_unit():
    item = {"id": 7, "name": "Filamento PETG 1 kg", "prices": {"price": "34900", "currency_minor_unit": 2, "currency_code": "MXN"}, "is_in_stock": True, "average_rating": "4.5", "review_count": 2}
    product = parse_api_product(item)
    assert product["price"] == 349
    assert product["category"] == "PETG"
    assert product["rating"] == 4.5


def test_shop3d_api_uses_material_category_and_image():
    item = {"id": 9, "name": "PETG – Gris", "categories": [{"slug": "petg"}],
            "prices": {"price": "49900", "currency_minor_unit": 2, "currency_code": "MXN"},
            "images": [{"src": "https://shop3d.mx/petg.jpg"}], "is_in_stock": True}
    product = parse_api_product(item, "Shop3D")
    assert (product["category"], product["price"], product["image"]) == ("PETG", 499, "https://shop3d.mx/petg.jpg")


def test_api_uses_store_category_for_model_only_name():
    item = {"id": 8, "name": "Bambu Lab P2S", "categories": [{"slug": "impresora-filamento"}],
            "prices": {"price": "14689", "currency_minor_unit": 0, "currency_code": "MXN"},
            "is_in_stock": True}
    product = parse_api_product(item)
    assert product["category"] == "impresora"
    assert product["price"] == 14689


def test_scraper_extracts_card_and_ignores_unrelated_products():
    markup = '''<ul><li class="product-grid__item"><a href="/products/pla-azul">Filamento PLA Azul 1 kg</a><div class="price__regular"><span class="price-item">$ 299.00</span></div></li><li class="product-grid__item"><a href="/products/boquilla">Boquilla de impresora 3D</a><span class="price-item">$ 45.00</span></li></ul>'''
    products = parse_scrape_products(markup)
    assert len(products) == 1
    assert products[0]["price"] == 299
    assert products[0]["url"] == "https://www.3dcity.com.mx/products/pla-azul"
    assert category("Kit de ruedas para impresora 3D") is None
    assert category("Resistencia de 12v 40w para Impresora 3D") is None
    assert category("Hotend Anycubic Kobra Impresora 3D") is None
    assert category("Impresora 3D Bambu Lab A1") == "impresora"


def test_3dcity_live_search_does_not_persist_raw_data(monkeypatch):
    markup = '''<html><nav>Facebook Instagram</nav><ul><li class="product-grid__item"><a href="/products/pla-azul">Filamento PLA Azul 1 kg</a><div class="card__media"><img src="/pla.jpg"></div><div class="price__regular"><span class="price-item">$ 299.00</span></div><script>tracking()</script></li></ul><footer>Redes sociales</footer></html>'''
    class Response:
        text = markup
        def raise_for_status(self):
            pass
    class Client:
        def get(self, url, params, timeout):
            return Response()
    monkeypatch.setattr("app.pipeline._save_raw_response", lambda *args: (_ for _ in ()).throw(AssertionError("raw saved")))
    monkeypatch.setattr("app.pipeline._write_json", lambda *args: (_ for _ in ()).throw(AssertionError("json saved")))
    result = search_live("PLA azul", stores=("3DCity",), client=Client(), delay=0)
    assert len(result["products"]) == 1
    assert result["products"][0]["price"] == 299


def test_explicit_search_captures_raw_response_and_replaces_price(tmp_path):
    class Response:
        text = '''<li class="product-grid__item"><a href="/products/pla-azul">Filamento PLA Azul 1 kg</a><div class="price__regular"><span class="price-item">$299.00</span></div></li>'''
        def raise_for_status(self):
            pass

    class Client:
        def get(self, url, params, timeout):
            return Response()

    found = search_live("PLA Azul", stores=("3DCity",), client=Client(), delay=0,
                        context="filamento", capture_dir=tmp_path)
    saved = pipeline.save_search_results("PLA Azul", "filamento", found, tmp_path)
    assert saved["count"] == 1
    captures = list((tmp_path / "landing" / "current").glob("search_*.html.gz"))
    assert len(captures) == 1
    assert b"Filamento PLA Azul" in gzip.decompress(captures[0].read_bytes())
    newer = {**found, "products": [{**found["products"][0], "price": 279}]}
    pipeline.save_search_results("PLA Azul", "filamento", newer, tmp_path)
    assert load_data(tmp_path)["count"] == 1
    assert load_data(tmp_path)["products"][0]["price"] == 279


def test_catalog_is_published_as_one_snapshot_and_cache_reloads(tmp_path):
    path = tmp_path / "products.json"
    first = {"updated_at": "first", "count": 1, "sources": [], "failures": [], "products": [{"id": "one"}]}
    second = {"updated_at": "second", "count": 2, "sources": [], "failures": [], "products": [{"id": "two"}, {"id": "three"}]}
    _publish_catalog(path, first)
    assert load_data(tmp_path) == first
    assert path.read_text(encoding="utf-8").startswith('{\n  "updated_at"')
    _publish_catalog(path, second)
    assert load_data(tmp_path) == second
    assert not list(tmp_path.glob('*.tmp'))


def test_targeted_search_survives_later_bulk_ingest(tmp_path, monkeypatch):
    product = {"id": "shop3d:special", "name": "Filamento PETG especial", "store": "Shop3D",
               "category": "PETG", "price": 420, "available": True}
    pipeline.save_search_results("PETG especial", "filamento", {
        "searched_at": "2026-09-25T00:00:00Z", "products": [product],
        "sources": [{"store": "Shop3D", "status": "ok", "count": 1}], "failures": []}, tmp_path)
    assert load_data(tmp_path)["products"][0]["search_updated_at"]
    monkeypatch.setattr(pipeline, "_ingest_store", lambda *args, **kwargs: ({}, [], []))
    ingest(tmp_path, delay=0)
    saved = load_data(tmp_path)
    assert saved["count"] == 1
    assert saved["products"][0]["id"] == product["id"]
    assert "filamento:petg especial" in saved["searches"]


def test_market_listing_adds_iva_and_image():
    markup = '''<div class="product type-product instock"><a href="https://www.3dmarket.mx/p/k1-max-creality/">K1 Max Creality</a><a class="product-content-image"><img data-lazy-src="https://www.3dmarket.mx/k1.webp"></a><span class="price"><span class="amount">$ 14,482.00</span></span></div>'''
    product = parse_market_listing(markup, "impresora")[0]
    assert product["price"] == 16799.12
    assert product["image"] == "https://www.3dmarket.mx/k1.webp"


def test_market_product_uses_product_category():
    markup = '''<html><div class="product_meta"><a href="https://www.3dmarket.mx/c/impresoras-3d-mexico/">Impresoras</a></div><script type="application/ld+json">{"@type":"Product","name":"K1 Max Creality","image":"https://x.test/k1.webp","offers":[{"availability":"https://schema.org/InStock","priceSpecification":[{"price":"14482.00","valueAddedTaxIncluded":false}]}]}</script></html>'''
    product = parse_market_product(markup, "https://www.3dmarket.mx/p/k1-max-creality/")
    assert product["category"] == "impresora"
    assert product["price"] == 16799.12


def test_creality_listing_uses_sale_price_and_image():
    markup = '''<div class="product-item"><a class="item-img" href="/mx/products/k1-max"><img alt="K1 Max 2025 Impresora 3D" src="https://cdn.test/k1.webp"></a><div class="product-price"><s>MX$21,199.00</s><span class="price">MX$11,499.00</span></div></div>'''
    product = parse_creality_listing(markup, "impresora")[0]
    assert product["price"] == 11499
    assert product["image"] == "https://cdn.test/k1.webp"


def test_failed_source_keeps_previous_snapshot(tmp_path):
    class Response:
        status_code = 200
        text = '<li class="product-grid__item"><a href="/products/pla">Filamento PLA 1 kg</a><div class="price__regular"><span class="price-item">$ 250.00</span></div></li>'
        def raise_for_status(self):
            pass
        def json(self):
            return [{"id": 1, "name": "Filamento PLA 1 kg", "prices": {"price": "300", "currency_minor_unit": 0, "currency_code": "MXN"}}]

    class Client:
        fail = False
        empty_city = False
        def get(self, url, params, timeout):
            if self.fail and "inovamarket" in url:
                raise requests.ConnectionError("offline")
            response = Response()
            if self.empty_city and "3dcity" in url:
                response.text = "<html><body>Sin tarjetas reconocibles</body></html>"
            return response

    client = Client()
    first = ingest(tmp_path, client, delay=0)
    assert first["count"] == 3
    captures = list((tmp_path / "landing" / "current").glob("3DCity_*.html.gz"))
    assert captures
    assert b"product-grid__item" in gzip.decompress(captures[0].read_bytes())
    assert (tmp_path / "reports" / "latest.json").exists()
    client.fail = True
    client.empty_city = True
    second = ingest(tmp_path, client, delay=0)
    assert second["count"] == 3
    assert any(source["store"] == "3DCity" and source["status"] == "stale" for source in second["sources"])
    assert len(second["failures"]) == 5


def test_ingest_runs_independent_stores_concurrently(tmp_path, monkeypatch):
    lock = Lock()
    active = 0
    peak = 0
    updates = []

    def fake_store(store, tasks, data_dir, previous, delay, client=None):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        sleep(0.03)
        with lock:
            active -= 1
        return {}, [{"store": store, "status": "ok", "count": 0}], []

    monkeypatch.setattr(pipeline, "_ingest_store", fake_store)
    result = ingest(tmp_path, delay=0, progress=lambda store, done, total: updates.append((store, done, total)))
    assert peak >= 2
    assert len(updates) == 7
    assert [done for _, done, _ in updates] == list(range(1, 8))
    assert result["count"] == 0


def test_total_source_outage_preserves_published_catalog(tmp_path, monkeypatch):
    original = {"updated_at": "before", "count": 1, "sources": [], "failures": [],
                "products": [{"id": "saved", "name": "Impresora guardada", "category": "impresora",
                              "store": "3DCity", "price": 5000}]}
    _publish_catalog(tmp_path / "products.json", original)
    monkeypatch.setattr(pipeline, "_ingest_store", lambda store, *args, **kwargs: (
        {}, [{"store": store, "status": "error", "count": 0}], [{"store": store, "error": "offline"}]))
    with pytest.raises(RuntimeError, match="Ninguna fuente respondió"):
        ingest(tmp_path, delay=0)
    assert load_data(tmp_path) == original


def test_filament_materials_beyond_pla_petg():
    assert category("Filamento ABS 1 kg") == "ABS"
    assert category("Filamento PA6-CF Polymaker") == "PA-CF"
    assert category("Filamento TPU flexible") == "TPU"
    assert category("Filamento técnico sin material") == "OTRO"
    item = {"id": 11, "name": "Filamento de limpieza 1 kg", "categories": [{"slug": "filamentos"}],
            "prices": {"price": "25000", "currency_minor_unit": 2, "currency_code": "MXN"}}
    assert parse_api_product(item) is None


def test_amazon_search_extracts_price_image_and_deduplicates_asin():
    markup = '''<div data-component-type="s-search-result" data-asin="B012345678"><h2>Filamento ABS 1 kg</h2><span class="a-price"><span class="a-offscreen">$399.00</span></span><img class="s-image" src="https://images-na.ssl-images-amazon.com/abs.jpg"></div>'''
    products = parse_amazon_search(markup + markup, "filamento")
    assert len(products) == 1
    assert (products[0]["category"], products[0]["price"], products[0]["image"]) == ("ABS", 399, "https://images-na.ssl-images-amazon.com/abs.jpg")
    assert products[0]["url"] == "https://www.amazon.com.mx/dp/B012345678"
    pen_markup = markup.replace("B012345678", "B012345679").replace("Filamento ABS 1 kg", "Filamentos para bolígrafo 3D de baja temperatura")
    assert parse_amazon_search(pen_markup, "filamento") == []


def test_mercadolibre_scraper_extracts_current_price_image_and_offer():
    markup = '''
    <li class="ui-search-layout__item"><img class="poly-component__picture" src="https://http2.mlstatic.com/pla.webp">
      <h3><a class="poly-component__title" href="https://www.mercadolibre.com.mx/filamento-pla/p/MLM123456#position=1&wid=MLM987654">Filamento PLA 1 kg</a></h3>
      <div class="poly-price__current"><span class="andes-money-amount"><span class="andes-money-amount__fraction">1,234</span><span class="andes-money-amount__cents">50</span></span></div>
      <div class="poly-buy-box__alternative-option"><span class="andes-money-amount__fraction">999</span></div>
    </li>
    <li class="ui-search-layout__item"><img class="poly-component__picture" src="https://http2.mlstatic.com/ad.webp">
      <h3><a class="poly-component__title" href="https://click1.mercadolibre.com.mx/track">Filamento PETG 1 kg</a></h3>
      <div class="poly-price__current"><span class="andes-money-amount"><span class="andes-money-amount__fraction">500</span></span></div>
    </li>'''
    product = parse_mercadolibre_search(markup, "filamento")[0]
    assert (product["store"], product["category"], product["price"], product["method"]) == ("Mercado Libre", "PLA", 1234.5, "scraping")
    assert product["id"] == "mercadolibre:MLM987654"
    assert product["image"] == "https://http2.mlstatic.com/pla.webp"
    assert product["url"] == "https://www.mercadolibre.com.mx/filamento-pla/p/MLM123456#wid=MLM987654"
    assert product["available"] is None
    assert len(parse_mercadolibre_search(markup, "filamento")) == 1


def test_mercadolibre_scraper_accepts_printers_and_rejects_accessories():
    markup = '''
    <li class="ui-search-layout__item"><img class="poly-component__picture" src="https://http2.mlstatic.com/a.webp">
      <h3><a class="poly-component__title" href="https://articulo.mercadolibre.com.mx/MLM-123456-impresora-_JM">Impresora 3D Bambu Lab A1 Mini</a></h3>
      <div class="poly-price__current"><span class="andes-money-amount"><span class="andes-money-amount__fraction">8,399</span></span></div>
    </li>
    <li class="ui-search-layout__item"><img class="poly-component__picture" src="https://http2.mlstatic.com/b.webp">
      <h3><a class="poly-component__title" href="https://articulo.mercadolibre.com.mx/MLM-123457-fep-_JM">Película FEP para impresora 3D</a></h3>
      <div class="poly-price__current"><span class="andes-money-amount"><span class="andes-money-amount__fraction">499</span></span></div>
    </li>'''
    products = parse_mercadolibre_search(markup, "impresora")
    assert len(products) == 1
    assert (products[0]["category"], products[0]["price"], products[0]["id"]) == (
        "impresora", 8399, "mercadolibre:MLM123456")


def test_mercadolibre_scraper_reports_verification_without_token(tmp_path):
    class Response:
        status_code = 200
        url = "https://www.mercadolibre.com.mx/gz/account-verification"
        text = "<html>Verifica tu cuenta</html>"

    class Client:
        def get(self, url, params, timeout):
            return Response()

    tasks = [("Mercado Libre", "impresora", "scraping", pipeline.mercadolibre_search_url("impresora 3d"), None)]
    products, sources, failures = pipeline._ingest_store(
        "Mercado Libre", tasks, tmp_path, {"products": []}, 0, Client())
    assert not products
    assert sources[0]["status"] == "blocked"
    assert "verificación" in failures[0]["error"]


def test_mercadolibre_scraper_reports_http_block(tmp_path, monkeypatch):
    class Response:
        status_code = 403
        url = "https://listado.mercadolibre.com.mx/impresora-3d"
        text = "Acceso denegado"

    class Client:
        def get(self, url, params, timeout):
            return Response()

    monkeypatch.setattr(pipeline, "fetch_listing", lambda url: (_ for _ in ()).throw(
        pipeline.BrowserListingError("Página de error")))
    tasks = [("Mercado Libre", "impresora", "scraping", pipeline.mercadolibre_search_url("impresora 3d"), None)]
    products, sources, failures = pipeline._ingest_store(
        "Mercado Libre", tasks, tmp_path, {"products": []}, 0, Client())
    assert not products
    assert sources[0]["status"] == "blocked"
    assert "HTTP 403" in failures[0]["error"]


def test_mercadolibre_uses_browser_html_after_http_403(tmp_path, monkeypatch):
    markup = '''<li class="ui-search-layout__item"><img class="poly-component__picture" src="https://http2.mlstatic.com/impresora.webp">
      <h2 class="poly-box poly-component__title"><a href="https://articulo.mercadolibre.com.mx/MLM-123456-impresora-_JM">Impresora 3D Bambu Lab A1 Mini</a></h2>
      <div class="poly-price__current"><span role="img" aria-label="$ 8,399.00"></span></div></li>'''
    class Response:
        status_code = 403
        url = "https://listado.mercadolibre.com.mx/impresora-3d"
        text = "Acceso denegado"

    class Client:
        def get(self, url, params, timeout):
            return Response()

    monkeypatch.setattr(pipeline, "fetch_listing", lambda url: BrowserPage(url, markup))
    tasks = [("Mercado Libre", "impresora", "scraping", pipeline.mercadolibre_search_url("impresora 3d"), None)]
    products, sources, failures = pipeline._ingest_store(
        "Mercado Libre", tasks, tmp_path, {"products": []}, 0, Client())
    assert not failures
    assert sources[0]["status"] == "ok"
    assert products["mercadolibre:MLM123456"]["price"] == 8399
    assert list((tmp_path / "landing" / "current").glob("MercadoLibre_*.html.gz"))
