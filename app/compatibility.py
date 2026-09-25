"""Compatibilidad por materiales expresamente indicados en la ficha de la impresora."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .pipeline import FILAMENT_MATERIALS

_material_pattern = re.compile(
    r"(?<![A-Z0-9])(?:" + "|".join(re.escape(m) for m in FILAMENT_MATERIALS) +
    r"|NYLON)(?:\d+)?(?![A-Z0-9])", re.I
)
_positive_pattern = re.compile(
    r"(?:materiales? compatibles?|filamentos? compatibles?|"
    r"compatible con (?:materiales?|filamentos?)(?: como)?|"
    r"puede imprimir|admite (?:materiales?|filamentos?))\s*[:\-]?\s*([^.!?]{0,180})",
    re.I,
)


def _materials(text: str) -> list[str]:
    found = []
    for match in _material_pattern.finditer(text.upper()):
        value = match.group().upper()
        canonical = "PA" if value == "NYLON" or re.fullmatch(r"PA\d+", value) else re.sub(r"\d+$", "", value)
        if canonical not in found:
            found.append(canonical)
    return found


def extract_compatibility(markup: str, store: str) -> tuple[list[str], str | None]:
    soup = BeautifulSoup(markup, "html.parser")
    if store == "3DCity":
        for heading in soup.select("th"):
            if re.search(r"materiales? compatibles?|filamentos? compatibles?", heading.get_text(" ", strip=True), re.I):
                row = heading.find_parent("tr")
                cell = row.find("td") if row else None
                if cell:
                    excerpt = cell.get_text(" ", strip=True).split(";")[0]
                    materials = _materials(excerpt)
                    if materials:
                        return materials, f"Materiales compatibles: {excerpt[:170]}"
    selectors = {
        "3D Market": ".woocommerce-Tabs-panel--description",
        "Inovamarket": ".tab-content",
        "Amazon México": "#feature-bullets, #productDescription, #prodDetails",
        "Mercado Libre": ".ui-pdp-description__content",
    }
    scope = soup.select_one(selectors.get(store, ".product__description, .product-description"))
    if not scope:
        return [], None
    text = scope.get_text(" ", strip=True)
    for match in _positive_pattern.finditer(text):
        excerpt = match.group(0)
        excerpt = re.split(r"\b(?:no recomendad[oa]s?|no compatible|excepto|requiere cámara)\b", excerpt, maxsplit=1, flags=re.I)[0]
        materials = _materials(excerpt)
        if materials:
            return materials, excerpt[:180]
    return [], None


def matching_filaments(products: list[dict], materials: list[str]) -> list[dict]:
    return sorted((p for p in products if p["category"] in materials and p["category"] != "impresora"),
                  key=lambda p: (p["price"], p["name"]))
