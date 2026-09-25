"""API de lectura y actualización manual para la interfaz responsive."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import csv
import io
import json
import logging
import secrets
from threading import Condition, Lock, Thread
from time import monotonic
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .compatibility import extract_compatibility, matching_filaments
from .pipeline import DATA_DIR, FILAMENT_MATERIALS, ingest, load_data, save_search_results, search_live, session

ROOT = Path(__file__).resolve().parents[1]
app = FastAPI(title="Compara3D", version="1.0.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
refresh_lock = Lock()
refresh_state_lock = Lock()
refresh_state = {"status": "idle", "completed_stores": 0, "total_stores": 0,
                 "last_store": None, "started_at": None, "finished_at": None,
                 "count": None, "failures": [], "error": None}
LOG = logging.getLogger(__name__)
compatibility_cache = {}
STORES = {"todas", "Inovamarket", "3DCity", "Shop3D", "3D Market", "Creality México", "Amazon México", "Mercado Libre"}
PRESENCE_TTL = 35
presence_lock = Lock()
presence_condition = Condition(presence_lock)
presence_sessions: dict[str, dict] = {}
presence_revision = 0


class PresenceJoin(BaseModel):
    username: str
    session_id: str | None = None


class PresenceSession(BaseModel):
    session_id: str


class SearchRefresh(BaseModel):
    query: str
    context: str
    store: str = "todas"


def _presence_data(now: float) -> dict:
    """Call while holding presence_lock."""
    expired = False
    for session_id, user in list(presence_sessions.items()):
        if now - user["last_seen"] > PRESENCE_TTL:
            del presence_sessions[session_id]
            expired = True
    if expired:
        _presence_changed()
    users = sorted(presence_sessions.values(), key=lambda user: user["joined_at"])
    return {"count": len(users),
            "users": [{"username": user["username"], "joined_at": user["joined_at"]}
                      for user in users]}


def _presence_changed() -> None:
    """Notify all open presence streams while holding presence_lock."""
    global presence_revision
    presence_revision += 1
    presence_condition.notify_all()


def _presence_stream():
    last_revision = -1
    while True:
        with presence_condition:
            data = _presence_data(monotonic())
            if presence_revision == last_revision:
                presence_condition.wait(timeout=5)
                data = _presence_data(monotonic())
            changed = presence_revision != last_revision
            last_revision = presence_revision
        yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n" if changed else ": keepalive\n\n"


@app.get("/api/presence/events")
def presence_events():
    return StreamingResponse(_presence_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/presence")
def get_presence():
    with presence_lock:
        return _presence_data(monotonic())


@app.post("/api/presence/join")
def join_presence(body: PresenceJoin):
    username = body.username.strip()
    if not username or len(username) > 32 or any(ord(char) < 32 for char in username):
        raise HTTPException(400, "Escribe un nombre de usuario de hasta 32 caracteres")
    with presence_lock:
        now = monotonic()
        _presence_data(now)
        session_id = body.session_id
        if not session_id or session_id not in presence_sessions or presence_sessions[session_id]["username"] != username:
            session_id = secrets.token_urlsafe(24)
            joined_at = datetime.now(timezone.utc).isoformat()
        else:
            joined_at = presence_sessions[session_id]["joined_at"]
        is_new = session_id not in presence_sessions
        presence_sessions[session_id] = {"username": username, "joined_at": joined_at, "last_seen": now}
        if is_new:
            _presence_changed()
        return {"session_id": session_id, "username": username, **_presence_data(now)}


@app.post("/api/presence/heartbeat")
def heartbeat_presence(body: PresenceSession):
    with presence_lock:
        now = monotonic()
        _presence_data(now)
        if body.session_id not in presence_sessions:
            raise HTTPException(404, "Sesión vencida")
        presence_sessions[body.session_id]["last_seen"] = now
        return _presence_data(now)


@app.post("/api/presence/leave")
def leave_presence(body: PresenceSession):
    with presence_lock:
        if presence_sessions.pop(body.session_id, None) is not None:
            _presence_changed()
        return _presence_data(monotonic())


@app.get("/")
def home():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/products")
def products(q: str = Query("", max_length=100), category: str = Query("todas"),
             store: str = Query("todas"), available: bool = False,
             sort: str = Query("price_asc"), context: str = Query("impresora"),
             limit: int = 16, offset: int = 0):
    if not 1 <= limit <= 100 or not 0 <= offset <= 10000:
        raise HTTPException(400, "Paginación inválida")
    data, candidates, matched, entries = _filtered_catalog(q, category, store, available, sort, context)
    printers = sum(p["category"] == "impresora" for p in matched)
    filaments = len(matched) - printers
    return {"updated_at": data["updated_at"], "catalog_count": data["count"],
            "total": len(entries), "offset": offset,
            "printer_count": printers, "filament_count": filaments,
            "materials": sorted({p["category"] for p in candidates.values() if p["category"] != "impresora"}),
            "sources": data["sources"], "failures": data["failures"],
            "products": entries[offset:offset + limit]}


def _filtered_catalog(q: str, category: str, store: str, available: bool, sort: str, context: str):
    if category not in {"todas", "impresora", "filamento", "OTRO", *FILAMENT_MATERIALS}:
        raise HTTPException(400, "Categoría inválida")
    if sort not in {"price_asc", "price_desc", "name"}:
        raise HTTPException(400, "Orden inválido")
    if context not in {"impresora", "filamento"}:
        raise HTTPException(400, "Contexto inválido")
    if store not in STORES:
        raise HTTPException(400, "Tienda inválida")
    data = load_data()
    candidates = {p["id"]: p for p in data["products"]}
    terms = q.casefold().split()
    def matches(product):
        return ((store == "todas" or product["store"] == store) and
                (not available or product["available"]) and
                all(term in (product["name"] + " " + product["store"]).casefold() for term in terms))
    matched = [p for p in candidates.values() if matches(p)]
    entries = [p for p in matched if (category == "todas" or
               (category == "filamento" and p["category"] != "impresora") or p["category"] == category)]
    entries.sort(key=(lambda p: (p["name"].casefold(), p["id"])) if sort == "name" else
                 (lambda p: (p["price"], p["id"])), reverse=sort == "price_desc")
    return data, candidates, matched, entries


def _csv_text(value):
    """Keep untrusted store text from being evaluated as a spreadsheet formula."""
    value = "" if value is None else str(value)
    if value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


@app.get("/api/export.csv")
def export_csv(q: str = Query("", max_length=100), category: str = Query("todas"),
               store: str = Query("todas"), available: bool = False,
               sort: str = Query("price_asc"), context: str = Query("impresora")):
    data, _, _, entries = _filtered_catalog(q, category, store, available, sort, context)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("id", "nombre", "categoria", "tienda", "precio_mxn", "disponibilidad",
                     "metodo", "url", "imagen", "nota_precio", "actualizado_en"))
    for product in entries:
        availability = {True: "disponible", False: "no disponible", None: "sin confirmar"}[product.get("available")]
        writer.writerow((_csv_text(product.get("id")), _csv_text(product.get("name")),
                         _csv_text(product.get("category")), _csv_text(product.get("store")),
                         product.get("price"), availability, _csv_text(product.get("method")),
                         _csv_text(product.get("url")), _csv_text(product.get("image")),
                         _csv_text(product.get("price_note")),
                         _csv_text(product.get("search_updated_at") or data.get("updated_at"))))
    filename = f"comparador3d-{context}-{datetime.now(timezone.utc):%Y%m%d}.csv"
    return Response(content="\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/api/search/refresh")
def refresh_search(body: SearchRefresh):
    query = " ".join(body.query.split())
    if not 2 <= len(query) <= 100:
        raise HTTPException(400, "Escribe al menos dos caracteres para actualizar la búsqueda")
    if body.context not in {"impresora", "filamento"} or body.store not in STORES:
        raise HTTPException(400, "Filtro de búsqueda inválido")
    if not refresh_lock.acquire(blocking=False):
        raise HTTPException(409, "Ya hay una actualización en curso; inténtalo al terminar")
    try:
        stores = (body.store,) if body.store != "todas" else tuple(sorted(STORES - {"todas"}))
        with ThreadPoolExecutor(max_workers=min(4, len(stores))) as pool:
            responses = list(pool.map(lambda store: search_live(query, stores=(store,), context=body.context,
                                                           delay=0, capture_dir=DATA_DIR), stores))
        result = {"searched_at": datetime.now(timezone.utc).isoformat(),
                  "products": list({p["id"]: p for response in responses for p in response["products"]}.values()),
                  "sources": [source for response in responses for source in response["sources"]],
                  "failures": [failure for response in responses for failure in response["failures"]]}
        catalog = save_search_results(query, body.context, result)
        compatibility_cache.clear()
        return {"query": query, "saved": len(result["products"]), "catalog_count": catalog["count"],
                "searched_at": result["searched_at"], "sources": result["sources"],
                "failures": result["failures"]}
    finally:
        refresh_lock.release()


@app.get("/api/compatibility/{printer_id:path}")
def compatibility(printer_id: str):
    data = load_data()
    printer = next((p for p in data["products"] if p["id"] == printer_id and p["category"] == "impresora"), None)
    if not printer:
        raise HTTPException(404, "Impresora no encontrada")
    host = urlparse(printer["url"]).hostname
    if host not in {"www.inovamarket.com", "www.3dcity.com.mx", "www.3dmarket.mx", "store.creality.com", "shop3d.mx", "www.amazon.com.mx"} and not (host or "").endswith(".mercadolibre.com.mx"):
        raise HTTPException(400, "Origen de producto no permitido")
    cached = compatibility_cache.get(printer_id)
    if not cached or monotonic() - cached[0] > 3600:
        try:
            response = session().get(printer["url"], timeout=(5, 15))
            response.raise_for_status()
        except requests.RequestException as exc:
            raise HTTPException(502, f"No se pudo consultar la ficha: {exc}") from exc
        materials, evidence = extract_compatibility(response.text, printer["store"])
        cached = (monotonic(), materials, evidence)
        compatibility_cache[printer_id] = cached
    _, materials, evidence = cached
    matches = matching_filaments(data["products"], materials)
    material_counts = {material: sum(p["category"] == material for p in matches) for material in materials}
    return {"printer": printer["name"], "materials": materials, "evidence": evidence,
            "source_url": printer["url"], "total": len(matches), "material_counts": material_counts,
            "filaments": [{key: p[key] for key in ("name", "category", "store", "price", "url")}
                          for p in matches]}


@app.get("/api/status")
def status():
    data = load_data()
    return {k: data[k] for k in ("updated_at", "count", "sources", "failures")}


def refresh_status():
    with refresh_state_lock:
        return refresh_state.copy()


@app.get("/api/refresh/status")
def get_refresh_status():
    return refresh_status()


def _run_refresh():
    def progress(store: str, completed: int, total: int):
        with refresh_state_lock:
            refresh_state.update(completed_stores=completed, total_stores=total, last_store=store)
    try:
        result = ingest(progress=progress)
        compatibility_cache.clear()
        with refresh_state_lock:
            refresh_state.update(status="complete", finished_at=datetime.now(timezone.utc).isoformat(),
                                 count=result["count"], failures=result["failures"])
    except Exception as exc:
        LOG.exception("Error durante la actualización")
        with refresh_state_lock:
            refresh_state.update(status="error", finished_at=datetime.now(timezone.utc).isoformat(), error=str(exc))
    finally:
        refresh_lock.release()


@app.post("/api/refresh", status_code=202)
def refresh():
    if not refresh_lock.acquire(blocking=False):
        state = refresh_status()
        if state["status"] == "running":
            return state
        raise HTTPException(409, "Ya hay una búsqueda actualizándose; inténtalo al terminar")
    with refresh_state_lock:
        refresh_state.update(status="running", completed_stores=0, total_stores=0, last_store=None,
                             started_at=datetime.now(timezone.utc).isoformat(), finished_at=None,
                             count=None, failures=[], error=None)
    Thread(target=_run_refresh, name="catalog-refresh", daemon=True).start()
    return refresh_status()
