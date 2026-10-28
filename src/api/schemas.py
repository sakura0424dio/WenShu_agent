from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class TraceResponse(BaseModel):
    run_id: str
    path: str | None
    events: list[dict[str, Any]] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    version: str
    model_mode: str
    clock_mode: str
    langgraph_version: str
    data_versions: dict[str, str]
