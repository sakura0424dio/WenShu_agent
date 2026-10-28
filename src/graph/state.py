from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class RunState(TypedDict, total=False):
    run_id: str
    parent_run_id: str | None
    attempt: int
    request: dict[str, Any]
    options: dict[str, Any]
    normalized: dict[str, Any] | None
    validation: dict[str, Any] | None

    candidates: list[dict[str, Any]]
    selected_plan_id: str | None
    materials: list[dict[str, Any]]
    scripts: dict[str, dict[str, Any]]
    artifacts: dict[str, dict[str, Any]]
    estimates: dict[str, dict[str, Any]]
    saved_plan: dict[str, Any] | None

    tasks: list[dict[str, Any]]
    steps: Annotated[list[dict[str, Any]], operator.add]
    observations: Annotated[list[dict[str, Any]], operator.add]
    decisions: Annotated[list[dict[str, Any]], operator.add]
    caveats: Annotated[list[str], operator.add]
    last_observation: dict[str, Any] | None

    tool_calls: int
    steps_used: int
    step_seq: int
    invalid_actions: int
    tool_attempts: dict[str, int]
    max_steps: int
    max_tool_calls: int

    pending_action: dict[str, Any] | None
    stop_reason: str | None
    forced_code: str | None
    route: str
    status: str
    incomplete: dict[str, Any] | None
    resume_context: dict[str, Any] | None

    model_mode: str
    disclaimer: str
    failure_injection: list[dict[str, Any]]
    started_at: str
    finished_at: str | None
    clock_mode: str
