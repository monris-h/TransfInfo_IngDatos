import csv
import io
import json
from fastapi.testclient import TestClient
from threading import Event
from time import monotonic, sleep

from app import server
from app import pipeline
from fastapi import HTTPException


def test_rate_limit_blocks_repeated_refresh_and_recovers(monkeypatch):
    monkeypatch.setattr(server, "rate_windows", {})
    assert server._rate_wait("one", "/api/refresh", 100) == 0
    assert server._rate_wait("one", "/api/refresh", 101) == 59
    assert server._rate_wait("other", "/api/refresh", 101) == 0
    assert server._rate_wait("one", "/api/refresh", 160) == 0


def test_rate_limit_http_returns_retry_after_and_keeps_leave_available(monkeypatch):
    monkeypatch.setattr(server, "rate_windows", {})
    client = TestClient(server.app)
    for _ in range(20):
        assert client.get("/").status_code == 200
    limited = client.get("/")
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0
    for _ in range(4):
        assert client.post("/api/search/refresh", json={"query": "x", "context": "impresora"}).status_code == 400
    limited = client.post("/api/search/refresh", json={"query": "x", "context": "impresora"})
    assert limited.status_code == 429
    assert limited.json()["retry_after"] > 0
    assert client.post("/api/presence/leave", json={"session_id": "missing"}).status_code == 200


def test_products_returns_local_results_without_live_request(monkeypatch):
    product = {"id": "one", "name": "Bambu Lab P2S", "store": "Shop3D", "category": "impresora",
               "available": True, "price": 100, "url": "https://shop3d.mx/product"}
    monkeypatch.setattr(server, "load_data", lambda: {"products": [product], "updated_at": "2026-09-24T00:00:00Z",
                                                   "count": 1, "sources": [], "failures": []})
    monkeypatch.setattr(server, "search_live", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network called")))
    result = server.products(q="Bambu Lab P2S", category="todas", store="todas", available=False,
                             sort="price_asc", context="impresora")
    assert "needs_live" not in result
    assert result["products"] == [product]


def test_explicit_search_upserts_and_persists_results(monkeypatch, tmp_path):
    product = {"id": "one", "name": "Bambu Lab P2S", "store": "Shop3D", "category": "impresora",
               "available": True, "price": 100, "url": "https://shop3d.mx/product"}
    pipeline._publish_catalog(tmp_path / "products.json", {"products": [product], "updated_at": None,
                                                           "count": 1, "sources": [], "failures": []})
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "load_data", lambda: pipeline.load_data(tmp_path))
    monkeypatch.setattr(server, "save_search_results",
                        lambda query, context, result: pipeline.save_search_results(query, context, result, tmp_path))
    requested = []
    def fake_search(query, stores, context, delay, capture_dir):
        requested.append((query, stores, context, capture_dir))
        return {"products": [{**product, "price": 90}, {**product, "id": "two", "price": 95}],
                "searched_at": "2026-09-24T00:00:00Z", "sources": [{"store": "Shop3D", "status": "ok", "count": 2}],
                "failures": []}
    monkeypatch.setattr(server, "search_live", fake_search)
    refreshed = server.refresh_search(server.SearchRefresh(query="Bambu Lab P2S", context="impresora", store="Shop3D"))
    assert refreshed["saved"] == 2
    assert requested == [("Bambu Lab P2S", ("Shop3D",), "impresora", tmp_path)]
    result = server.products(q="Bambu Lab P2S", category="todas", store="todas", available=False,
                             sort="price_asc", context="impresora")
    assert [p["price"] for p in result["products"]] == [90, 95]
    assert pipeline.load_data(tmp_path)["searches"]["impresora:bambu lab p2s"]["count"] == 2


def test_explicit_search_rejects_short_queries():
    try:
        server.refresh_search(server.SearchRefresh(query="A", context="impresora"))
        assert False, "A broad one-character query should be rejected"
    except HTTPException as exc:
        assert exc.status_code == 400


def test_products_pages_filtered_offers_and_returns_facets(monkeypatch):
    products = [
        {"id": "p", "name": "Impresora 3D", "store": "3DCity", "category": "impresora", "available": True, "price": 5000},
        {"id": "a", "name": "Filamento ABS", "store": "3DCity", "category": "ABS", "available": True, "price": 300},
        {"id": "b", "name": "Filamento PLA", "store": "Shop3D", "category": "PLA", "available": True, "price": 200},
        {"id": "c", "name": "Filamento PETG", "store": "Shop3D", "category": "PETG", "available": False, "price": 250},
    ]
    monkeypatch.setattr(server, "load_data", lambda: {"products": products, "updated_at": None,
                                                   "count": 4, "sources": [], "failures": []})
    first = server.products(q="", category="filamento", store="todas", available=False,
                            sort="price_asc", context="filamento", limit=1, offset=0)
    second = server.products(q="", category="filamento", store="todas", available=False,
                             sort="price_asc", context="filamento", limit=1, offset=1)
    assert (first["total"], first["printer_count"], first["filament_count"]) == (3, 1, 3)
    assert first["materials"] == ["ABS", "PETG", "PLA"]
    assert [first["products"][0]["id"], second["products"][0]["id"]] == ["b", "c"]
    filtered = server.products(q="", category="PLA", store="todas", available=True,
                               sort="price_asc", context="filamento", limit=16, offset=0)
    assert filtered["total"] == 1
    assert filtered["products"][0]["id"] == "b"


def test_csv_exports_all_filtered_offers_from_saved_catalog(monkeypatch):
    products = [
        {"id": f"p{i}", "name": f"Impresora {i}", "store": "3DCity", "category": "impresora",
         "available": True, "price": 100 + i, "url": f"https://example.com/{i}", "image": ""}
        for i in range(20)
    ]
    products += [{"id": "filament", "name": "=SUM(1,2)", "store": "3DCity", "category": "PLA",
                  "available": None, "price": 300, "url": "https://example.com/filament", "image": ""}]
    monkeypatch.setattr(server, "load_data", lambda: {"products": products, "updated_at": "2026-09-25T00:00:00Z",
                                                   "count": len(products), "sources": [], "failures": []})
    monkeypatch.setattr(server, "search_live", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network called")))
    response = server.export_csv(q="Impresora", category="impresora", store="3DCity", available=True,
                                 sort="price_desc", context="impresora")
    assert response.body.startswith(b"\xef\xbb\xbf")
    assert "attachment; filename=" in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.body.decode("utf-8-sig"))))
    assert len(rows) == 20
    assert [row["precio_mxn"] for row in rows[:2]] == ["119", "118"]
    assert all(row["categoria"] == "impresora" for row in rows)
    filament_response = server.export_csv(q="", category="PLA", store="todas", available=False,
                                          sort="name", context="filamento")
    filament = next(csv.DictReader(io.StringIO(filament_response.body.decode("utf-8-sig"))))
    assert filament["nombre"] == "'=SUM(1,2)"
    assert filament["disponibilidad"] == "sin confirmar"


def test_refresh_returns_immediately_and_reports_progress(monkeypatch):
    started = Event()
    release = Event()
    calls = []

    def fake_ingest(progress):
        calls.append(1)
        started.set()
        release.wait(timeout=2)
        progress("3DCity", 1, 2)
        progress("Amazon México", 2, 2)
        return {"count": 42, "failures": []}

    monkeypatch.setattr(server, "ingest", fake_ingest)
    try:
        response = server.refresh()
        assert response["status"] == "running"
        assert started.wait(timeout=1)
        assert server.refresh()["status"] == "running"
        assert len(calls) == 1
    finally:
        release.set()
    deadline = monotonic() + 2
    while server.refresh_status()["status"] == "running" and monotonic() < deadline:
        sleep(0.01)
    status = server.refresh_status()
    assert status["status"] == "complete"
    assert (status["completed_stores"], status["total_stores"], status["count"]) == (2, 2, 42)


def test_presence_counts_sessions_and_expires_inactive_users(monkeypatch):
    monkeypatch.setattr(server, "presence_sessions", {})
    now = [100.0]
    monkeypatch.setattr(server, "monotonic", lambda: now[0])
    first = server.join_presence(server.PresenceJoin(username=" Ana "))
    second = server.join_presence(server.PresenceJoin(username="Luis"))
    assert (first["username"], second["count"]) == ("Ana", 2)
    assert [user["username"] for user in server.get_presence()["users"]] == ["Ana", "Luis"]
    now[0] += 20
    server.heartbeat_presence(server.PresenceSession(session_id=first["session_id"]))
    now[0] += 16
    assert server.get_presence()["count"] == 1
    assert server.get_presence()["users"][0]["username"] == "Ana"
    assert server.leave_presence(server.PresenceSession(session_id=first["session_id"]))["count"] == 0


def test_presence_stream_emits_joins_and_leaves_immediately(monkeypatch):
    monkeypatch.setattr(server, "presence_sessions", {})
    stream = server._presence_stream()
    try:
        assert json.loads(next(stream).removeprefix("data: "))["count"] == 0
        joined = server.join_presence(server.PresenceJoin(username="Ana"))
        assert json.loads(next(stream).removeprefix("data: "))["users"][0]["username"] == "Ana"
        server.leave_presence(server.PresenceSession(session_id=joined["session_id"]))
        assert json.loads(next(stream).removeprefix("data: "))["count"] == 0
    finally:
        stream.close()


def test_reload_reuses_presence_and_ignores_late_leave(monkeypatch):
    monkeypatch.setattr(server, "presence_sessions", {})
    first = server.join_presence(server.PresenceJoin(username="Yerik", view_id="page-1"))
    reloaded = server.join_presence(server.PresenceJoin(
        username="Yerik", session_id=first["session_id"], view_id="page-2"))
    assert reloaded["session_id"] == first["session_id"]
    assert reloaded["count"] == 1
    assert server.leave_presence(server.PresenceSession(
        session_id=first["session_id"], view_id="page-1"))["count"] == 1
    assert server.heartbeat_presence(server.PresenceSession(
        session_id=first["session_id"], view_id="page-2"))["count"] == 1
    assert server.leave_presence(server.PresenceSession(
        session_id=first["session_id"], view_id="page-2"))["count"] == 0


def test_same_name_is_listed_once_even_if_old_tab_session_lingers(monkeypatch):
    monkeypatch.setattr(server, "presence_sessions", {})
    first = server.join_presence(server.PresenceJoin(username="Yerik Laptop"))
    second = server.join_presence(server.PresenceJoin(username="yerik laptop"))
    assert second["count"] == 1
    assert len(second["users"]) == 1
    assert server.leave_presence(server.PresenceSession(session_id=first["session_id"]))["count"] == 1
    assert server.leave_presence(server.PresenceSession(session_id=second["session_id"]))["count"] == 0


def test_presence_rejects_blank_name_and_expired_session(monkeypatch):
    monkeypatch.setattr(server, "presence_sessions", {})
    try:
        server.join_presence(server.PresenceJoin(username="  "))
        assert False, "Blank username should be rejected"
    except HTTPException as exc:
        assert exc.status_code == 400
    try:
        server.heartbeat_presence(server.PresenceSession(session_id="missing"))
        assert False, "Unknown session should be rejected"
    except HTTPException as exc:
        assert exc.status_code == 404
