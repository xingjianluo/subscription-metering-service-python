"""HTTP 层：把 MeteringError 映射成带稳定错误码的响应。"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.errors import MeteringError
from app.service import MeteringService
from app.store import Store

logger = logging.getLogger("metering.api")

# 客户端输入类错误 -> 422；状态/冲突类错误 -> 409
_BAD_REQUEST_PREFIXES = ("usage_", "missing", "invalid", "unknown")


def create_app(service: MeteringService | None = None) -> FastAPI:
    app = FastAPI(title="Subscription Metering Service", version="0.1.0")
    app.state.service = service or MeteringService(Store())

    @app.exception_handler(MeteringError)
    async def metering_error_handler(request: Request, exc: MeteringError):
        code = exc.code.value
        status = 422 if code.startswith(_BAD_REQUEST_PREFIXES) else 409
        logger.info(
            "http rejection path=%s code=%s details=%s",
            request.url.path, code, exc.details,
        )
        return JSONResponse(
            status_code=status,
            content={"error": code, "message": exc.message, "details": exc.details},
        )

    @app.post("/admin/plans")
    def define_plan(body: dict) -> dict:
        return app.state.service.define_plan(body)

    @app.post("/subscriptions")
    def create_subscription(body: dict) -> dict:
        return app.state.service.create_subscription(body)

    @app.post("/usage")
    def report_usage(body: dict) -> dict:
        return app.state.service.report_usage(body)

    @app.post("/subscriptions/{subscription_id}/plan-change")
    def change_plan(subscription_id: str, body: dict) -> dict:
        body = dict(body)
        body["subscription_id"] = subscription_id
        return app.state.service.change_plan(body)

    @app.post("/subscriptions/{subscription_id}/cancel")
    def cancel(subscription_id: str, body: dict) -> dict:
        body = dict(body)
        body["subscription_id"] = subscription_id
        return app.state.service.cancel_subscription(body)

    @app.post("/subscriptions/{subscription_id}/invoices")
    def invoice_period(subscription_id: str, body: dict) -> dict:
        body = dict(body or {})
        body["subscription_id"] = subscription_id
        return app.state.service.invoice_period(body)

    @app.post("/invoices/{invoice_id}/settle")
    def settle(invoice_id: str, body: dict) -> dict:
        body = dict(body or {})
        body["invoice_id"] = invoice_id
        return app.state.service.settle(body)

    @app.get("/subscriptions/{subscription_id}")
    def get_subscription(subscription_id: str) -> dict:
        return app.state.service.get_subscription(subscription_id)

    @app.get("/invoices/{invoice_id}")
    def get_invoice(invoice_id: str) -> dict:
        return app.state.service.get_invoice(invoice_id)

    @app.get("/events")
    def list_events() -> list[dict]:
        return app.state.service.list_events()

    @app.post("/admin/recover")
    def recover() -> dict:
        return {"recovered": app.state.service.recover_pending()}

    @app.post("/admin/replay")
    def replay() -> dict:
        return app.state.service.replay()

    return app
