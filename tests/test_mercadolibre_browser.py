import pytest

from app import mercadolibre_browser as browser


class FakeDriver:
    def __init__(self, blocked=False):
        self.blocked = blocked
        self.current_url = "https://listado.mercadolibre.com.mx/impresora-3d"
        self.page_source = '<li class="ui-search-layout__item">Oferta</li>'
        self.closed = False

    def set_page_load_timeout(self, timeout):
        self.timeout = timeout

    def get(self, url):
        self.current_url = url

    def find_element(self, by, selector):
        return type("Body", (), {"text": "Hubo un error accediendo a esta pagina" if self.blocked else "Ofertas"})()

    def find_elements(self, by, selector):
        return [] if self.blocked else [object()]

    def quit(self):
        self.closed = True


def test_browser_listing_waits_for_cards_and_closes_driver(monkeypatch):
    driver = FakeDriver()
    monkeypatch.setattr(browser.webdriver, "Edge", lambda options: driver)
    page = browser.fetch_listing(driver.current_url)
    assert page.status_code == 200
    assert b"ui-search-layout__item" in page.content
    assert driver.closed


def test_browser_listing_rejects_error_page_and_closes_driver(monkeypatch):
    driver = FakeDriver(blocked=True)
    monkeypatch.setattr(browser.webdriver, "Edge", lambda options: driver)
    with pytest.raises(browser.BrowserListingError, match="error o verificación"):
        browser.fetch_listing(driver.current_url)
    assert driver.closed
