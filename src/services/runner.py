from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from src.agents.base import build_agent
from src.config import ROOT_DIR, AppConfig, get_config
from src.domain.catalog import Catalog, stable_hash
from src.domain.clock import Clock
from src.domain.models import ResumeRequest, RunRequest
from src.graph.build import build_graph
from src.graph.nodes import GraphDeps, finalize_node
from src.services.report import build_response
from src.services.trace import build_trace_events, write_trace
from src.storage.repo import RunRepository

MOCK_DISCLAIMER = "Mock 模式只验证流程、异常处理与状态一致性，不证明真实大模型的决策效果。"
DEEPSEEK_DISCLAIMER = "真实模型负责选择动作；费用、时间与步骤状态以工具返回为准。"
NON_RESUMABLE_CODES = {"MISSING_REQUIRED_INPUT", "INVALID_INPUT"}
PLAN_ID_PATTERN = re.compile(r"^plan_[0-9a-f]{12}$")


class ServiceError(Exception):
    status_code = 400
    code = "SERVICE_ERROR"


class RunNotFound(ServiceError):
    status_code = 404
    code = "RUN_NOT_FOUND"


class PlanNotFound(ServiceError):
    status_code = 404
    code = "PLAN_NOT_FOUND"


class IdempotencyConflict(ServiceError):
    status_code = 409
    code = "IDEMPOTENCY_KEY_CONFLICT"


class RunInProgress(ServiceError):
    status_code = 409
    code = "RUN_IN_PROGRESS"


class RunNotResumable(ServiceError):
    status_code = 409
    code = "RUN_NOT_RESUMABLE"


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:16]


def _max_step_number(steps: list[dict[str, Any]]) -> int:
    numbers = []
    for step in steps:
        match = re.fullmatch(r"S(\d+)", str(step.get("step_id", "")))
        if match:
            numbers.append(int(match.group(1)))
    return max(numbers, default=0)


class RunService:
    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()
        self.catalog = Catalog()
        self.clock = Clock(self.config.demo.fixed_now or None)
        self.database_path = self.config.resolve_path(self.config.storage.database_path)
        self.checkpoint_path = self.config.resolve_path(self.config.storage.checkpoint_path)
        self.plans_dir = self.config.resolve_path(self.config.storage.plans_dir)
        self.traces_dir = self.config.resolve_path(self.config.storage.traces_dir)
        self.repo = RunRepository(self.database_path)
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        self.traces_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 创建运行
    def start_run(self, request: RunRequest, idempotency_key: str | None = None) -> dict[str, Any]:
        payload = request.model_dump(mode="json")
        request_hash = stable_hash(payload, length=32)

        if idempotency_key:
            existing = self.repo.get_by_idempotency(idempotency_key)
            if existing:
                return self._replay(existing, request_hash)

        run_id = _new_run_id()
        inserted = self.repo.insert_running(
            run_id=run_id,
            parent_run_id=None,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            request_json=payload,
            attempt=1,
        )
        if not inserted:
            existing = self.repo.get_by_idempotency(idempotency_key) if idempotency_key else None
            if existing:
                return self._replay(existing, request_hash)
            raise ServiceError("无法创建运行记录")

        seed = self._initial_state(
            run_id=run_id,
            parent_run_id=None,
            attempt=1,
            payload=payload,
            model_mode=self._effective_mode(payload),
            failure_injection=list(payload.get("options", {}).get("failure_injection", [])),
            disclaimer=None,
        )
        return self._execute(
            run_id=run_id,
            initial_state=seed,
            model_mode=seed["model_mode"],
        )

    def _replay(self, existing: dict[str, Any], request_hash: str) -> dict[str, Any]:
        if existing["request_hash"] != request_hash:
            raise IdempotencyConflict("同一 Idempotency-Key 已用于不同请求内容")
        if existing.get("status") == "running" or not existing.get("response_json"):
            raise RunInProgress("相同幂等键的运行仍在执行中")
        response = copy.deepcopy(existing["response_json"])
        response["deduplicated"] = True
        return response

    # ------------------------------------------------------------------ 查询
    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self.repo.get(run_id)
        if not row:
            raise RunNotFound(f"运行不存在: {run_id}")
        if row.get("response_json"):
            return row["response_json"]
        return {
            "run_id": run_id,
            "parent_run_id": row.get("parent_run_id"),
            "attempt": row.get("attempt", 1),
            "status": "running",
            "message": "运行尚未完成",
        }

    def get_trace(self, run_id: str) -> dict[str, Any]:
        row = self.repo.get(run_id)
        if not row:
            raise RunNotFound(f"运行不存在: {run_id}")
        path = self.traces_dir / f"{run_id}.jsonl"
        if path.exists():
            events = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            return {"run_id": run_id, "path": self._display_path(path), "events": events}
        state = row.get("state_json")
        if not state:
            return {"run_id": run_id, "path": None, "events": []}
        events = build_trace_events(state)
        return {"run_id": run_id, "path": None, "events": events}

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        if not PLAN_ID_PATTERN.fullmatch(plan_id):
            raise PlanNotFound(f"非法方案 ID: {plan_id}")
        path = self.plans_dir / f"{plan_id}.json"
        if not path.exists():
            raise PlanNotFound(f"方案不存在: {plan_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ 续跑
    def resume(self, run_id: str, request: ResumeRequest | None = None) -> dict[str, Any]:
        parent = self.repo.get(run_id)
        if not parent:
            raise RunNotFound(f"运行不存在: {run_id}")
        if parent["status"] in {"completed", "completed_with_caveats"}:
            response = copy.deepcopy(parent.get("response_json") or {})
            response["resume_noop"] = True
            response["resume_note"] = "父运行已完成，未创建新运行"
            return response
        if parent["status"] == "running" or not parent.get("response_json"):
            raise RunInProgress("父运行仍在执行中，不能续跑")

        parent_response = parent["response_json"]
        code = (parent_response.get("incomplete") or {}).get("code")
        if code in NON_RESUMABLE_CODES:
            raise RunNotResumable(f"{code} 不能续跑，请补充输入后提交新请求")

        child = self.repo.find_child(run_id)
        if child:
            if child.get("response_json"):
                response = copy.deepcopy(child["response_json"])
                response["deduplicated"] = True
                return response
            raise RunInProgress("该父运行的续跑子运行仍在执行中")

        parent_state = parent.get("state_json")
        if not parent_state:
            raise RunNotResumable("父运行缺少可恢复状态")

        attempt = int(parent.get("attempt", 1)) + 1
        child_id = _new_run_id()
        payload = parent["request_json"]
        model_mode = parent_state.get("model_mode") or self._effective_mode(payload)
        failure_injection = (
            [item.model_dump() for item in request.failure_injection]
            if request is not None and request.failure_injection is not None
            else []
        )
        seed = self._resume_state(
            parent_state=parent_state,
            child_id=child_id,
            parent_id=run_id,
            attempt=attempt,
            payload=payload,
            model_mode=model_mode,
            failure_injection=failure_injection,
            parent_response=parent_response,
        )
        inserted = self.repo.insert_running(
            run_id=child_id,
            parent_run_id=run_id,
            idempotency_key=None,
            request_hash=parent["request_hash"],
            request_json=payload,
            attempt=attempt,
        )
        if not inserted:
            existing = self.repo.find_child(run_id)
            if existing and existing.get("response_json"):
                return existing["response_json"]
            raise RunInProgress("续跑子运行创建失败或正在执行")
        return self._execute(
            run_id=child_id,
            initial_state=seed,
            model_mode=model_mode,
        )

    # ------------------------------------------------------------------ 执行
    def _execute(
        self,
        *,
        run_id: str,
        initial_state: dict[str, Any],
        model_mode: str,
    ) -> dict[str, Any]:
        agent = build_agent(self.config, model_mode)
        deps = GraphDeps(
            config=self.config,
            catalog=self.catalog,
            clock=self.clock,
            agent=agent,
            plans_dir=self.plans_dir,
        )
        initial_state["disclaimer"] = (
            MOCK_DISCLAIMER if model_mode == "mock" else DEEPSEEK_DISCLAIMER
        )
        state = self._invoke(initial_state, deps, run_id, int(initial_state.get("attempt", 1)))
        trace_path, _events = write_trace(state, self.traces_dir)
        trace_display = self._display_path(Path(trace_path))
        response = build_response(state, trace_path=trace_display)
        response["deduplicated"] = False
        response["resume_noop"] = False
        self.repo.finish_run(
            run_id,
            status=str(response.get("status", "incomplete")),
            state=state,
            response=response,
        )
        return response

    def _invoke(
        self,
        initial_state: dict[str, Any],
        deps: GraphDeps,
        run_id: str,
        attempt: int,
    ) -> dict[str, Any]:
        from langgraph.checkpoint.sqlite import SqliteSaver

        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        thread_id = f"{run_id}:{attempt}"
        langgraph_config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": int(self.config.limits.recursion_limit),
        }
        with SqliteSaver.from_conn_string(str(self.checkpoint_path)) as checkpointer:
            graph = build_graph(deps, checkpointer)
            try:
                return dict(graph.invoke(initial_state, config=langgraph_config))
            except Exception as exc:  # noqa: BLE001 - 必须形成可审计的未完成报告
                try:
                    partial = dict(graph.get_state(langgraph_config).values)
                except Exception:  # noqa: BLE001
                    partial = dict(initial_state)
                partial.update(
                    {
                        "forced_code": "INTERNAL_ERROR",
                        "status": "incomplete",
                        "route": "finalize",
                    }
                )
                try:
                    finalized = finalize_node(partial, deps)  # type: ignore[arg-type]
                except Exception:  # noqa: BLE001
                    finalized = {
                        "status": "incomplete",
                        "incomplete": {
                            "code": "INTERNAL_ERROR",
                            "message": "运行与报告组装均发生内部异常。",
                            "missing_fields": [],
                            "invalid_fields": [],
                            "gap": {},
                            "completed_steps": [],
                            "failed_steps": [],
                            "preserved_results": [],
                            "next_actions": ["查看服务日志后重试"],
                        },
                        "finished_at": _now(),
                        "route": "end",
                    }
                partial.update(finalized)
                partial["caveats"] = list(partial.get("caveats") or []) + [
                    f"运行内部异常: {type(exc).__name__}"
                ]
                return partial

    # ------------------------------------------------------------------ 状态装配
    def _effective_mode(self, payload: dict[str, Any]) -> str:
        requested = (payload.get("options") or {}).get("model_mode")
        return str(requested or self.config.model.mode)

    def _limits(self, payload: dict[str, Any]) -> tuple[int, int]:
        options = payload.get("options") or {}
        requested_steps = int(options.get("max_steps") or self.config.limits.max_steps)
        requested_calls = int(options.get("max_tool_calls") or self.config.limits.max_tool_calls)
        max_steps = max(1, min(requested_steps, self.config.limits.max_steps_hard))
        max_calls = max(1, min(requested_calls, self.config.limits.max_tool_calls_hard))
        return max_steps, max_calls

    def _initial_state(
        self,
        *,
        run_id: str,
        parent_run_id: str | None,
        attempt: int,
        payload: dict[str, Any],
        model_mode: str,
        failure_injection: list[dict[str, Any]],
        disclaimer: str | None,
    ) -> dict[str, Any]:
        max_steps, max_calls = self._limits(payload)
        return {
            "run_id": run_id,
            "parent_run_id": parent_run_id,
            "attempt": attempt,
            "request": payload,
            "options": payload.get("options") or {},
            "normalized": None,
            "validation": None,
            "candidates": [],
            "selected_plan_id": None,
            "materials": [],
            "scripts": {},
            "artifacts": {},
            "estimates": {},
            "saved_plan": None,
            "tasks": [],
            "steps": [],
            "observations": [],
            "decisions": [],
            "caveats": [],
            "last_observation": None,
            "tool_calls": 0,
            "steps_used": 0,
            "step_seq": 0,
            "invalid_actions": 0,
            "tool_attempts": {},
            "max_steps": max_steps,
            "max_tool_calls": max_calls,
            "pending_action": None,
            "stop_reason": None,
            "forced_code": None,
            "route": "prepare",
            "status": "running",
            "incomplete": None,
            "resume_context": None,
            "model_mode": model_mode,
            "disclaimer": disclaimer
            or (MOCK_DISCLAIMER if model_mode == "mock" else DEEPSEEK_DISCLAIMER),
            "failure_injection": failure_injection,
            "started_at": _now(),
            "finished_at": None,
            "clock_mode": self.clock.mode,
        }

    def _resume_state(
        self,
        *,
        parent_state: dict[str, Any],
        child_id: str,
        parent_id: str,
        attempt: int,
        payload: dict[str, Any],
        model_mode: str,
        failure_injection: list[dict[str, Any]],
        parent_response: dict[str, Any],
    ) -> dict[str, Any]:
        seed = self._initial_state(
            run_id=child_id,
            parent_run_id=parent_id,
            attempt=attempt,
            payload=payload,
            model_mode=model_mode,
            failure_injection=failure_injection,
            disclaimer=parent_state.get("disclaimer"),
        )
        parent_steps = [copy.deepcopy(item) for item in parent_state.get("steps", [])]
        for item in parent_steps:
            item["source_run_id"] = parent_id
            item["inherited_from_parent"] = True
            if item.get("status") in {"success", "partial"}:
                item["from_parent_run"] = True
                item["reused"] = True
        reused_ids = [item["step_id"] for item in parent_steps if item.get("from_parent_run")]
        incomplete = parent_response.get("incomplete") or {}
        seed.update(
            {
                "candidates": copy.deepcopy(parent_state.get("candidates", [])),
                "selected_plan_id": parent_state.get("selected_plan_id"),
                "materials": copy.deepcopy(parent_state.get("materials", [])),
                "scripts": copy.deepcopy(parent_state.get("scripts", {})),
                "artifacts": copy.deepcopy(parent_state.get("artifacts", {})),
                "estimates": copy.deepcopy(parent_state.get("estimates", {})),
                "tasks": copy.deepcopy(parent_state.get("tasks", [])),
                "steps": parent_steps,
                "step_seq": _max_step_number(parent_steps),
                # 父运行的失败已在上一次尝试中记录并保留；新尝试只继承与部分成功相关的 caveat。
                "caveats": [
                    item
                    for item in parent_state.get("caveats", [])
                    if "返回部分结果" in item
                ],
                "resume_context": {
                    "parent_run_id": parent_id,
                    "parent_attempt": int(parent_state.get("attempt", 1)),
                    "parent_incomplete_code": incomplete.get("code"),
                    "reused_step_ids": reused_ids,
                    "failed_step_ids": incomplete.get("failed_steps", []),
                    "resume_reason": "从失败点继续，未重跑已成功工具",
                    "resume_instruction": (
                        "这是新的续跑尝试：父运行已成功的步骤已复用，父运行的失败步骤"
                        "仅作为历史保留；本次未启用失败注入，可以从父运行失败点重新调用"
                        "工具，不要仅因父运行历史失败而停止。"
                    ),
                },
            }
        )
        return seed

    @staticmethod
    def _display_path(path: Path) -> str:
        try:
            return str(path.resolve().relative_to(ROOT_DIR.resolve())).replace("\\", "/")
        except ValueError:
            return str(path)
