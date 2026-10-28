from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from src.domain.clock import Clock


def validate_request(request: dict[str, Any], clock: Clock) -> dict[str, Any]:
    """校验并归一化输入。缺失字段绝不猜值。"""
    missing: list[str] = []
    invalid: list[dict[str, str]] = []

    topic_raw = request.get("topic")
    topic = str(topic_raw).strip() if topic_raw is not None else ""
    if not topic:
        missing.append("topic")
    elif len(topic) > 200:
        invalid.append({"field": "topic", "reason": "主题长度不能超过 200 字"})

    budget_raw = request.get("budget")
    budget: Decimal | None = None
    if budget_raw is None or (isinstance(budget_raw, str) and not budget_raw.strip()):
        missing.append("budget")
    else:
        try:
            budget = Decimal(str(budget_raw))
            if not budget.is_finite() or budget <= 0:
                invalid.append({"field": "budget", "reason": "预算必须是大于 0 的数字"})
            elif budget.as_tuple().exponent < -2:
                invalid.append({"field": "budget", "reason": "预算最多保留两位小数"})
        except (InvalidOperation, ValueError):
            invalid.append({"field": "budget", "reason": "预算不是合法数字"})

    deadline_raw = request.get("deadline")
    deadline: datetime | None = None
    if deadline_raw is None or (isinstance(deadline_raw, str) and not deadline_raw.strip()):
        missing.append("deadline")
    else:
        try:
            deadline = datetime.fromisoformat(str(deadline_raw).replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                invalid.append({"field": "deadline", "reason": "截止时间必须带时区偏移"})
            elif deadline <= clock.now():
                invalid.append({"field": "deadline", "reason": "截止时间必须晚于当前时间"})
        except ValueError:
            invalid.append({"field": "deadline", "reason": "截止时间不是合法 ISO 8601 时间"})

    valid = not missing and not invalid
    normalized = None
    if valid:
        normalized = {
            "topic": topic,
            "budget": format(budget, "f"),
            "deadline": deadline.isoformat() if deadline else None,
            "currency": "CNY",
            "clock_mode": clock.mode,
            "evaluated_at": clock.now().isoformat(),
        }
    return {
        "valid": valid,
        "missing_fields": missing,
        "invalid_fields": invalid,
        "normalized": normalized,
    }
