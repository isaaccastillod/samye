"""FastAPI application for reviewing local edit proposals."""

from __future__ import annotations

import secrets
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from samye.engine import Engine

STATIC_DIR = Path(__file__).with_name("static")
TOKEN_PLACEHOLDER = "__SAMYE_TOKEN__"


def create_app(engine: Engine) -> FastAPI:
    """Construct a proposal UI bound to one engine instance."""
    app = FastAPI(title="samye", docs_url=None, redoc_url=None, openapi_url=None)
    csrf_token = secrets.token_urlsafe(32)
    app.state.csrf_token = csrf_token
    allowed_hosts = {"localhost", "127.0.0.1"}
    if engine.cfg.web_base_url is not None:
        host = urlparse(engine.cfg.web_base_url).hostname
        if host is not None:
            allowed_hosts.add(host.lower())

    @app.middleware("http")
    async def guard_host(request: Request, call_next):  # type: ignore[no-untyped-def]
        host = request.url.hostname
        if host is None or host.lower() not in allowed_hosts:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        template = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(template.replace(TOKEN_PLACEHOLDER, csrf_token))

    @app.get("/api/proposals")
    async def list_proposals() -> list[dict[str, object]]:
        return [
            {"file_id": file_id, **asdict(proposal)}
            for file_id, proposal in engine.list_proposals()
        ]

    async def require_token(request: Request) -> None:
        supplied = request.headers.get("X-Samye-Token", "")
        if not secrets.compare_digest(supplied, csrf_token):
            raise HTTPException(status_code=403, detail="invalid CSRF token")

    @app.post("/api/proposals/{file_id}/{proposal_id}/accept")
    async def accept(file_id: str, proposal_id: str, request: Request) -> dict[str, str]:
        await require_token(request)
        try:
            status = await engine.accept_proposal(file_id, proposal_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="proposal not found") from None
        return {"status": status}

    @app.post("/api/proposals/{file_id}/{proposal_id}/reject")
    async def reject(file_id: str, proposal_id: str, request: Request) -> dict[str, str]:
        await require_token(request)
        try:
            status = await engine.reject_proposal(file_id, proposal_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="proposal not found") from None
        return {"status": status}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
