"""Lectura opcional de listados públicos de Mercado Libre con Selenium."""
from dataclasses import dataclass

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.support.ui import WebDriverWait


class BrowserListingError(RuntimeError):
    """El navegador no pudo entregar un listado público legible."""


@dataclass
class BrowserPage:
    url: str
    text: str
    status_code: int = 200

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8")

    def raise_for_status(self) -> None:
        return None


def fetch_listing(url: str, timeout: int = 12) -> BrowserPage:
    """Abre una página pública; no interactúa con verificaciones de cuenta."""
    options = EdgeOptions()
    options.add_argument("--headless=new")
    options.page_load_strategy = "eager"
    driver = None
    try:
        driver = webdriver.Edge(options=options)
        driver.set_page_load_timeout(timeout)
        driver.get(url)

        def listing_state(browser):
            current_url = browser.current_url.casefold()
            body = browser.find_element(By.TAG_NAME, "body").text.casefold()
            if ("account-verification" in current_url or
                    "hubo un error accediendo" in body or
                    "verifica tu cuenta" in body or
                    "suspicious-traffic" in current_url):
                return "blocked"
            if browser.find_elements(By.CSS_SELECTOR, ".ui-search-layout__item"):
                return "results"
            return False

        state = WebDriverWait(driver, timeout, poll_frequency=0.5).until(listing_state)
        if state == "blocked":
            raise BrowserListingError("Mercado Libre mostró una página de error o verificación")
        return BrowserPage(url=driver.current_url, text=driver.page_source)
    except TimeoutException as exc:
        raise BrowserListingError("Mercado Libre no mostró resultados antes del límite de espera") from exc
    except WebDriverException as exc:
        raise BrowserListingError(f"No se pudo leer el listado con Selenium: {exc.msg[:160]}") from exc
    finally:
        if driver is not None:
            try:
                driver.quit()
            except WebDriverException:
                pass
