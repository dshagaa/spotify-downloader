"""FastAPI application powering the spotify-dl web UI."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .auth import (
    LoginLimiter,
    constant_time_equals,
    new_csrf_token,
    safe_next,
    valid_csrf,
)
from .config import APP_NAME, SUPPORTED_FORMATS, Settings, load_settings
from .db import ACTIVE_STATUSES, JobStore, TERMINAL_STATUSES
from .jobs import JobManager
from .playlists import normalize_playlist, normalize_subfolder

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

STATUS_CLASS = {
    "queued": "warn",
    "running": "info",
    "cancelling": "warn",
    "completed": "ok",
    "failed": "err",
    "cancelled": "muted",
}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
}


class SecurityHeadersMiddleware:
    """Add response headers without buffering streamed responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _display_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def _duration(job: dict[str, Any]) -> str:
    started = job.get("started_at")
    if not started:
        return "—"
    try:
        start = datetime.fromisoformat(started)
    except ValueError:
        return "—"

    finished = job.get("finished_at")
    if finished:
        try:
            end = datetime.fromisoformat(finished)
        except ValueError:
            end = datetime.now(timezone.utc)
    else:
        end = datetime.now(timezone.utc)

    seconds = max(0, int((end - start).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _decorate(job: dict[str, Any]) -> dict[str, Any]:
    item = dict(job)
    item["status_class"] = STATUS_CLASS.get(job["status"], "muted")
    item["is_active"] = job["status"] in ACTIVE_STATUSES
    item["is_terminal"] = job["status"] in TERMINAL_STATUSES
    item["title_first"] = bool(job["title_first"])
    item["download_archive"] = bool(job["download_archive"])
    item["created_display"] = _display_time(job["created_at"])
    item["finished_display"] = _display_time(job.get("finished_at"))
    item["duration"] = _duration(job)
    return item


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    store = JobStore(settings.db_path)
    manager = JobManager(settings, store)
    limiter = LoginLimiter()
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        manager.start()
        try:
            yield
        finally:
            manager.shutdown()

    app = FastAPI(
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.manager = manager

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        max_age=settings.session_max_age,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def ensure_csrf(request: Request) -> str:
        token = request.session.get("csrf")
        if not token:
            token = new_csrf_token()
            request.session["csrf"] = token
        return token

    def context(request: Request, **extra: Any) -> dict[str, Any]:
        return {
            "app_name": APP_NAME,
            "version": __version__,
            "csrf_token": ensure_csrf(request),
            "session_auth": bool(request.session.get("auth")),
            "active_statuses": ACTIVE_STATUSES,
            **extra,
        }

    def render(
        request: Request,
        template: str,
        status_code: int = 200,
        **extra: Any,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            template,
            context(request, **extra),
            status_code=status_code,
        )

    def check_csrf(request: Request, token: str) -> None:
        if not valid_csrf(request.session.get("csrf"), token):
            raise HTTPException(
                status_code=400, detail="Invalid CSRF token"
            )

    async def require_session(request: Request) -> None:
        if request.session.get("auth"):
            return
        if request.headers.get("HX-Request"):
            raise HTTPException(
                status_code=401, headers={"HX-Redirect": "/login"}
            )
        target = quote(request.url.path, safe="/")
        raise HTTPException(
            status_code=303,
            headers={"Location": f"/login?next={target}"},
        )

    def jobs_partial(request: Request) -> HTMLResponse:
        jobs = [_decorate(job) for job in store.list(limit=25)]
        return render(request, "_jobs.html", jobs=jobs)

    def index_response(
        request: Request,
        *,
        error: str | None = None,
        values: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        values = values or {}
        jobs = [_decorate(job) for job in store.list(limit=25)]
        return render(
            request,
            "index.html",
            status_code=status_code,
            jobs=jobs,
            error=error,
            values=values,
            formats=SUPPORTED_FORMATS,
            defaults={
                "format": settings.default_format,
                "concurrent": settings.default_concurrent,
            },
            max_concurrent=settings.max_concurrent,
            download_dir=str(settings.download_dir),
        )

    # -- public routes ---------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/") -> Response:
        if request.session.get("auth"):
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", next=safe_next(next))

    @app.post("/login")
    async def login(
        request: Request,
        password: str = Form(...),
        csrf_token: str = Form(...),
        next: str = Form("/"),
    ) -> Response:
        check_csrf(request, csrf_token)
        client = request.client.host if request.client else "unknown"
        if limiter.blocked(client):
            return render(
                request,
                "login.html",
                status_code=429,
                next=safe_next(next),
                error="Too many failed attempts. Try again later.",
            )
        if not constant_time_equals(password, settings.app_password):
            limiter.record_failure(client)
            return render(
                request,
                "login.html",
                status_code=401,
                next=safe_next(next),
                error="Incorrect password.",
            )
        limiter.reset(client)
        request.session.clear()
        request.session["auth"] = True
        request.session["csrf"] = new_csrf_token()
        return RedirectResponse(safe_next(next), status_code=303)

    @app.post("/logout")
    async def logout(
        request: Request,
        csrf_token: str = Form(...),
    ) -> Response:
        check_csrf(request, csrf_token)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # -- authenticated pages ---------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request, _: None = Depends(require_session)
    ) -> HTMLResponse:
        return index_response(request)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_detail(
        request: Request,
        job_id: str,
        _: None = Depends(require_session),
    ) -> Response:
        job = store.get(job_id)
        if job is None:
            return render(
                request,
                "not_found.html",
                status_code=404,
                job_id=job_id,
            )
        return render(request, "job.html", job=_decorate(job))

    @app.post("/jobs")
    async def create_job(
        request: Request,
        playlist_url: str = Form(...),
        audio_format: str = Form(...),
        max_concurrent: int = Form(...),
        subfolder: str = Form(""),
        title_first: str | None = Form(None),
        download_archive: str | None = Form(None),
        csrf_token: str = Form(...),
        _: None = Depends(require_session),
    ) -> Response:
        check_csrf(request, csrf_token)
        values = {
            "playlist_url": playlist_url,
            "audio_format": audio_format,
            "max_concurrent": max_concurrent,
            "subfolder": subfolder,
            "title_first": bool(title_first),
            "download_archive": bool(download_archive),
        }

        playlist = normalize_playlist(playlist_url)
        if playlist is None:
            return index_response(
                request,
                error=(
                    "That does not look like a Spotify playlist link or ID."
                ),
                values=values,
                status_code=400,
            )

        if audio_format not in SUPPORTED_FORMATS:
            return index_response(
                request,
                error="Unsupported audio format.",
                values=values,
                status_code=400,
            )

        if not 1 <= max_concurrent <= settings.max_concurrent:
            return index_response(
                request,
                error=(
                    f"Concurrent matches must be between 1 and "
                    f"{settings.max_concurrent}."
                ),
                values=values,
                status_code=400,
            )

        try:
            safe_subfolder = normalize_subfolder(subfolder)
        except ValueError as exc:
            return index_response(
                request,
                error=str(exc),
                values=values,
                status_code=400,
            )

        canonical_url, label = playlist
        job = manager.submit(
            playlist_url=canonical_url,
            playlist_label=label,
            audio_format=audio_format,
            title_first=bool(title_first),
            max_concurrent=max_concurrent,
            download_archive=bool(download_archive),
            subfolder=safe_subfolder,
        )
        return RedirectResponse(f"/jobs/{job['id']}", status_code=303)

    @app.post("/jobs/{job_id}/cancel")
    async def cancel_job(
        request: Request,
        job_id: str,
        csrf_token: str = Form(...),
        _: None = Depends(require_session),
    ) -> Response:
        check_csrf(request, csrf_token)
        if store.get(job_id) is None:
            raise HTTPException(status_code=404)
        manager.cancel(job_id)
        if request.headers.get("HX-Request"):
            return jobs_partial(request)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/delete")
    async def delete_job(
        request: Request,
        job_id: str,
        csrf_token: str = Form(...),
        _: None = Depends(require_session),
    ) -> Response:
        check_csrf(request, csrf_token)
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404)
        if job["status"] in ACTIVE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail="Cancel the job before deleting it.",
            )
        store.delete(job_id)
        try:
            (settings.log_dir / f"{job_id}.log").unlink()
        except FileNotFoundError:
            pass
        if request.headers.get("HX-Request"):
            return jobs_partial(request)
        return RedirectResponse("/", status_code=303)

    @app.get("/partials/jobs", response_class=HTMLResponse)
    async def jobs_table(
        request: Request, _: None = Depends(require_session)
    ) -> HTMLResponse:
        return jobs_partial(request)

    # -- live log stream --------------------------------------------------

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(
        request: Request,
        job_id: str,
        _: None = Depends(require_session),
    ) -> StreamingResponse:
        if store.get(job_id) is None:
            raise HTTPException(status_code=404)

        log_path = settings.log_dir / f"{job_id}.log"

        async def stream() -> AsyncIterator[str]:
            offset = 0
            last_status: str | None = None
            idle_ticks = 0

            while True:
                if await request.is_disconnected():
                    return

                try:
                    job = store.get(job_id)
                except sqlite3.OperationalError:
                    # Transient lock/IO error; keep the stream alive.
                    await asyncio.sleep(1)
                    continue
                if job is None:
                    yield _sse("end", {})
                    return

                text = ""
                if log_path.exists():
                    with log_path.open("rb") as log:
                        log.seek(offset)
                        chunk = log.read(64 * 1024)
                    if chunk:
                        offset += len(chunk)
                        text = chunk.decode("utf-8", errors="replace")

                if text:
                    idle_ticks = 0
                    yield _sse("chunk", {"text": text})
                else:
                    idle_ticks += 1

                if job["status"] != last_status:
                    last_status = job["status"]
                    idle_ticks = 0
                    yield _sse(
                        "status",
                        {
                            "status": last_status,
                            "matched": job["matched"],
                            "no_match": job["no_match"],
                        },
                    )

                finished = last_status in TERMINAL_STATUSES
                if finished and not text:
                    yield _sse("end", {"status": last_status})
                    return

                if idle_ticks >= 40:
                    idle_ticks = 0
                    yield ": keep-alive\n\n"

                await asyncio.sleep(0.4)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


app = create_app()
