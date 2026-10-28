from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from src.domain.models import FailureInjection, ResumeRequest, RunRequest
from src.services.runner import IdempotencyConflict, RunNotResumable, RunService

DEADLINE = "2026-11-05T18:00:00+08:00"
TOPIC = "春季露营装备种草"


def request_payload(**overrides):
    payload = {
        "topic": TOPIC,
        "budget": 10,
        "deadline": DEADLINE,
        "options": {"model_mode": "mock", "max_steps": 12, "max_tool_calls": 8},
    }
    payload.update(overrides)
    return payload


def run(service: RunService, **overrides):
    return service.start_run(RunRequest.model_validate(request_payload(**overrides)))


def test_a01_normal_completion(service: RunService):
    response = run(service, budget=20)

    assert response["status"] == "completed"
    assert response["incomplete"] is None
    assert response["saved_plan"]["plan_type"] == "P-STD"
    assert response["cost"]["total"] == "12.00"
    assert response["cost"]["within_budget"] is True
    assert response["time"]["required_minutes"] == 180
    assert response["time"]["available_minutes"] == 6300
    assert response["time"]["within_deadline"] is True

    tasks = {item["task_id"]: item for item in response["plan"]["tasks"]}
    assert set(tasks) == {
        "search",
        "script:P-STD",
        "estimate:P-STD",
        "save:P-STD",
    }
    assert all(item["status"] == "succeeded" for item in tasks.values())
    assert all(step["status"] != "failed" for step in response["steps"])

    line_total = sum(Decimal(item["amount"]) for item in response["cost"]["line_items"])
    assert line_total == Decimal(response["cost"]["total"])
    assert response["saved_plan"]["estimate_id"] == response["cost"]["estimate_id"]


def test_a02_missing_budget_is_not_guessed(service: RunService):
    payload = request_payload()
    payload.pop("budget")
    response = service.start_run(RunRequest.model_validate(payload))

    assert response["status"] == "incomplete"
    assert response["incomplete"]["code"] == "MISSING_REQUIRED_INPUT"
    assert response["incomplete"]["missing_fields"] == ["budget"]
    assert response["steps"] == []
    assert response["tool_results"]["estimates"] == {}
    assert any("budget" in item for item in response["incomplete"]["next_actions"])


def test_a02_missing_deadline_and_naive_deadline(service: RunService):
    payload = request_payload()
    payload.pop("deadline")
    missing = service.start_run(RunRequest.model_validate(payload))
    assert missing["incomplete"]["missing_fields"] == ["deadline"]

    naive = run(service, deadline="2026-11-05T18:00:00")
    assert naive["status"] == "incomplete"
    assert naive["incomplete"]["code"] == "INVALID_INPUT"
    assert naive["incomplete"]["invalid_fields"][0]["field"] == "deadline"
    assert naive["steps"] == []


def test_a03_script_failure_triggers_replan_and_preserves_results(service: RunService):
    response = run(
        service,
        options={
            "model_mode": "mock",
            "max_steps": 12,
            "max_tool_calls": 8,
            "failure_injection": [
                FailureInjection(
                    tool="generate_script",
                    mode="error",
                    call_ordinal=1,
                    times=1,
                    error_code="TOOL_TIMEOUT",
                ).model_dump()
            ],
        },
    )

    assert response["status"] == "completed_with_caveats"
    assert response["plan"]["selected_plan_id"] == "P-LITE"
    failed = [item for item in response["steps"] if item["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["tool"] == "generate_script"
    assert failed[0]["arguments"]["plan_id"] == "P-STD"
    assert failed[0]["injected"] is True

    superseded = {item["task_id"]: item for item in response["tool_results"]["superseded_tasks"]}
    assert superseded["script:P-STD"]["status"] == "failed"
    assert all(item["status"] != "succeeded" for item in superseded.values())

    tasks = {item["task_id"]: item for item in response["plan"]["tasks"]}
    assert tasks["search"]["status"] == "succeeded"
    assert tasks["save:P-LITE"]["status"] == "succeeded"
    assert any("TOOL_TIMEOUT" in item for item in response["caveats"])
    assert any(
        item["reason_code"] == "TOOL_ERROR_REPLAN" for item in response["decisions"]
    )


def test_a04_a06_budget_overrun_replans_to_lite(service: RunService):
    response = run(service, budget=10)

    assert response["status"] == "completed"
    assert response["plan"]["selected_plan_id"] == "P-LITE"
    assert response["cost"]["total"] == "8.00"
    assert response["cost"]["budget"] == "10.00"
    assert response["cost"]["remaining"] == "2.00"
    assert response["cost"]["within_budget"] is True
    assert response["saved_plan"]["plan_type"] == "P-LITE"

    estimates = response["tool_results"]["estimates"]
    assert estimates["P-STD"]["total"] == "12.00"
    assert estimates["P-STD"]["within_budget"] is False
    assert estimates["P-LITE"]["total"] == "8.00"
    assert estimates["P-LITE"]["within_budget"] is True

    replan = [
        item for item in response["decisions"] if item["reason_code"] == "COST_OVER_BUDGET_REPLAN"
    ]
    assert len(replan) == 1
    assert replan[0]["tool"] == "generate_script"
    assert replan[0]["arguments"]["plan_id"] == "P-LITE"

    superseded = {item["task_id"]: item for item in response["tool_results"]["superseded_tasks"]}
    assert superseded["estimate:P-STD"]["status"] == "succeeded"


def test_a05_idempotency_and_content_deduplication(service: RunService):
    payload = RunRequest.model_validate(request_payload())
    first = service.start_run(payload, "same-key")
    second = service.start_run(payload, "same-key")
    assert second["run_id"] == first["run_id"]
    assert second["deduplicated"] is True
    assert second["saved_plan"]["plan_id"] == first["saved_plan"]["plan_id"]

    with pytest.raises(IdempotencyConflict):
        unchanged = request_payload()
        unchanged["topic"] = "另一个主题"
        service.start_run(RunRequest.model_validate(unchanged), "same-key")

    first_again = service.start_run(payload)
    assert first_again["run_id"] != first["run_id"]
    assert first_again["saved_plan"]["plan_id"] == first["saved_plan"]["plan_id"]
    assert first_again["saved_plan"]["already_exists"] is True


def test_a07_no_feasible_plan_reports_gap(service: RunService):
    response = run(service, budget=5)

    assert response["status"] == "incomplete"
    assert response["incomplete"]["code"] == "NO_FEASIBLE_PLAN"
    assert response["incomplete"]["gap"]["minimum_feasible_cost"] == "8.00"
    assert response["incomplete"]["gap"]["budget_shortfall"] == "3.00"
    assert response["saved_plan"] is None
    assert response["incomplete"]["failed_steps"] == []
    assert {
        item["plan_id"] for item in response["incomplete"]["gap"]["evaluated_plans"]
    } == {"P-STD", "P-LITE", "P-VIDEO"}
    assert all(
        item["within_budget"] is False
        for item in response["incomplete"]["gap"]["evaluated_plans"]
    )


def test_a08_resume_reuses_successful_steps(service: RunService):
    injection = FailureInjection(
        tool="save_plan",
        mode="error",
        call_ordinal=1,
        times=99,
        error_code="TOOL_TIMEOUT",
        message="注入：保存服务持续超时",
    )
    parent = run(
        service,
        options={
            "model_mode": "mock",
            "max_steps": 12,
            "max_tool_calls": 8,
            "failure_injection": [injection.model_dump()],
        },
    )
    assert parent["status"] == "incomplete"
    assert parent["incomplete"]["code"] == "SAVE_FAILED"
    assert parent["saved_plan"] is None
    assert [item["tool"] for item in parent["steps"]].count("save_plan") == 2
    reused_ids = [
        item["step_id"]
        for item in parent["steps"]
        if item["status"] in {"success", "partial"}
    ]

    child = service.resume(parent["run_id"], ResumeRequest(failure_injection=[]))
    assert child["status"] == "completed"
    assert child["parent_run_id"] == parent["run_id"]
    assert child["attempt"] == 2
    assert child["saved_plan"]["plan_type"] == "P-LITE"
    assert set(reused_ids).issubset(set(child["reused_steps"]))

    inherited = {
        item["step_id"]: item
        for item in child["steps"]
        if item.get("from_parent_run") is True
    }
    assert set(inherited) == set(reused_ids)
    parent_failed = {
        item["step_id"] for item in parent["steps"] if item["status"] == "failed"
    }
    assert parent_failed
    assert all(
        item.get("inherited_from_parent") is True
        and item.get("from_parent_run") is False
        for item in child["steps"]
        if item["step_id"] in parent_failed
    )
    assert all(
        item["tool"] not in {"generate_script", "estimate_cost"}
        for item in child["steps"]
        if not item.get("from_parent_run") and item["status"] == "success"
    )

    repeated = service.resume(parent["run_id"], ResumeRequest(failure_injection=[]))
    assert repeated["run_id"] == child["run_id"]
    assert repeated["deduplicated"] is True

    noop = service.resume(child["run_id"], ResumeRequest(failure_injection=[]))
    assert noop["resume_noop"] is True
    assert noop["run_id"] == child["run_id"]


def test_a08_non_resumable_missing_input(service: RunService):
    payload = request_payload()
    payload.pop("deadline")
    parent = service.start_run(RunRequest.model_validate(payload))
    with pytest.raises(RunNotResumable):
        service.resume(parent["run_id"])


def test_partial_search_degrades_plan_and_marks_task_partial(service: RunService):
    response = run(
        service,
        options={
            "model_mode": "mock",
            "failure_injection": [
                {"tool": "search_materials", "mode": "partial", "call_ordinal": 1, "times": 1}
            ],
        },
    )

    assert response["status"] == "completed_with_caveats"
    assert response["plan"]["selected_plan_id"] == "P-LITE"
    steps = {item["step_id"]: item for item in response["steps"]}
    assert steps["S001"]["status"] == "partial"
    tasks = {item["task_id"]: item for item in response["plan"]["tasks"]}
    assert tasks["search"]["status"] == "partial"
    assert tasks["save:P-LITE"]["status"] == "succeeded"
    assert any("PARTIAL_RESULTS" in item for item in response["caveats"])


def test_tool_call_limit_preserves_state_and_reports_remaining_work(service: RunService):
    response = run(
        service,
        options={"model_mode": "mock", "max_steps": 12, "max_tool_calls": 3},
    )
    assert response["status"] == "incomplete"
    assert response["incomplete"]["code"] == "TOOL_CALL_LIMIT_REACHED"
    assert response["incomplete"]["gap"]["limit"] == 3
    assert response["incomplete"]["gap"]["tool_calls"] == 3
    assert response["incomplete"]["gap"]["remaining_tasks"] == [
        "script:P-LITE",
        "estimate:P-LITE",
        "save:P-LITE",
    ]
    assert response["saved_plan"] is None
    assert all(step["status"] != "failed" for step in response["steps"])


def test_trace_is_written_and_contains_dynamic_replan(service: RunService):
    response = run(service, budget=10)
    trace = service.get_trace(response["run_id"])
    events = trace["events"]
    assert events[0]["event"] == "run_started"
    assert any(item["event"] == "tool_result" and item["status"] == "success" for item in events)
    replans = [
        item
        for item in events
        if item["event"] == "model_action" and item["reason_code"] == "COST_OVER_BUDGET_REPLAN"
    ]
    assert replans
    tool_results = [item for item in events if item["event"] == "tool_result"]
    assert tool_results
    assert all(item["decision_id"] for item in tool_results)
    assert all(item["next_action"]["action"] for item in tool_results)
    replan_index = events.index(replans[0])
    assert events[replan_index - 1]["event"] == "tool_result"
    assert events[replan_index - 1]["next_action"]["decision_id"] == replans[0]["decision_id"]
    assert events[replan_index - 1]["next_action"]["action"] == "call_tool"
    assert events[-1]["event"] == "run_finished"
    assert events[-1]["status"] == "completed"


def test_resume_trace_marks_inherited_steps(service: RunService):
    injection = {
        "tool": "save_plan",
        "mode": "error",
        "call_ordinal": 1,
        "times": 99,
    }
    parent = run(
        service,
        options={
            "model_mode": "mock",
            "failure_injection": [injection],
        },
    )
    child = service.resume(parent["run_id"], ResumeRequest(failure_injection=[]))
    events = service.get_trace(child["run_id"])["events"]
    inherited = [item for item in events if item["event"] == "inherited_step"]
    assert inherited
    assert any(item["reused"] is True for item in inherited)
    assert any(item["reused"] is False and item["status"] == "failed" for item in inherited)
    assert all(item["source_run_id"] == parent["run_id"] for item in inherited)
    inherited_ids = {item["step_id"] for item in inherited}
    assert set(child["reused_steps"]) <= inherited_ids
    assert {item["step_id"] for item in child["steps"] if item["status"] == "failed"} <= inherited_ids
    tool_results = [item for item in events if item["event"] == "tool_result"]
    assert tool_results
    assert all(item["from_parent_run"] is False for item in tool_results)
    assert not inherited_ids & {item["step_id"] for item in tool_results}
    assert all(item["next_action"]["action"] for item in inherited)


def test_final_estimate_time_is_reproducible(service: RunService):
    response = run(service, budget=10)
    evaluated_at = datetime.fromisoformat(response["time"]["evaluated_at"])
    assert evaluated_at.isoformat() == "2026-11-01T09:00:00+08:00"
    assert response["time"]["available_minutes"] == 6300
