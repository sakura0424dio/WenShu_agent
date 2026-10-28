from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _next_action(
    following: dict[str, Any] | None, state: dict[str, Any]
) -> dict[str, Any]:
    if following is not None:
        return {
            "decision_id": following.get("decision_id"),
            "action": following.get("action"),
            "tool": following.get("tool"),
            "reason_code": following.get("reason_code"),
        }
    incomplete = state.get("incomplete") or {}
    return {
        "decision_id": None,
        "action": "end",
        "tool": None,
        "reason_code": incomplete.get("code") or state.get("status"),
    }


def build_trace_events(state: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    events.append(
        {
            "event": "run_started",
            "run_id": state.get("run_id"),
            "parent_run_id": state.get("parent_run_id"),
            "attempt": state.get("attempt", 1),
            "model_mode": state.get("model_mode"),
            "clock_mode": state.get("clock_mode"),
            "started_at": state.get("started_at"),
            "resume_context": state.get("resume_context"),
        }
    )
    validation = state.get("validation")
    if validation is not None:
        events.append(
            {
                "event": "validation",
                "ok": validation.get("valid"),
                "missing_fields": validation.get("missing_fields", []),
                "invalid_fields": validation.get("invalid_fields", []),
            }
        )

    steps_by_decision: dict[str, list[dict[str, Any]]] = {}
    decision_ids = {str(item.get("decision_id")) for item in state.get("decisions", [])}
    for step in state.get("steps", []):
        if step.get("inherited_from_parent"):
            continue
        steps_by_decision.setdefault(step.get("decision_id", ""), []).append(step)

    for step in state.get("steps", []):
        if not step.get("inherited_from_parent") and str(step.get("decision_id")) in decision_ids:
            continue
        events.append(
            {
                "event": "inherited_step",
                "step_id": step.get("step_id"),
                "parent_decision_id": step.get("decision_id"),
                "tool": step.get("tool"),
                "arguments": step.get("arguments"),
                "status": step.get("status"),
                "result_summary": step.get("result_summary"),
                "error": step.get("error"),
                "reused": bool(step.get("from_parent_run")),
                "source_run_id": step.get("source_run_id"),
                "original_created_at": step.get("created_at"),
                "next_action": {
                    "decision_id": None,
                    "action": "reuse" if step.get("from_parent_run") else "history",
                    "tool": None,
                    "reason_code": (
                        "PARENT_STEP_REUSED"
                        if step.get("from_parent_run")
                        else "PARENT_STEP_NOT_REUSED"
                    ),
                },
            }
        )

    decisions = state.get("decisions", [])
    for index, decision in enumerate(decisions):
        following = decisions[index + 1] if index + 1 < len(decisions) else None
        next_action = _next_action(following, state)
        events.append(
            {
                "event": "model_action",
                "decision_id": decision.get("decision_id"),
                "step": decision.get("step"),
                "action": decision.get("action"),
                "tool": decision.get("tool"),
                "arguments": decision.get("arguments"),
                "reason_code": decision.get("reason_code"),
                "decision_basis": decision.get("decision_basis"),
                "model_mode": decision.get("model_mode"),
                "created_at": decision.get("created_at"),
            }
        )
        for step in steps_by_decision.get(decision.get("decision_id", ""), []):
            events.append(
                {
                    "event": "tool_call",
                    "decision_id": decision.get("decision_id"),
                    "step_id": step.get("step_id"),
                    "tool": step.get("tool"),
                    "arguments": step.get("arguments"),
                    "attempt": step.get("attempt"),
                    "injected": step.get("injected", False),
                    "from_parent_run": step.get("from_parent_run", False),
                }
            )
            events.append(
                {
                    "event": "tool_result",
                    "decision_id": decision.get("decision_id"),
                    "step_id": step.get("step_id"),
                    "status": step.get("status"),
                    "result_summary": step.get("result_summary"),
                    "error": step.get("error"),
                    "warnings": step.get("warnings", []),
                    "latency_ms": step.get("latency_ms"),
                    "injected": step.get("injected", False),
                    "from_parent_run": step.get("from_parent_run", False),
                    "next_action": next_action,
                }
            )

    events.append(
        {
            "event": "run_finished",
            "run_id": state.get("run_id"),
            "status": state.get("status"),
            "finished_at": state.get("finished_at"),
            "saved_plan_id": (state.get("saved_plan") or {}).get("plan_id"),
            "incomplete_code": (state.get("incomplete") or {}).get("code"),
            "decision_count": len(state.get("decisions", [])),
        }
    )
    return events


def write_trace(state: dict[str, Any], trace_dir: Path) -> tuple[str, list[dict[str, Any]]]:
    events = build_trace_events(state)
    trace_dir.mkdir(parents=True, exist_ok=True)
    path = trace_dir / f"{state.get('run_id')}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    return str(path), events
