from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.agents.base import AgentContext, ModelOutputError, ModelUnavailableError
from src.config import AppConfig
from src.domain.catalog import Catalog, money, money_str
from src.domain.clock import Clock
from src.domain.models import AgentAction, FailureInjection, ToolError, ToolMetrics, ToolResult
from src.domain.validation import validate_request
from src.graph.state import RunState
from src.tools.base import ToolRuntime, apply_partial, run_tool

VALID_TOOL_NAMES = {"search_materials", "generate_script", "estimate_cost", "save_plan"}
ACTIVE = "active"
SUPERSEDED = "superseded"
SUCCESS_STATES = {"succeeded", "partial"}


@dataclass
class GraphDeps:
    config: AppConfig
    catalog: Catalog
    clock: Clock
    agent: Any
    plans_dir: Any


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _task(tasks: list[dict[str, Any]], task_id: str) -> dict[str, Any] | None:
    for item in tasks:
        if item.get("task_id") == task_id:
            return item
    return None


def _upsert_task(
    tasks: list[dict[str, Any]],
    *,
    task_id: str,
    name: str,
    plan_id: str | None,
    status: str,
    depends_on: list[str],
    step_id: str | None,
) -> list[dict[str, Any]]:
    result = [dict(item) for item in tasks]
    current = _task(result, task_id)
    if current is None:
        current = {
            "task_id": task_id,
            "name": name,
            "plan_id": plan_id,
            "status": status,
            "depends_on": depends_on,
            "evidence_step_id": step_id,
            "branch": ACTIVE,
            "attempts": 0,
        }
        result.append(current)
    current.update(
        {
            "name": name,
            "plan_id": plan_id,
            "status": status,
            "depends_on": depends_on,
            "evidence_step_id": step_id,
            "branch": ACTIVE,
        }
    )
    current["attempts"] = int(current.get("attempts", 0)) + 1
    return result


def _switch_plan(tasks: list[dict[str, Any]], old_plan: str | None, new_plan: str) -> list[dict[str, Any]]:
    if old_plan == new_plan:
        return tasks
    result = [dict(item) for item in tasks]
    for item in result:
        if old_plan and item.get("plan_id") == old_plan and item.get("branch") == ACTIVE:
            item["branch"] = SUPERSEDED
    return result


def _active_plan_tasks(tasks: list[dict[str, Any]], plan_id: str) -> list[dict[str, Any]]:
    return [
        item
        for item in tasks
        if item.get("plan_id") == plan_id and item.get("branch") == ACTIVE
    ]


def can_finish(state: RunState) -> bool:
    if not state.get("saved_plan"):
        return False
    plan_id = state.get("selected_plan_id")
    if not plan_id:
        return False
    estimates = state.get("estimates") or {}
    estimate = estimates.get(plan_id)
    if not estimate or not estimate.get("within_budget") or not estimate.get("within_deadline"):
        return False
    tasks = state.get("tasks") or []
    search = _task(tasks, "search")
    if not search or search.get("status") not in SUCCESS_STATES:
        return False
    for task_id in (f"script:{plan_id}", f"estimate:{plan_id}", f"save:{plan_id}"):
        item = _task(tasks, task_id)
        if not item or item.get("branch") != ACTIVE:
            return False
        if item.get("status") not in SUCCESS_STATES:
            return False
        # save 的 partial 表示交付物已落盘但带告警，允许结束，终态会带 caveat。
    return True


def _observation(
    *,
    step_id: str | None,
    tool: str | None,
    status: str,
    summary: str,
    data: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    injected: bool = False,
    cache_hit: bool = False,
) -> dict[str, Any]:
    return {
        "observation_id": f"O{step_id or 'A'}-{datetime.now().strftime('%H%M%S%f')}",
        "step_id": step_id,
        "tool": tool,
        "status": status,
        "summary": summary,
        "data": data or {},
        "error": error,
        "warnings": warnings or [],
        "injected": injected,
        "cache_hit": cache_hit,
        "created_at": _now(),
    }


def prepare_node(state: RunState, deps: GraphDeps) -> dict[str, Any]:
    validation = validate_request(state.get("request") or {}, deps.clock)
    update: dict[str, Any] = {
        "validation": validation,
        "started_at": state.get("started_at") or _now(),
        "clock_mode": deps.clock.mode,
    }
    if not validation["valid"]:
        update.update({"status": "incomplete", "route": "finalize"})
        return update

    update.update(
        {
            "normalized": validation["normalized"],
            "status": "running",
            "route": "agent",
            "tool_attempts": state.get("tool_attempts") or {},
            "candidates": state.get("candidates") or [],
            "scripts": state.get("scripts") or {},
            "artifacts": state.get("artifacts") or {},
            "estimates": state.get("estimates") or {},
            "tasks": state.get("tasks") or [],
            "materials": state.get("materials") or [],
        }
    )
    injections = state.get("failure_injection") or []
    if injections and not deps.config.demo.allow_failure_injection:
        update["failure_injection"] = []
        update["caveats"] = ["服务端已禁用失败注入，本次请求的注入配置被忽略"]
    return update


def route_after_prepare(state: RunState) -> str:
    validation = state.get("validation") or {}
    return "agent" if validation.get("valid") else "finalize"


def _timed_out(state: RunState, deps: GraphDeps) -> bool:
    raw = state.get("started_at")
    if not raw:
        return False
    try:
        started = datetime.fromisoformat(str(raw))
        elapsed = (datetime.now().astimezone() - started).total_seconds()
    except ValueError:
        return False
    return elapsed > int(deps.config.limits.run_timeout_seconds)


def agent_node(state: RunState, deps: GraphDeps) -> dict[str, Any]:
    if state.get("forced_code"):
        return {"route": "finalize"}

    if _timed_out(state, deps):
        return {
            "forced_code": "RUN_TIMEOUT",
            "route": "finalize",
            "status": "incomplete",
        }

    max_steps = int(state.get("max_steps", deps.config.limits.max_steps))
    steps_used = int(state.get("steps_used", 0))
    if steps_used >= max_steps:
        return {
            "forced_code": "STEP_LIMIT_REACHED",
            "route": "finalize",
            "status": "incomplete",
        }

    try:
        action = deps.agent.next_action(AgentContext(state=dict(state), catalog=deps.catalog))
    except ModelUnavailableError:
        return {
            "forced_code": "MODEL_UNAVAILABLE",
            "route": "finalize",
            "status": "incomplete",
        }
    except ModelOutputError:
        return {
            "forced_code": "MODEL_OUTPUT_INVALID",
            "route": "finalize",
            "status": "incomplete",
        }
    except Exception:  # noqa: BLE001 - 真实模型异常必须形成未完成状态
        return {
            "forced_code": "MODEL_UNAVAILABLE",
            "route": "finalize",
            "status": "incomplete",
        }

    steps_used += 1
    decision_id = f"D{steps_used:03d}"
    decision = {
        "decision_id": decision_id,
        "step": steps_used,
        "action": action.action,
        "tool": action.tool_name,
        "arguments": action.arguments,
        "reason_code": action.reason_code,
        "decision_basis": action.decision_basis,
        "stop_reason": action.stop_reason,
        "model_mode": state.get("model_mode", deps.agent.mode),
        "created_at": _now(),
    }
    return {
        "steps_used": steps_used,
        "last_action": action.model_dump(),
        "pending_action": action.model_dump(),
        "decisions": [decision],
        "route": "guard",
    }


def _reject(
    state: RunState,
    *,
    tool: str | None,
    message: str,
    code: str,
    retryable: bool = False,
) -> dict[str, Any]:
    invalid = int(state.get("invalid_actions", 0)) + 1
    observation = _observation(
        step_id=None,
        tool=tool,
        status="failed",
        summary=message,
        error={"code": code, "message": message, "retryable": retryable, "details": {}},
    )
    update: dict[str, Any] = {
        "invalid_actions": invalid,
        "last_observation": observation,
        "observations": [observation],
    }
    if invalid >= 2:
        update.update(
            {
                "forced_code": "MODEL_OUTPUT_INVALID",
                "route": "finalize",
                "status": "incomplete",
            }
        )
    else:
        update["route"] = "agent"
    return update


def guard_node(state: RunState, deps: GraphDeps) -> dict[str, Any]:
    if state.get("forced_code"):
        return {"route": "finalize"}

    raw = state.get("pending_action") or {}
    try:
        action = AgentAction.model_validate(raw)
    except Exception:  # noqa: BLE001
        return _reject(state, tool=None, message="模型动作不符合 schema", code="INVALID_ACTION")

    if action.action == "stop":
        return {
            "stop_reason": action.stop_reason or "MODEL_STOPPED",
            "route": "finalize",
            "status": "incomplete",
        }

    if action.action == "finish":
        if can_finish(state):
            return {"route": "finalize"}
        return _reject(
            state,
            tool=None,
            message="方案尚未保存或仍有未完成任务，不能 finish",
            code="FINALIZE_PRECONDITION_FAILED",
        )

    tool = action.tool_name
    args = action.arguments
    if tool not in VALID_TOOL_NAMES:
        return _reject(state, tool=tool, message=f"未知工具: {tool}", code="UNKNOWN_TOOL")

    max_calls = int(state.get("max_tool_calls", deps.config.limits.max_tool_calls))
    if int(state.get("tool_calls", 0)) >= max_calls:
        return {
            "forced_code": "TOOL_CALL_LIMIT_REACHED",
            "route": "finalize",
            "status": "incomplete",
        }

    if tool == "generate_script":
        plan_id = args.get("plan_id")
        candidate = next(
            (item for item in state.get("candidates", []) if item.get("plan_id") == plan_id),
            None,
        )
        if not plan_id or not candidate or not candidate.get("available"):
            return _reject(
                state,
                tool=tool,
                message=f"方案不可用或缺少 plan_id: {plan_id}",
                code="PRECONDITION_FAILED",
            )
    elif tool == "estimate_cost":
        plan_id = args.get("plan_id")
        script = (state.get("scripts") or {}).get(plan_id)
        if not plan_id or not script:
            return _reject(
                state,
                tool=tool,
                message=f"缺少 {plan_id} 的脚本，不能估算",
                code="PRECONDITION_FAILED",
            )
        if args.get("script_id") and args.get("script_id") != script.get("script_id"):
            return _reject(
                state,
                tool=tool,
                message="script_id 与当前方案脚本不一致",
                code="PRECONDITION_FAILED",
            )
        args["script_id"] = script["script_id"]
    elif tool == "save_plan":
        plan_id = args.get("plan_id")
        estimate = (state.get("estimates") or {}).get(plan_id)
        script = (state.get("scripts") or {}).get(plan_id)
        if not plan_id or not estimate or not script:
            return _reject(
                state,
                tool=tool,
                message="缺少估算或脚本，不能保存",
                code="PRECONDITION_FAILED",
            )
        if args.get("estimate_id") and args.get("estimate_id") != estimate.get("estimate_id"):
            return _reject(state, tool=tool, message="estimate_id 不匹配", code="PRECONDITION_FAILED")
        if args.get("script_id") and args.get("script_id") != script.get("script_id"):
            return _reject(state, tool=tool, message="script_id 不匹配", code="PRECONDITION_FAILED")
        if not estimate.get("within_budget") or not estimate.get("within_deadline"):
            return _reject(
                state,
                tool=tool,
                message="估算不满足预算或截止时间，不能保存",
                code="PRECONDITION_FAILED",
            )
        args["estimate_id"] = estimate["estimate_id"]
        args["script_id"] = script["script_id"]

    return {
        "invalid_actions": 0,
        "pending_action": {**action.model_dump(), "arguments": args},
        "route": "execute",
    }


def route_after_agent(state: RunState) -> str:
    return "guard"


def route_after_guard(state: RunState) -> str:
    return state.get("route") or "agent"


def _injection_for(
    injections: list[dict[str, Any]], tool: str, attempt: int
) -> dict[str, Any] | None:
    for raw in injections:
        try:
            item = FailureInjection.model_validate(raw)
        except Exception:  # noqa: BLE001
            continue
        if item.tool != tool:
            continue
        if item.call_ordinal <= attempt < item.call_ordinal + item.times:
            return item.model_dump()
    return None


def _summary(tool: str, result: ToolResult) -> str:
    if result.status == "failed":
        error = result.error
        return f"{tool} 失败: {error.code if error else 'UNKNOWN'} - {error.message if error else ''}"
    data = result.data
    if tool == "search_materials":
        return f"检索到 {data.get('total_found', 0)} 个素材，可用方案: {', '.join(data.get('candidate_plan_ids', [])) or '无'}"
    if tool == "generate_script":
        return f"脚本 {data.get('script_id')} 生成完成，质量 {data.get('quality')}"
    if tool == "estimate_cost":
        verdict = "预算内" if data.get("within_budget") else "超预算"
        time_verdict = "时间可行" if data.get("within_deadline") else "时间不足"
        return f"{data.get('plan_id')} 合计 {data.get('total')}，{verdict}，{time_verdict}"
    if tool == "save_plan":
        return f"方案已保存: {data.get('plan_id')}（already_exists={data.get('already_exists')}）"
    return f"{tool} 完成: {result.status}"


def execute_node(state: RunState, deps: GraphDeps) -> dict[str, Any]:
    pending = state.get("pending_action") or {}
    tool = pending.get("tool_name")
    args = dict(pending.get("arguments") or {})
    if not tool:
        return {"forced_code": "INTERNAL_ERROR", "route": "finalize", "status": "incomplete"}

    attempt = int((state.get("tool_attempts") or {}).get(tool, 0)) + 1
    tool_attempts = dict(state.get("tool_attempts") or {})
    tool_attempts[tool] = attempt
    step_seq = int(state.get("step_seq", 0)) + 1
    step_id = f"S{step_seq:03d}"
    tool_calls = int(state.get("tool_calls", 0)) + 1

    tasks = list(state.get("tasks") or [])
    selected = state.get("selected_plan_id")
    plan_id = args.get("plan_id")
    if tool in {"generate_script", "estimate_cost", "save_plan"} and plan_id:
        tasks = _switch_plan(tasks, selected, str(plan_id))
        selected = str(plan_id)

    injection = _injection_for(state.get("failure_injection") or [], tool, attempt)
    if injection and injection.get("mode") == "error":
        result = ToolResult(
            status="failed",
            error=ToolError(
                code=injection.get("error_code", "TOOL_TIMEOUT"),
                message=injection.get("message", "注入的工具失败"),
                retryable=True,
                details={"injected": True},
            ),
            metrics=ToolMetrics(latency_ms=0),
            injected=True,
        )
    else:
        runtime = ToolRuntime(
            catalog=deps.catalog,
            state=dict(state),
            clock=deps.clock,
            plans_dir=deps.plans_dir,
        )
        result = run_tool(tool, args, runtime)
        if injection and injection.get("mode") == "partial" and result.status != "failed":
            data, warnings = apply_partial(tool, result.data, runtime)
            result = result.model_copy(
                update={
                    "status": "partial",
                    "data": data,
                    "warnings": list(result.warnings) + warnings,
                    "injected": True,
                }
            )

    task_id = None
    task_name = None
    depends: list[str] = []
    if tool == "search_materials":
        task_id, task_name = "search", "检索可用素材"
    elif tool == "generate_script":
        task_id, task_name = f"script:{plan_id}", f"生成 {plan_id} 脚本"
        depends = ["search"]
    elif tool == "estimate_cost":
        task_id, task_name = f"estimate:{plan_id}", f"估算 {plan_id} 成本"
        depends = [f"script:{plan_id}"]
    elif tool == "save_plan":
        task_id, task_name = f"save:{plan_id}", f"保存 {plan_id} 方案"
        depends = [f"estimate:{plan_id}"]

    task_status = {"success": "succeeded", "partial": "partial", "failed": "failed"}[result.status]
    if task_id:
        tasks = _upsert_task(
            tasks,
            task_id=task_id,
            name=task_name or tool,
            plan_id=str(plan_id) if plan_id else None,
            status=task_status,
            depends_on=depends,
            step_id=step_id,
        )

    existing_caveats = set(state.get("caveats") or [])
    new_caveats: list[str] = []
    if result.status == "failed":
        error = result.error
        caveat = f"{tool} 调用失败：{error.code if error else 'UNKNOWN'}（步骤 {step_id}）"
        if caveat not in existing_caveats:
            new_caveats.append(caveat)
    elif result.status == "partial":
        caveat = f"{tool} 返回部分结果：{', '.join(result.warnings) or 'PARTIAL'}（步骤 {step_id}）"
        if caveat not in existing_caveats:
            new_caveats.append(caveat)

    observation = _observation(
        step_id=step_id,
        tool=tool,
        status=result.status,
        summary=_summary(tool, result),
        data=result.data,
        error=result.error.model_dump() if result.error else None,
        warnings=result.warnings,
        injected=result.injected,
    )
    update: dict[str, Any] = {
        "step_seq": step_seq,
        "tool_calls": tool_calls,
        "tool_attempts": tool_attempts,
        "selected_plan_id": selected,
        "tasks": tasks,
        "caveats": new_caveats,
        "last_observation": observation,
        "steps": [
            {
                "step_id": step_id,
                "decision_id": f"D{state.get('steps_used', 0):03d}",
                "task_id": task_id,
                "tool": tool,
                "arguments": args,
                "attempt": attempt,
                "status": result.status,
                "result_summary": _summary(tool, result),
                "data": result.data,
                "error": result.error.model_dump() if result.error else None,
                "warnings": result.warnings,
                "injected": result.injected,
                "latency_ms": result.metrics.latency_ms,
                "created_at": _now(),
                "source_run_id": state.get("run_id"),
                "from_parent_run": False,
            }
        ],
        "observations": [observation],
        "pending_action": None,
    }

    if result.status != "failed":
        data = result.data
        if tool == "search_materials":
            update["candidates"] = data.get("candidate_plans", [])
            update["materials"] = data.get("materials", [])
        elif tool == "generate_script":
            scripts = dict(state.get("scripts") or {})
            scripts[str(plan_id)] = data
            artifacts = dict(state.get("artifacts") or {})
            artifacts[data["script_id"]] = data
            update["scripts"] = scripts
            update["artifacts"] = artifacts
        elif tool == "estimate_cost":
            estimates = dict(state.get("estimates") or {})
            estimates[str(plan_id)] = data
            update["estimates"] = estimates
        elif tool == "save_plan":
            update["saved_plan"] = data

    if int(state.get("steps_used", 0)) >= int(state.get("max_steps", deps.config.limits.max_steps)):
        update.update({"forced_code": "STEP_LIMIT_REACHED", "route": "finalize", "status": "incomplete"})
    elif (
        tool_calls >= int(state.get("max_tool_calls", deps.config.limits.max_tool_calls))
        and not update.get("saved_plan", state.get("saved_plan"))
    ):
        update.update({"forced_code": "TOOL_CALL_LIMIT_REACHED", "route": "finalize", "status": "incomplete"})
    else:
        update["route"] = "agent"
    return update


def route_after_execute(state: RunState) -> str:
    return state.get("route") or "agent"


def _ids(steps: list[dict[str, Any]], statuses: set[str]) -> list[str]:
    return [item["step_id"] for item in steps if item.get("status") in statuses]


def _remaining_tasks(state: RunState) -> list[str]:
    plan_id = state.get("selected_plan_id")
    tasks = state.get("tasks") or []
    scripts = state.get("scripts") or {}
    estimates = state.get("estimates") or {}
    remaining = [
        item["task_id"]
        for item in tasks
        if item.get("branch") == ACTIVE and item.get("status") not in SUCCESS_STATES
    ]
    if plan_id:
        script = scripts.get(plan_id)
        estimate = estimates.get(plan_id)
        if estimate and (
            not estimate.get("within_budget") or not estimate.get("within_deadline")
        ):
            # 当前方案不可行，剩余工作是改选替代方案，而不是保存一个超预算方案。
            alternatives = sorted(
                (
                    item
                    for item in state.get("candidates", [])
                    if item.get("available")
                    and item.get("plan_id") != plan_id
                    and item.get("plan_id") not in estimates
                ),
                key=lambda item: item.get("tier_rank", 99),
            )
            if alternatives:
                next_plan = alternatives[0]["plan_id"]
                if next_plan not in scripts:
                    remaining.append(f"script:{next_plan}")
                remaining.append(f"estimate:{next_plan}")
                remaining.append(f"save:{next_plan}")
            remaining = [item for item in remaining if item != f"save:{plan_id}"]
        else:
            save = _task(tasks, f"save:{plan_id}")
            if not script:
                remaining.append(f"script:{plan_id}")
            if script and not estimate:
                remaining.append(f"estimate:{plan_id}")
            if estimate and not save:
                remaining.append(f"save:{plan_id}")
    rank = {"search": 0, "script": 1, "estimate": 2, "save": 3}
    unique = list(dict.fromkeys(remaining))
    return sorted(unique, key=lambda item: (rank.get(item.split(":", 1)[0], 9), item))


def _incomplete(
    state: RunState,
    deps: GraphDeps,
    *,
    code: str,
    message: str,
    gap: dict[str, Any] | None = None,
    next_actions: list[str] | None = None,
) -> dict[str, Any]:
    steps = state.get("steps") or []
    failed = [item for item in steps if item.get("status") == "failed"]
    validation = state.get("validation") or {}
    base_gap = gap or {}
    if failed:
        base_gap.setdefault("failed_tool", failed[-1].get("tool"))
        base_gap.setdefault("error_code", (failed[-1].get("error") or {}).get("code"))
        base_gap.setdefault("retryable", (failed[-1].get("error") or {}).get("retryable", False))
        base_gap.setdefault("preserved_steps", _ids(steps, {"success", "partial"}))
    if code in {"STEP_LIMIT_REACHED", "TOOL_CALL_LIMIT_REACHED"}:
        base_gap.setdefault("limit", state.get("max_steps") if code == "STEP_LIMIT_REACHED" else state.get("max_tool_calls"))
        base_gap.setdefault("steps_used", state.get("steps_used"))
        base_gap.setdefault("tool_calls", state.get("tool_calls"))
        base_gap.setdefault("remaining_tasks", _remaining_tasks(state))
    return {
        "code": code,
        "message": message,
        "missing_fields": validation.get("missing_fields", []),
        "invalid_fields": validation.get("invalid_fields", []),
        "gap": base_gap,
        "completed_steps": _ids(steps, {"success", "partial"}),
        "failed_steps": [item["step_id"] for item in failed],
        "preserved_results": _ids(steps, {"success", "partial"}),
        "next_actions": next_actions or [],
    }


def finalize_node(state: RunState, deps: GraphDeps) -> dict[str, Any]:
    validation = state.get("validation") or {}
    if validation and not validation.get("valid"):
        missing = validation.get("missing_fields", [])
        invalid = validation.get("invalid_fields", [])
        if missing:
            code = "MISSING_REQUIRED_INPUT"
            message = f"缺少必填输入：{', '.join(missing)}"
            actions = [f"补充 {', '.join(missing)} 后重新提交"]
        else:
            code = "INVALID_INPUT"
            message = "输入不合法：" + "；".join(item.get("reason", "") for item in invalid)
            actions = ["修正输入后重新提交"]
        return {
            "status": "incomplete",
            "incomplete": _incomplete(
                state,
                deps,
                code=code,
                message=message,
                gap={"missing_fields": missing, "invalid_fields": invalid},
                next_actions=actions,
            ),
            "finished_at": _now(),
            "route": "end",
        }

    if state.get("saved_plan") and can_finish(state):
        return {
            "status": "completed_with_caveats" if state.get("caveats") else "completed",
            "incomplete": None,
            "finished_at": _now(),
            "route": "end",
        }

    forced = state.get("forced_code")
    stop_reason = state.get("stop_reason")
    estimates = state.get("estimates") or {}
    normalized = state.get("normalized") or {}
    budget = Decimal(str(normalized.get("budget", "0")))
    steps = state.get("steps") or []
    failed = [item for item in steps if item.get("status") == "failed"]
    code = forced or stop_reason
    gap: dict[str, Any] = {}
    next_actions: list[str] = []
    candidates = [item for item in state.get("candidates", []) if item.get("available")]
    all_infeasible = bool(candidates) and all(
        (estimates.get(item["plan_id"]) or {}).get("within_budget") is False
        or (estimates.get(item["plan_id"]) or {}).get("within_deadline") is False
        for item in candidates
    )

    if code == "NO_FEASIBLE_PLAN" or (not code and all_infeasible and not state.get("saved_plan")):
        code = "NO_FEASIBLE_PLAN"
        gap = deps.catalog.infeasible_gap(estimates, budget)
        message = "所有候选方案均不满足预算或截止时间。"
        min_cost = gap.get("minimum_feasible_cost")
        if min_cost:
            next_actions = [f"将预算提高至至少 {min_cost}，或延长截止时间后重试"]
    elif code == "SAVE_FAILED":
        message = "方案保存失败，交付物未落盘。"
        next_actions = ["检查 plans 目录写权限后从失败点续跑"]
    elif code == "TOOL_FAILURE_UNRECOVERED":
        message = "工具失败且没有可替代方案。"
        next_actions = ["排查工具错误，或清除注入后从失败点续跑"]
    elif code in {"STEP_LIMIT_REACHED", "TOOL_CALL_LIMIT_REACHED"}:
        message = "达到执行上限，任务未完成。"
        next_actions = ["提高上限后重新提交，或减少候选方案范围"]
    elif code == "RUN_TIMEOUT":
        message = "运行超过时间上限，任务未完成。"
        next_actions = ["检查模型/网络延迟后重试，或使用 Mock 模式验证流程"]
    elif code == "MODEL_UNAVAILABLE":
        message = "真实模型不可用或未配置，运行未完成。"
        next_actions = ["检查 config.yaml 的 DeepSeek 配置，或使用 MODEL_MODE=mock"]
    elif code == "MODEL_OUTPUT_INVALID":
        message = "模型连续输出非法动作，运行未完成。"
        next_actions = ["检查模型是否支持 JSON 输出，或降低 temperature 后重试"]
    elif code == "INTERNAL_ERROR":
        message = "运行发生内部错误，未完成任务。"
        next_actions = ["查看 trace 与服务日志后重试"]
    elif failed:
        last = failed[-1]
        code = "SAVE_FAILED" if last.get("tool") == "save_plan" else "TOOL_FAILURE_UNRECOVERED"
        message = f"工具 {last.get('tool')} 失败：{(last.get('error') or {}).get('code')}"
        next_actions = ["从失败点续跑或调整方案"]
    else:
        code = "MODEL_STOPPED"
        message = "模型主动停止，任务未完成。"
        next_actions = ["补充信息后重新提交"]

    incomplete = _incomplete(
        state,
        deps,
        code=code,
        message=message,
        gap=gap,
        next_actions=next_actions,
    )
    return {
        "status": "incomplete",
        "incomplete": incomplete,
        "finished_at": _now(),
        "route": "end",
    }
