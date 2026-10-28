from __future__ import annotations

from typing import Any


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def build_response(
    state: dict[str, Any],
    *,
    trace_path: str | None = None,
) -> dict[str, Any]:
    normalized = state.get("normalized") or {}
    request = state.get("request") or {}
    selected = state.get("selected_plan_id")
    candidates = state.get("candidates") or []
    candidate = next((item for item in candidates if item.get("plan_id") == selected), None)
    tasks = state.get("tasks") or []
    active_tasks = [
        item
        for item in tasks
        if item.get("branch") == "active"
        and (item.get("task_id") == "search" or item.get("plan_id") == selected)
    ]
    superseded_tasks = [item for item in tasks if item.get("branch") == "superseded"]
    estimates = state.get("estimates") or {}
    estimate = estimates.get(selected) if selected else None
    saved_plan = state.get("saved_plan")
    caveats = state.get("caveats") or []
    steps = state.get("steps") or []
    decisions = state.get("decisions") or []

    plan = None
    if candidate:
        plan = {
            "selected_plan_id": selected,
            "title": f"{normalized.get('topic', '')} · {candidate.get('name', selected)}",
            "name": candidate.get("name"),
            "production_minutes": candidate.get("production_minutes"),
            "tasks": active_tasks,
        }
    elif selected:
        plan = {
            "selected_plan_id": selected,
            "title": normalized.get("topic"),
            "tasks": active_tasks,
        }

    cost = None
    time_info = None
    if estimate:
        cost = {
            "currency": estimate.get("currency", "CNY"),
            "pricing_version": estimate.get("pricing_version"),
            "estimate_id": estimate.get("estimate_id"),
            "line_items": estimate.get("line_items", []),
            "total": estimate.get("total"),
            "budget": estimate.get("budget"),
            "remaining": estimate.get("remaining"),
            "within_budget": estimate.get("within_budget"),
        }
        time_info = {
            "required_minutes": estimate.get("required_minutes"),
            "available_minutes": estimate.get("available_minutes"),
            "within_deadline": estimate.get("within_deadline"),
            "evaluated_at": estimate.get("evaluated_at"),
        }

    input_info = {
        "topic": normalized.get("topic", request.get("topic")),
        "budget": _jsonable(normalized.get("budget", request.get("budget"))),
        "deadline": _jsonable(normalized.get("deadline", request.get("deadline"))),
        "clock_mode": state.get("clock_mode"),
        "evaluated_at": normalized.get("evaluated_at"),
    }

    return {
        "run_id": state.get("run_id"),
        "parent_run_id": state.get("parent_run_id"),
        "attempt": state.get("attempt", 1),
        "status": state.get("status", "incomplete"),
        "model_mode": state.get("model_mode"),
        "disclaimer": state.get("disclaimer"),
        "input": input_info,
        "plan": plan,
        "steps": steps,
        "tool_results": {
            "materials": state.get("materials", []),
            "scripts": state.get("scripts", {}),
            "estimates": estimates,
            "saved_plan": saved_plan,
            "superseded_tasks": superseded_tasks,
        },
        "cost": cost,
        "time": time_info,
        "saved_plan": saved_plan,
        "incomplete": state.get("incomplete"),
        "caveats": caveats,
        "decisions": decisions,
        "reused_steps": [item["step_id"] for item in steps if item.get("from_parent_run")],
        "failure_injection": state.get("failure_injection") or [],
        "trace": {
            "path": trace_path,
            "decision_count": len(decisions),
        },
    }
