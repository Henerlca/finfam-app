from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.auth import SESSION_COOKIE, read_session, verify_csrf
from app.database import get_engine
from app.services.assets import service_worker_source
from app.routes import router
from app.planning_routes import router as planning_router
from app.notification_routes import router as notification_router, NATIVE_PATHS
from app.assistant_routes import router as assistant_router
from app.daily_routes import daily_router
from app.recovery_routes import router as recovery_router
from app.occurrence_routes import router as occurrence_router
from app.open_finance_routes import router as open_finance_router

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_PATHS = {"/login", "/_app/health", "/sw.js", "/manus-routes.json", "/favicon.ico", "/api/scheduled/pluggy-sync"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Schema migration is additive and versioned; workbook data is imported separately once.
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    yield
    if get_engine().pool:
        get_engine().dispose()


app = FastAPI(title="Controle Financeiro Hener & Ju", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")
app.include_router(router)
app.include_router(planning_router)
app.include_router(notification_router)
app.include_router(assistant_router)
app.include_router(daily_router)
app.include_router(recovery_router)
app.include_router(occurrence_router)
app.include_router(open_finance_router)


@app.middleware("http")
async def family_access_gate(request: Request, call_next):
    path = request.url.path
    # Somente estas três APIs usam pareamento/Bearer próprios. Não liberar
    # o prefixo inteiro nem permitir que tokens Android leiam a base financeira.
    is_public = path in PUBLIC_PATHS or path.startswith("/static/") or path in NATIVE_PATHS
    if not is_public:
        try:
            session = read_session(request.cookies.get(SESSION_COOKIE))
        except RuntimeError:
            session = None
        if not session:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Autenticação necessária."}, status_code=401, headers={"Cache-Control": "no-store"})
            return RedirectResponse(url="/login", status_code=303, headers={"Cache-Control": "no-store"})
        request.state.family_session = session
    else:
        request.state.family_session = None

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if not path.startswith("/static/"):
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
    return response


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return Response(
        content=service_worker_source(),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/manus-routes.json", include_in_schema=False)
def route_manifest():
    return FileResponse(ROOT / "public" / "manus-routes.json", media_type="application/json", headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(ROOT / "app" / "static" / "icons" / "app-icon.svg", media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/_app/health", include_in_schema=False)
def health():
    try:
        with get_engine().connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        return {"ok": True}
    except Exception:
        return JSONResponse({"ok": False}, status_code=503)
