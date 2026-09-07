"""Loopback-only dashboard with a same-origin control API and read-only SSE panel."""

from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import Field, StrictBool

from .config import Config, StrictModel
from .control import Controller
from .domain import NeedsAttention

ASSETS = Path(__file__).with_name("static")


class StartRequest(StrictModel):
    auto_submit: StrictBool = False
    confirmed: StrictBool = False
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class TaskRequest(StrictModel):
    task_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")


class ResolveRequest(TaskRequest):
    outcome: Literal["no-order", "done"]
    confirmation: str = Field(max_length=40)


def create_app(config: Config, data_dir: Path, *, demo=False, adapter_factory=None):
    controller = Controller(config, data_dir, demo=demo, adapter_factory=adapter_factory)
    control_token = secrets.token_urlsafe(32)
    cookie_name = "ticket_control_" + secrets.token_hex(4)

    @asynccontextmanager
    async def lifespan(_):
        await controller.open()
        try:
            yield
        finally:
            await controller.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.controller = controller

    @app.middleware("http")
    async def security(request: Request, call_next):
        # Prevent DNS rebinding; remote use is through an SSH loopback tunnel.
        hostname = urlsplit("//" + request.headers.get("host", "")).hostname
        if hostname not in {"127.0.0.1", "localhost", "::1"}:
            return JSONResponse(
                {"detail": "控制台仅允许本机地址，请使用 SSH 隧道"}, status_code=403
            )
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.headers.get('host')}"
        if origin and origin != expected_origin:
            return JSONResponse({"detail": "禁止跨站访问控制台"}, status_code=403)
        if request.url.path.startswith("/api/") and request.url.path != "/api/bootstrap":
            if not secrets.compare_digest(request.cookies.get(cookie_name, ""), control_token):
                return JSONResponse({"detail": "请刷新控制台建立本机会话"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not secrets.compare_digest(
                request.headers.get("x-control-token", ""), control_token
            ):
                return JSONResponse({"detail": "缺少本机操作确认令牌，请刷新页面"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "仅接受 JSON 请求"}, status_code=415)
            if len(await request.body()) > 32768:
                return JSONResponse({"detail": "请求过大"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-src 'self' http://127.0.0.1:6080 http://localhost:6080 http://[::1]:6080; "
            "frame-ancestors 'self'; object-src 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(NeedsAttention)
    async def attention(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def validation(_, exc):
        # Do not echo the input body (passenger names/settings) into errors.
        return JSONResponse(
            {
                "detail": "配置校验失败，请检查必填项、时间、日期、席别和预算",
                "fields": [".".join(map(str, e["loc"])) for e in exc.errors()],
            },
            status_code=422,
        )

    @app.get("/healthz")
    async def health():
        return {"ok": True, "version": "0.2.0", "mode": "demo" if demo else "browser"}

    @app.get("/")
    async def home():
        return FileResponse(ASSETS / "index.html")

    @app.get("/monitor")
    async def monitor():
        return FileResponse(ASSETS / "monitor.html")

    @app.get("/assets/{filename}")
    async def asset(filename: str):
        if filename not in {"app.js", "monitor.js", "styles.css", "stations.json"}:
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return FileResponse(ASSETS / filename)

    @app.get("/api/bootstrap")
    async def bootstrap():
        response = JSONResponse(
            {
                "token": control_token,
                "config": controller.draft.model_dump(mode="json"),
                "fingerprint": controller.draft.fingerprint(),
                "snapshot": controller.snapshot(),
            }
        )
        response.set_cookie(cookie_name, control_token, httponly=True, samesite="strict")
        return response

    @app.get("/api/status")
    async def status():
        return controller.snapshot()

    @app.get("/api/timings")
    async def timings():
        return controller.store.timing_report()

    @app.get("/api/events")
    async def events(request: Request):
        async def stream():
            previous = None
            heartbeats = 0
            while not await request.is_disconnected():
                if previous != controller.version or heartbeats >= 15:
                    previous = controller.version
                    payload = json.dumps(controller.snapshot(), ensure_ascii=False)
                    yield f"event: snapshot\ndata: {payload}\n\n"
                    heartbeats = 0
                else:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(1)
                heartbeats += 1

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.post("/api/config")
    async def save(payload: Config):
        controller.save(payload)
        return {
            "config": controller.draft.model_dump(mode="json"),
            "fingerprint": controller.draft.fingerprint(),
        }

    @app.post("/api/login", status_code=202)
    async def login():
        controller.login()
        return {"accepted": True}

    @app.post("/api/query", status_code=202)
    async def query():
        controller.query()
        return {"accepted": True}

    @app.post("/api/start", status_code=202)
    async def start(payload: StartRequest):
        controller.start(**payload.model_dump())
        return {"accepted": True, "task_id": controller.active_task}

    @app.post("/api/stop")
    async def stop():
        await controller.stop()
        return controller.snapshot()

    @app.post("/api/reconcile", status_code=202)
    async def reconcile(payload: TaskRequest):
        controller.reconcile(payload.task_id)
        return {"accepted": True}

    @app.post("/api/resolve")
    async def resolve(payload: ResolveRequest):
        controller.resolve(**payload.model_dump())
        return controller.snapshot()

    return app
