from app.compatibility import extract_compatibility, matching_filaments


def test_compatibility_ignores_materials_marked_not_recommended():
    markup = '''<table><tr><th>Materiales compatibles</th><td>PLA, PETG, TPU, PVA; ABS/ASA/PC/PA no recomendados (cámara abierta)</td></tr></table>'''
    materials, evidence = extract_compatibility(markup, "3DCity")
    assert materials == ["PLA", "PETG", "TPU", "PVA"]
    assert "ABS" not in evidence


def test_compatibility_matches_catalog_by_declared_material():
    markup = '''<div class="tab-content"><div class="faq-answer">Esta impresora puede imprimir PLA, PETG y TPU, materiales comunes.</div></div>'''
    materials, _ = extract_compatibility(markup, "Inovamarket")
    products = [{"category": "TPU", "price": 350, "name": "Filamento TPU"},
                {"category": "ABS", "price": 100, "name": "Filamento ABS"},
                {"category": "PLA", "price": 250, "name": "Filamento PLA"}]
    assert materials == ["PLA", "PETG", "TPU"]
    assert [p["name"] for p in matching_filaments(products, materials)] == ["Filamento PLA", "Filamento TPU"]
