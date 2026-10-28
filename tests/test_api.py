from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from src.config import reset_config_cache


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = {
        "server": {"host": "127.0.0.1", "port": 8000},
        "model": {"mode": "mock", "provider": "deepseek", "api_key": ""},
        "limits": {
            "max_steps": 12,
            "max_tool_calls": 8,
            "max_steps_hard": 20,
            "max_tool_calls_hard": 12,
            "recursion_limit": 80,
            "run_timeout_seconds": 120,
        },
        "storage": {
            "database_path": str(tmp_path / "api.db"),
            "checkpoint_path": str(tmp_path / "api-checkpoints.sqlite3"),
            "plans_dir": str(tmp_path / "plans"),
            "traces_dir": str(tmp_path / "traces"),
        },
        "demo": {
            "fixed_now": "2026-11-01T09:00:00+08:00",
            "allow_failure_injection": True,
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("APP_CONFIG", str(config_path))
    reset_config_cache()

    from src.api.app import app

    with TestClient(app) as test_client:
        yield test_client
    reset_config_cache()


def payload(**overrides):
    value = {
        "topic": "春季露营装备种草",
        "budget": 10,
        "deadline": "2026-11-05T18:00:00+08:00",
        "options": {"model_mode": "mock", "max_steps": 12, "max_tool_calls": 8},
    }
    value.update(overrides)
    return value


def test_health_and_normal_flow(client: TestClient):
    health = client.get("/healthz")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert body["model_mode"] == "mock"
    assert body["clock_mode"] == "fixed"
    assert body["langgraph_version"]

    response = client.post(
        "/v1/runs",
        headers={"Idempotency-Key": "api-normal-1"},
        json=payload(budget=20),
    )
    assert response.status_code == 200
    report = response.json()
    assert report["status"] == "completed"
    assert report["saved_plan"]["plan_type"] == "P-STD"

    run_id = report["run_id"]
    fetched = client.get(f"/v1/runs/{run_id}")
    assert fetched.status_code == 200
    assert fetched.json()["run_id"] == run_id

    trace = client.get(f"/v1/runs/{run_id}/trace")
    assert trace.status_code == 200
    assert trace.json()["events"][0]["event"] == "run_started"

    plan = client.get(f"/v1/plans/{report['saved_plan']['plan_id']}")
    assert plan.status_code == 200
    assert plan.json()["content_hash"].startswith("sha256:")
    assert plan.json()["content"]["plan_id"] == "P-STD"


def test_business_incomplete_is_http_200(client: TestClient):
    response = client.post("/v1/runs", json={"topic": "缺预算", "deadline": "2026-11-05T18:00:00+08:00"})
    assert response.status_code == 200
    assert response.json()["status"] == "incomplete"
    assert response.json()["incomplete"]["missing_fields"] == ["budget"]

    invalid = client.post(
        "/v1/runs",
        json={"topic": "非法时间", "budget": 10, "deadline": "2026-11-05T18:00:00"},
    )
    assert invalid.status_code == 200
    assert invalid.json()["incomplete"]["code"] == "INVALID_INPUT"


def test_idempotency_and_not_found(client: TestClient):
    first = client.post("/v1/runs", headers={"Idempotency-Key": "api-idem"}, json=payload())
    second = client.post("/v1/runs", headers={"Idempotency-Key": "api-idem"}, json=payload())
    assert first.status_code == second.status_code == 200
    assert second.json()["deduplicated"] is True
    assert second.json()["run_id"] == first.json()["run_id"]

    conflict = client.post(
        "/v1/runs",
        headers={"Idempotency-Key": "api-idem"},
        json=payload(topic="不同主题"),
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    missing = client.get("/v1/runs/run_missing")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "RUN_NOT_FOUND"

    bad_plan = client.get("/v1/plans/..%2Fsecret")
    assert bad_plan.status_code in {404, 422}


def test_resume_completed_is_noop(client: TestClient):
    response = client.post("/v1/runs", json=payload(budget=20))
    report = response.json()
    resumed = client.post(f"/v1/runs/{report['run_id']}/resume", json={})
    assert resumed.status_code == 200
    assert resumed.json()["resume_noop"] is True
    assert resumed.json()["run_id"] == report["run_id"]
