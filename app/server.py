from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import shutil
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from PIL import Image
from starlette.concurrency import run_in_threadpool

from .config import contained
from .workspace import Workspace


class ChatRequest(BaseModel):
    # Historical mode/plan/goal fields are ignored. There is no mode state or routing.
    model_config = ConfigDict(extra="ignore")
    session_id: str
    message: str = Field(default="", max_length=40000)
    model_id: str | None = None
    attachments: list[str] = Field(default_factory=list, max_length=8)


class RunRequest(BaseModel):
    session_id: str
    code: str = Field(max_length=200000)
    timeout: int = Field(default=30, ge=1, le=300)
    user_initiated: bool


class DownloadRequest(BaseModel):
    token: str | None = Field(default=None, max_length=1000)


class ImportRequest(BaseModel):
    directory: str = Field(max_length=2000)


def create_app(settings=None, runtime=None):
    workspace = Workspace(settings, runtime)
    settings, store = workspace.settings, workspace.store
    attachments, catalog, runtime = workspace.attachments, workspace.catalog, workspace.runtime
    python = workspace.python
    token = secrets.token_urlsafe(32)
    upload_locks = {}
    session_locks = workspace._session_locks

    async def housekeeping():
        while True:
            await asyncio.sleep(300)
            await run_in_threadpool(workspace.housekeeping)

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(attachments.cleanup)
        cleanup_task = asyncio.create_task(housekeeping())
        yield
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task
        await run_in_threadpool(workspace.close)

    app = FastAPI(title="Local AI Workspace", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings, app.state.store = settings, store
    app.state.catalog, app.state.runtime, app.state.attachments = catalog, runtime, attachments
    app.state.python = python
    app.state.workspace = workspace

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        if host not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse({"error": "仅接受本机访问"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in {f"http://127.0.0.1:{settings['port']}", f"http://localhost:{settings['port']}", "http://testserver"}:
            return JSONResponse({"error": "不允许跨站请求"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not secrets.compare_digest(request.headers.get("x-workspace-token", ""), token):
                return JSONResponse({"error": "本地操作令牌无效，请刷新页面"}, status_code=403)
            # Raw attachment uploads have their own streaming limit.
            if not request.url.path.endswith("/attachments"):
                length = request.headers.get("content-length")
                if (length is None and request.method != "DELETE") or (length is not None and (not length.isdigit() or int(length) > 1024 * 1024)):
                    return JSONResponse({"error": "请求过大或缺少长度"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(RuntimeError)
    async def runtime_error(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=503)

    @app.get("/health")
    def health():
        return {"status": "ok", "application": "LocalAIWorkspace", "version": "1.0.0"}

    @app.get("/api/bootstrap")
    def bootstrap():
        return {"token": token, "default_model": settings["default_model"], "vision_model": settings["vision_model"],
                "limits": {k: settings[k] for k in ("upload_bytes", "video_bytes", "max_attachments", "python_timeout_seconds")},
                "sessions": store.sessions()}

    @app.post("/api/sessions")
    def new_session():
        return store.new_session()

    @app.get("/api/sessions")
    def sessions():
        return store.sessions()

    @app.get("/api/sessions/{sid}")
    def history(sid: str):
        return workspace.history(sid)

    @app.delete("/api/sessions/{sid}")
    def delete_session(sid: str):
        return workspace.delete_session(sid)

    @app.post("/api/sessions/{sid}/attachments")
    async def upload(sid: str, request: Request):
        lock = upload_locks.setdefault(sid, asyncio.Lock())
        async with lock:
            session_lock = session_locks.setdefault(sid, threading.Lock())
            if not session_lock.acquire(False):
                raise ValueError("会话正在使用，请稍后上传")
            name = unquote(request.headers.get("x-filename", ""))
            try:
                row = attachments.prepare(sid, name)
            except BaseException:
                session_lock.release()
                raise
            path = Path(row["path"])
            limit = settings["video_bytes"] if row["kind"] == "video" else settings["upload_bytes"]
            total = sum(r["size"] for r in store.attachments(sid))
            size = 0
            try:
                async with asyncio.timeout(120):
                    with path.open("wb") as out:
                        async for chunk in request.stream():
                            size += len(chunk)
                            if size > limit or total + size > settings["session_bytes"]:
                                raise ValueError("附件过大或当前会话附件总量超过上限")
                            out.write(chunk)
                await run_in_threadpool(attachments.process, row)
                return attachments.public(row)
            except BaseException as exc:
                shutil.rmtree(contained(attachments.directory(sid), path.parent), ignore_errors=True)
                if isinstance(exc, (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning)):
                    raise ValueError("附件已损坏或无法识别：" + str(exc).split(":")[0]) from exc
                if isinstance(exc, TimeoutError):
                    raise ValueError("附件上传超时，请重试") from exc
                raise
            finally:
                session_lock.release()

    @app.delete("/api/sessions/{sid}/attachments/{aid}")
    def remove_attachment(sid: str, aid: str):
        return workspace.remove_attachment(sid, aid)

    @app.get("/api/sessions/{sid}/attachments/{aid}/{kind}")
    def attachment_file(sid: str, aid: str, kind: str):
        row = store.attachment(sid, aid)
        path = contained(attachments.directory(sid), Path(row["path"]))
        if kind == "preview" and row["kind"] in {"image", "video"}:
            path = path.parent / "preview.jpg"
            return FileResponse(path, media_type="image/jpeg")
        if kind != "download":
            raise ValueError("无效的附件操作")
        return FileResponse(path, media_type="application/octet-stream", filename=row["name"])

    @app.get("/api/models")
    def models():
        return {"models": catalog.public(), "loaded": runtime.model_id}

    @app.post("/api/models/{mid}/download")
    def download(mid: str, body: DownloadRequest):
        return catalog.download(mid, body.token)

    @app.post("/api/models/{mid}/import")
    def import_model(mid: str, body: ImportRequest):
        return catalog.import_model(mid, body.directory)

    @app.post("/api/models/unload")
    def unload():
        if not runtime.lock.acquire(False):
            raise ValueError("模型正在回答，请等待完成")
        try:
            runtime.unload()
            return {"unloaded": True}
        finally:
            runtime.lock.release()

    @app.post("/api/shutdown")
    def shutdown(request: Request):
        stop = getattr(request.app.state, "stop_service", None)
        if stop is None:
            raise ValueError("服务关闭入口未初始化")
        stop()
        return {"stopping": True}

    @app.post("/chat")
    async def chat(body: ChatRequest, request: Request):
        cancelled = threading.Event()
        stream = workspace.chat(body.session_id, body.message, body.model_id, body.attachments, cancelled)

        def events():
            try:
                for event in stream:
                    if event["type"] == "status" and event.get("status") == "started":
                        event = {"type": "start", "model": event["model"]}
                    elif event["type"] == "status" and event.get("status") == "auxiliary":
                        event = {k: v for k, v in event.items() if k != "status"} | {"type": "auxiliary"}
                    elif event["type"] == "error":
                        event = event | {"error": event.get("text", event.get("message", "模型请求失败"))}
                    yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            finally:
                cancelled.set()
                stream.close()
        return StreamingResponse(events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.post("/api/python/run")
    def run_python(body: RunRequest):
        return workspace.run_python(body.session_id, body.code, body.timeout, body.user_initiated)

    if (settings.root / "web").is_dir():
        app.mount("/", StaticFiles(directory=settings.root / "web", html=True), name="web")
    else:
        @app.get("/{path:path}")
        def desktop_only(path: str):
            return JSONResponse({"error": "此便携包通过桌面窗口使用"}, status_code=404)
    return app
