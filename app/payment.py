"""本地模拟支付网关：不接真实渠道。

以幂等键（结算时用账单 id）保证：
  - 已成功的键重放 -> 返回原交易，绝不重复扣款；
  - 首次失败的键不缓存失败结果，允许同键重试（真实网关预授权语义）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class PaymentResult(str, Enum):
    CAPTURED = "captured"
    DECLINED = "declined"


@dataclass
class PaymentResponse:
    result: PaymentResult
    txn_id: str | None
    idempotency_key: str
    amount: Decimal
    reason: str | None = None


class PaymentGateway:
    def __init__(self) -> None:
        # 只记录“成功”的扣款，这是防止重复扣款的唯一事实来源
        self._captured: dict[str, PaymentResponse] = {}
        # 需要模拟首次失败的键（消费一次即移除）
        self._decline_once: set[str] = set()

    def charge(
        self,
        idempotency_key: str,
        amount: Decimal,
        currency: str,
        *,
        decline_once: bool = False,
    ) -> PaymentResponse:
        amount = Decimal(amount)
        existing = self._captured.get(idempotency_key)
        if existing is not None:
            return existing  # 重放：返回原交易，不再扣款

        if amount <= 0:
            # 零元/负账单无需扣款，直接视为成功但不产生交易号
            resp = PaymentResponse(PaymentResult.CAPTURED, None, idempotency_key, amount)
            self._captured[idempotency_key] = resp
            return resp

        if decline_once or idempotency_key in self._decline_once:
            self._decline_once.discard(idempotency_key)
            return PaymentResponse(
                PaymentResult.DECLINED,
                None,
                idempotency_key,
                amount,
                reason="模拟渠道余额不足",
            )

        resp = PaymentResponse(
            PaymentResult.CAPTURED,
            f"txn_{uuid.uuid4().hex[:16]}",
            idempotency_key,
            amount,
        )
        self._captured[idempotency_key] = resp
        return resp
