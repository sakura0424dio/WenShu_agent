from __future__ import annotations

from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from src import __version__
from src.api.schemas import HealthResponse, TraceResponse
from src.domain.models import ResumeRequest, RunRequest
from src.services.runner import RunService, ServiceError


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.service = RunService()
    try:
        yield
    finally:
        app.state.service.repo.close()


app = FastAPI(
    title="内容任务规划 Agent",
    version=__version__,
    description="在预算与截止时间内规划、估算并保存内容活动方案。",
    lifespan=lifespan,
)


def get_service(request: Request) -> RunService:
    return request.app.state.service


@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": str(exc)}},
    )


@app.get("/healthz", response_model=HealthResponse)
def healthz(service: RunService = Depends(get_service)) -> dict[str, Any]:
    return {
        "status": "ok",
        "version": __version__,
        "model_mode": service.config.model.mode,
        "clock_mode": service.clock.mode,
        "langgraph_version": _package_version("langgraph"),
        "data_versions": {
            "materials": service.catalog.materials_version,
            "pricing": service.catalog.pricing_version,
            "policy": service.catalog.policy_version,
            "rules_checksum": service.catalog.rules_checksum[:16],
        },
    }


@app.post("/v1/runs", response_model=None)
def create_run(
    payload: RunRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    service: RunService = Depends(get_service),
) -> dict[str, Any]:
    return service.start_run(payload, idempotency_key)


@app.get("/v1/runs/{run_id}", response_model=None)
def get_run(run_id: str, service: RunService = Depends(get_service)) -> dict[str, Any]:
    return service.get_run(run_id)


@app.get("/v1/runs/{run_id}/trace", response_model=TraceResponse)
def get_trace(run_id: str, service: RunService = Depends(get_service)) -> dict[str, Any]:
    return service.get_trace(run_id)


@app.post("/v1/runs/{run_id}/resume", response_model=None)
def resume_run(
    run_id: str,
    payload: ResumeRequest | None = None,
    service: RunService = Depends(get_service),
) -> dict[str, Any]:
    return service.resume(run_id, payload)


@app.get("/v1/plans/{plan_id}", response_model=None)
def get_plan(plan_id: str, service: RunService = Depends(get_service)) -> dict[str, Any]:
    return service.get_plan(plan_id)


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"service": "content-planning-agent", "docs": "/docs", "health": "/healthz"}
