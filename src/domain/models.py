from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ToolName = Literal["search_materials", "generate_script", "estimate_cost", "save_plan"]


class FailureInjection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: ToolName
    mode: Literal["error", "partial"] = "error"
    call_ordinal: int = Field(default=1, ge=1)
    times: int = Field(default=1, ge=1, le=999)
    error_code: str = "TOOL_TIMEOUT"
    message: str = "注入的工具失败"
    warnings: list[str] = Field(default_factory=list)


class RunOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_mode: Literal["mock", "deepseek"] | None = None
    max_steps: int | None = Field(default=None, ge=1, le=100)
    max_tool_calls: int | None = Field(default=None, ge=1, le=100)
    failure_injection: list[FailureInjection] = Field(default_factory=list)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: str | None = None
    # 保持宽松类型：非法数字/时间进入业务校验，返回明确的 INVALID_INPUT，
    # 而不是在 HTTP 层直接被 422 吞掉。
    budget: Decimal | str | None = None
    deadline: datetime | str | None = None
    options: RunOptions = Field(default_factory=RunOptions)


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    failure_injection: list[FailureInjection] | None = None


class AgentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["call_tool", "finish", "stop"]
    tool_name: ToolName | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason_code: str = "INITIAL_DISCOVERY"
    decision_basis: str = Field(default="", max_length=300)
    stop_reason: str | None = None


class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ToolMetrics(BaseModel):
    latency_ms: int = 0


class ToolResult(BaseModel):
    status: Literal["success", "partial", "failed"]
    data: dict[str, Any] = Field(default_factory=dict)
    error: ToolError | None = None
    warnings: list[str] = Field(default_factory=list)
    metrics: ToolMetrics = Field(default_factory=ToolMetrics)
    injected: bool = False
