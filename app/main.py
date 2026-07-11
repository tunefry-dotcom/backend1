"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from app.core.config import settings
from app.modules.auth.router import router as auth_router
from app.modules.billing.router import router as billing_router
from app.modules.profile.router import router as profile_router


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Expose frontend_url on app.state so Jinja2 templates can reference it.
    _app.state.frontend_url = settings.frontend_url
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Tunefry API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_url],
        allow_credentials=True,  # required for cookies
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_router)
    app.include_router(billing_router)
    app.include_router(profile_router)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def root() -> str:
        return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Tunefry API</title>
  <style>
    body{font-family:system-ui,sans-serif;background:#0a0a0a;color:#f0f0f0;
         display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
    .card{background:#161616;border:1px solid #2a2a2a;border-radius:12px;
          padding:2.5rem 2rem;max-width:380px;text-align:center}
    h1{font-size:1.6rem;margin-bottom:.5rem}
    p{color:#aaa;margin-bottom:1.5rem}
    a{display:inline-block;padding:.65rem 1.5rem;background:#6366f1;color:#fff;
      border-radius:6px;text-decoration:none;font-size:.95rem}
    a:hover{background:#4f46e5}
  </style>
</head>
<body>
  <div class="card">
    <h1>🎵 Tunefry API</h1>
    <p>Music distribution platform backend — v0.1.0</p>
    <a href="/docs">Open API Docs</a>
  </div>
</body>
</html>"""

    return app


app = create_app()
