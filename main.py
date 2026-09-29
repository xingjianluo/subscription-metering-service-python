"""FastAPI 入口：装配本地存储的计量结算服务。"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import router
from app.errors import DomainError
from app.services import MeteringService
from app.storage import InMemoryStore

logging.basicConfig(level=logging.INFO)


def create_app(service: MeteringService | None = None) -> FastAPI:
    app = FastAPI(title="Subscription Metering Service")
    app.state.service = service or MeteringService(store=InMemoryStore())

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        return JSONResponse(
            status_code=422,
            content={"error": exc.code.value, "message": exc.message, "details": exc.details},
        )

    app.include_router(router, prefix="/api/v1")

    @app.get("/")
    def read_root():
        return {"service": "subscription-metering", "status": "ok"}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
