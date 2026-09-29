"""事件台账：幂等去重、冲突判定、载荷指纹。"""

from __future__ import annotations

import hashlib
import json as _json

from app.errors import DomainError, ErrorCode
from app.models import EventType, LedgerEntry


def hash_payload(event_type: EventType, payload: dict) -> str:
    """对事件类型 + 规范化载荷做稳定指纹（与键顺序无关）。"""
    canonical = _json.dumps(
        {"type": event_type.value, "payload": payload},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def check_ledger(
    ledger: dict[str, LedgerEntry],
    event_id: str,
    event_type: EventType,
    payload_hash: str,
) -> LedgerEntry | None:
    """判定事件是重复、冲突还是未见。

    返回已有条目（重复 / 回放拒绝），未见返回 None；同 event_id 指纹不同则冲突拒绝。
    """
    existing = ledger.get(event_id)
    if existing is None:
        return None
    if existing.event_type != event_type:
        raise DomainError(
            ErrorCode.EVENT_PROCESSED_CONFLICT,
            f"事件 {event_id} 已按类型 {existing.event_type.value} 处理，"
            f"不能以 {event_type.value} 重放",
            details={
                "event_id": event_id,
                "existing_type": existing.event_type.value,
                "new_type": event_type.value,
            },
        )
    if existing.payload_hash != payload_hash:
        raise DomainError(
            ErrorCode.EVENT_PROCESSED_CONFLICT,
            f"事件 {event_id} 已处理但载荷不一致，拒绝被后来的内容覆盖",
            details={
                "event_id": event_id,
                "existing_hash": existing.payload_hash,
                "new_hash": payload_hash,
            },
        )
    return existing
