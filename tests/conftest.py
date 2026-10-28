from __future__ import annotations

from pathlib import Path

import pytest

from src.config import AppConfig
from src.services.runner import RunService


@pytest.fixture
def service(tmp_path: Path) -> RunService:
    config = AppConfig.model_validate(
        {
            "model": {"mode": "mock"},
            "limits": {
                "max_steps": 12,
                "max_tool_calls": 8,
                "max_steps_hard": 20,
                "max_tool_calls_hard": 12,
                "recursion_limit": 80,
                "run_timeout_seconds": 120,
            },
            "storage": {
                "database_path": str(tmp_path / "app.db"),
                "checkpoint_path": str(tmp_path / "checkpoints.sqlite3"),
                "plans_dir": str(tmp_path / "plans"),
                "traces_dir": str(tmp_path / "traces"),
            },
            "demo": {"fixed_now": "2026-11-01T09:00:00+08:00", "allow_failure_injection": True},
        }
    )
    instance = RunService(config)
    try:
        yield instance
    finally:
        instance.repo.close()
