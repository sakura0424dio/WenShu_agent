from __future__ import annotations

from datetime import datetime, timezone


class Clock:
    """可注入时钟，保证示例与测试可复现。"""

    def __init__(self, fixed_now: str | None = None) -> None:
        self.fixed = self._parse(fixed_now) if fixed_now else None

    @staticmethod
    def _parse(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("fixed_now 必须带时区偏移，例如 2026-11-01T09:00:00+08:00")
        return parsed

    @property
    def mode(self) -> str:
        return "fixed" if self.fixed else "system"

    def now(self) -> datetime:
        if self.fixed:
            return self.fixed
        return datetime.now(timezone.utc).astimezone()
