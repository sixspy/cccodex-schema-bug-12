import json
from pathlib import Path

from aiohttp import web
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import init_db
from app.services import (
    apply_contract,
    emit_outbound,
    list_contracts,
    list_reports,
    mask_status,
    masked,
    outbound,
    outbound_card,
    refresh_lock,
    refresh_masking,
    run_breaking_check,
    run_reconcile,
    set_page_mask,
)
from app.services.errors import ServiceError

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-schema-session")


def create_app(db_path: str | None = None) -> web.Application:
    if db_path:
        import os

        os.environ["DATABASE_PATH"] = db_path
    init_db(db_path)
    app = web.Application()
    app["db_path"] = db_path
    app.router.add_static("/static/", STATIC)
    app.router.add_get("/", root)
    app.router.add_get("/login", login_page)
    app.router.add_get("/app/", spa_page)
    app.router.add_post("/api/login", api_login)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_get("/api/meta", api_meta)
    app.router.add_get("/api/contracts", api_contracts)
    app.router.add_post("/api/contracts/{cid}/breaking-check", api_breaking)
    app.router.add_post("/api/contracts/{cid}/apply", api_apply)
    app.router.add_get("/api/reconcile-reports", api_reports)
    app.router.add_post("/api/reconcile-reports", api_reconcile)
    app.router.add_get("/api/mask", api_mask)
    app.router.add_post("/api/mask/refresh", api_mask_refresh)
    app.router.add_post("/api/mask/page", api_mask_page)
    app.router.add_post("/api/outbound", api_outbound)
    return app


def _session(request: web.Request) -> dict:
    raw = request.cookies.get("schema_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: web.Request) -> str:
    user = _session(request).get("user")
    if user != Config.ADMIN_USER:
        raise ServiceError("未登录", 401)
    return user


def json_resp(data: dict, status: int = 200) -> web.Response:
    return web.Response(
        status=status,
        text=json.dumps(data, ensure_ascii=False),
        content_type="application/json",
    )


async def root(request: web.Request):
    if _session(request).get("user") == Config.ADMIN_USER:
        raise web.HTTPFound("/app/")
    raise web.HTTPFound("/login")


async def login_page(_request: web.Request):
    return web.FileResponse(STATIC / "login.html")


async def spa_page(request: web.Request):
    if _session(request).get("user") != Config.ADMIN_USER:
        raise web.HTTPFound("/login")
    return web.FileResponse(STATIC / "index.html")


async def api_login(request: web.Request):
    body = await request.json()
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return json_resp({"ok": False, "error": "账号或密码错误"}, 400)
    resp = json_resp({"ok": True, "user": user})
    resp.set_cookie(
        "schema_session",
        serializer.dumps({"user": user}),
        httponly=True,
        path="/",
    )
    return resp


async def api_logout(_request: web.Request):
    resp = json_resp({"ok": True})
    resp.del_cookie("schema_session", path="/")
    return resp


async def api_meta(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    db_path = request.app.get("db_path")
    status = mask_status(db_path)
    return json_resp({
        "lab": Config.LAB_NAME,
        "product": "cc-schema-lab",
        "phone_masked": masked("phone", db_path),
        "outbound_phone": outbound("phone", "13800138000", db_path),
        "decision": status["decision"],
        "chip": status["chip"],
        "compliance": status["compliance"],
    })


async def api_contracts(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    return json_resp({"items": list_contracts(request.app.get("db_path"))})


async def api_breaking(request: web.Request):
    try:
        require_user(request)
        cid = int(request.match_info["cid"])
        return json_resp({"check": run_breaking_check(cid, request.app.get("db_path"))})
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)


async def api_apply(request: web.Request):
    try:
        require_user(request)
        cid = int(request.match_info["cid"])
        return json_resp({"contract": apply_contract(cid, request.app.get("db_path"))})
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)


async def api_reports(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    return json_resp({"items": list_reports(request.app.get("db_path"))})


async def api_reconcile(request: web.Request):
    try:
        require_user(request)
        body = await request.json()
        as_of = str(body.get("as_of") or "").strip() or "2026-11-18"
        return json_resp({"report": run_reconcile(as_of, request.app.get("db_path"))})
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)


async def api_mask(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    return json_resp(mask_status(request.app.get("db_path")))


async def api_mask_refresh(request: web.Request):
    try:
        user = require_user(request)
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    db_path = request.app.get("db_path")
    # 进程内锁先挡同进程并发，库内锁再挡多进程：两道都不能少。
    async with refresh_lock(db_path):
        try:
            return json_resp(refresh_masking(db_path, holder=user))
        except ServiceError as exc:
            return json_resp({"ok": False, "error": exc.message}, exc.status_code)


async def api_mask_page(request: web.Request):
    try:
        require_user(request)
        body = await request.json()
        field = str(body.get("field") or "").strip()
        if "masked" not in body:
            raise ServiceError("缺少 masked 开关值。", 400)
        masked_on = bool(body.get("masked"))
        return json_resp(set_page_mask(field, masked_on, request.app.get("db_path")))
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)


async def api_outbound(request: web.Request):
    try:
        require_user(request)
        body = await request.json()
        field = str(body.get("field") or "").strip()
        value = str(body.get("value") or "")
        if not field:
            raise ServiceError("缺少出站字段名。", 400)
        if not all(ch.isascii() and (ch.isalnum() or ch == "_") for ch in field):
            raise ServiceError("出站字段名只能由字母、数字、下划线组成。", 400)
        db_path = request.app.get("db_path")
        if body.get("commit"):
            return json_resp(emit_outbound(field, value, db_path))
        return json_resp({"card": outbound_card(field, value, db_path)})
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
