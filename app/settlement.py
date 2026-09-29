"""账单结算：状态机 + 幂等支付，保证不重复扣款。"""

from __future__ import annotations

from dataclasses import replace

from app.models import Invoice, InvoiceStatus
from app.payment import PaymentGateway, PaymentResult


def settle_invoice(
    invoice: Invoice,
    gateway: PaymentGateway,
    settled_at,
    *,
    decline_once: bool = False,
) -> Invoice:
    """对账单执行结算，返回新状态的账单（不就地修改）。

    - SETTLED：直接原样返回（结算幂等，不调用网关、不重复扣款）；
    - DRAFT / FAILED：调用网关；成功 -> SETTLED，拒绝 -> FAILED（可重试）。
    """
    if invoice.status == InvoiceStatus.SETTLED:
        return invoice

    response = gateway.charge(
        idempotency_key=invoice.id,
        amount=invoice.total_amount,
        currency=invoice.currency,
        decline_once=decline_once,
    )

    if response.result == PaymentResult.CAPTURED:
        return replace(
            invoice,
            status=InvoiceStatus.SETTLED,
            settled_at=settled_at,
            payment_txn_id=response.txn_id,
        )

    return replace(
        invoice,
        status=InvoiceStatus.FAILED,
        settled_at=None,
        payment_txn_id=None,
    )
