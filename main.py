from __future__ import annotations

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import init_db
from app.routes import _render, router

app = FastAPI(title="API Proxy")

app.mount("/static", StaticFiles(directory=str(settings.app_dir / "static")), name="static")
app.include_router(router)


@app.exception_handler(404)
async def not_found(request: Request, exc) -> HTMLResponse:
    return HTMLResponse(_render("404.html"), status_code=404)


@app.on_event("startup")
async def startup() -> None:
    await init_db()


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000)