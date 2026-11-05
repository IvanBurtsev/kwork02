from __future__ import annotations

import csv
import io
import json
import logging
import time
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import desc, func as sa_func, select
from sqlalchemy.orm import selectinload

from app.auth import (
    create_access_token,
    decode_access_token,
    generate_api_key,
    hash_password,
    verify_password,
)
from app.config import settings
from app.db import session_maker
from app.models import ApiKey, Log, User
from app.proxy import (
    KeyInactiveError,
    QuotaExceededError,
    proxy_chat_completion,
    proxy_completion_demo,
    resolve_key,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_templates_dir = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_templates_dir)),
    autoescape=select_autoescape(["html", "xml"]),
)


def _render(name: str, **context) -> str:
    return _env.get_template(name).render(**context)


def _get_current_user(request: Request) -> int | None:
    token = request.cookies.get("token")
    if not token:
        return None
    sub = decode_access_token(token)
    if sub is None:
        return None
    try:
        return int(sub)
    except ValueError:
        return None


def _login_required(request: Request) -> int:
    user_id = _get_current_user(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user_id


# ---- Pages ----

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user_id = _get_current_user(request)
    if user_id:
        return RedirectResponse(url="/dashboard", status_code=302)
    return HTMLResponse(_render("login.html"))


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return HTMLResponse(_render("login.html"))


@router.post("/login")
async def login_post(username: str = Form(...), password: str = Form(...)):
    async with session_maker() as session:
        user = await session.scalar(select(User).where(User.username == username))
        if user is None or not verify_password(password, user.password_hash):
            return HTMLResponse(_render("login.html", error="Неверное имя или пароль"))
        if not user.is_active:
            return HTMLResponse(_render("login.html", error="Пользователь заблокирован"))
        token = create_access_token(str(user.id))
        resp = RedirectResponse(url="/dashboard", status_code=302)
        resp.set_cookie(key="token", value=token, max_age=settings.jwt_expire_minutes * 60, httponly=True)
        return resp


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return HTMLResponse(_render("register.html"))


@router.post("/register")
async def register_post(
    username: str = Form(...), password: str = Form(...)
):
    if len(username.strip()) < 3 or len(password) < 4:
        return HTMLResponse(_render("register.html", error="Имя минимум 3 символа, пароль минимум 4"))
    async with session_maker() as session:
        existing = await session.scalar(select(User).where(User.username == username.strip()))
        if existing:
            return HTMLResponse(_render("register.html", error="Пользователь уже существует"))
        user = User(
            username=username.strip(),
            password_hash=hash_password(password),
            is_admin=(username.strip() == settings.admin_username),
        )
        session.add(user)
        await session.commit()
    return RedirectResponse(url="/login", status_code=303)


@router.get("/logout")
async def logout():
    resp = RedirectResponse(url="/", status_code=302)
    resp.delete_cookie("token")
    return resp


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user_id = _login_required(request)
    async with session_maker() as session:
        user = await session.get(User, user_id)
        keys = (
            await session.scalars(
                select(ApiKey).where(ApiKey.user_id == user_id).order_by(desc(ApiKey.id))
            )
        ).all()
        logs = (
            await session.scalars(
                select(Log).where(Log.user_id == user_id).order_by(desc(Log.id)).limit(10)
            )
        ).all()
        total_tokens = await session.scalar(
            select(sa_func.coalesce(sa_func.sum(Log.total_tokens), 0)).where(Log.user_id == user_id)
        )
        total_cost = await session.scalar(
            select(sa_func.coalesce(sa_func.sum(Log.cost), 0)).where(Log.user_id == user_id)
        )
    return HTMLResponse(
        _render(
            "dashboard.html",
            user=user,
            keys=keys,
            logs=logs,
            total_tokens=int(total_tokens),
            total_cost=round(float(total_cost), 4),
        )
    )


@router.post("/dashboard/key/create")
async def create_key(request: Request, name: str = Form("Default")):
    user_id = _login_required(request)
    async with session_maker() as session:
        key = ApiKey(
            user_id=user_id,
            key=generate_api_key(),
            name=name.strip() or "Default",
            tokens_limit=settings.max_tokens_per_key,
        )
        session.add(key)
        await session.commit()
    return RedirectResponse(url="/dashboard", status_code=303)


@router.post("/dashboard/key/{key_id}/toggle")
async def toggle_key(request: Request, key_id: int):
    user_id = _login_required(request)
    async with session_maker() as session:
        key = await session.get(ApiKey, key_id)
        if key and key.user_id == user_id:
            key.is_active = not key.is_active
            await session.commit()
    return RedirectResponse(url="/dashboard", status_code=303)


@router.post("/dashboard/key/{key_id}/delete")
async def delete_key(request: Request, key_id: int):
    user_id = _login_required(request)
    async with session_maker() as session:
        key = await session.get(ApiKey, key_id)
        if key and key.user_id == user_id:
            await session.delete(key)
            await session.commit()
    return RedirectResponse(url="/dashboard", status_code=303)


# ---- API ----

@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    api_key = auth[7:]
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    async with session_maker() as session:
        try:
            key_obj, user = await resolve_key(session, api_key)
        except (KeyInactiveError, QuotaExceededError) as exc:
            raise HTTPException(status_code=403, detail=str(exc))

        if not settings.deepseek_api_key:
            result = await proxy_completion_demo(body.get("messages", []))
            return JSONResponse(result)

        try:
            result = await proxy_chat_completion(session, key_obj, user, body)
            return JSONResponse(result)
        except Exception as exc:
            logger.warning("Proxy error: %s", exc)
            raise HTTPException(status_code=502, detail=str(exc))


# ---- Admin ----

@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user_id = _login_required(request)
    async with session_maker() as session:
        user = await session.get(User, user_id)
        if not user or not user.is_admin:
            return HTMLResponse(_render("login.html", error="Доступ запрещён"))
        users = (
            await session.scalars(
                select(User).options(selectinload(User.api_keys)).order_by(User.id)
            )
        ).all()
        keys = (await session.scalars(select(ApiKey).order_by(ApiKey.id))).all()
        stats = await session.execute(
            select(
                sa_func.coalesce(sa_func.sum(Log.total_tokens), 0).label("tokens"),
                sa_func.coalesce(sa_func.sum(Log.cost), 0).label("cost"),
                sa_func.count(Log.id).label("requests"),
            )
        )
        row = stats.one()
    return HTMLResponse(
        _render(
            "admin.html",
            users=users,
            keys=keys,
            total_tokens=int(row.tokens),
            total_cost=round(float(row.cost), 4),
            total_requests=int(row.requests),
        )
    )


@router.post("/admin/user/{target_id}/toggle")
async def admin_toggle_user(request: Request, target_id: int):
    user_id = _login_required(request)
    async with session_maker() as session:
        admin = await session.get(User, user_id)
        if not admin or not admin.is_admin:
            raise HTTPException(status_code=403)
        target = await session.get(User, target_id)
        if target:
            target.is_active = not target.is_active
            await session.commit()
    return RedirectResponse(url="/admin", status_code=303)


# ---- Utils ----

@router.get("/docs", response_class=HTMLResponse)
async def docs_page():
    return HTMLResponse(_render("docs.html"))


@router.get("/dashboard/logs.csv")
async def export_logs(request: Request):
    user_id = _login_required(request)
    async with session_maker() as session:
        logs = (
            await session.scalars(
                select(Log).where(Log.user_id == user_id).order_by(desc(Log.id)).limit(1000)
            )
        ).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "model", "prompt_tokens", "completion_tokens", "total_tokens", "cost", "status", "error", "date"])
    for log in logs:
        w.writerow(
            [
                log.id, log.model, log.prompt_tokens, log.completion_tokens,
                log.total_tokens, log.cost, log.status, log.error_message or "",
                log.created_at.isoformat() if log.created_at else "",
            ]
        )
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=logs.csv"},
    )
