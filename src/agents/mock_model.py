from __future__ import annotations

from typing import Any

from src.agents.base import AgentContext
from src.domain.models import AgentAction


class MockModel:
    """离线规则策略。

    注意：它读取真实工具观察来改变下一步，用于验证流程与异常处理，
    不能证明真实大模型的决策效果。
    """

    mode = "mock"

    def next_action(self, context: AgentContext) -> AgentAction:
        state = context.state
        if state.get("saved_plan"):
            return AgentAction(
                action="finish",
                reason_code="ALL_DONE",
                decision_basis="方案已保存，可以结束。",
            )

        observation = state.get("last_observation") or {}
        observation_status = observation.get("status")
        observation_tool = observation.get("tool")

        if observation_status == "failed":
            return self._after_failure(state, observation, observation_tool)

        candidates = [item for item in state.get("candidates", []) if item.get("available")]
        if not candidates:
            attempts = int((state.get("tool_attempts") or {}).get("search_materials", 0))
            if attempts < 2:
                return self._search(
                    state,
                    reason="INITIAL_DISCOVERY" if attempts == 0 else "RETRY_TRANSIENT",
                    basis="尚未获得可用素材，先检索。",
                )
            return AgentAction(
                action="stop",
                reason_code="STOP_UNRECOVERABLE",
                decision_basis="素材检索失败，无法继续。",
                stop_reason="TOOL_FAILURE_UNRECOVERED",
            )

        plan_id = state.get("selected_plan_id")
        if not plan_id or plan_id not in {item["plan_id"] for item in candidates}:
            plan_id = self._initial_plan(candidates)
            if not plan_id:
                return AgentAction(
                    action="stop",
                    reason_code="NO_FEASIBLE_PLAN",
                    decision_basis="没有可用素材支撑任何候选方案。",
                    stop_reason="NO_FEASIBLE_PLAN",
                )

        plan = self._candidate(candidates, plan_id)
        scripts = state.get("scripts") or {}
        estimates = state.get("estimates") or {}
        script = scripts.get(plan_id)
        estimate = estimates.get(plan_id)

        if not script:
            return self._script(state, plan, reason="GENERATE_SCRIPT", basis="为当前方案生成脚本。")

        if not estimate:
            return self._estimate(
                state,
                plan,
                script,
                reason="VERIFY_COST",
                basis="脚本已生成，调用估算工具确认成本与工期。",
            )

        if not estimate.get("within_budget") or not estimate.get("within_deadline"):
            alternative = self._alternative(candidates, estimates, plan_id)
            if alternative:
                reason = (
                    "COST_OVER_BUDGET_REPLAN"
                    if not estimate.get("within_budget")
                    else "TIME_OVER_DEADLINE_REPLAN"
                )
                basis = (
                    f"{plan_id} 估算 {estimate.get('total')}，不满足约束；改用 {alternative['plan_id']} 重新评估。"
                )
                return self._script(state, alternative, reason=reason, basis=basis)
            return AgentAction(
                action="stop",
                reason_code="NO_FEASIBLE_PLAN",
                decision_basis="所有可用候选方案均不满足预算或截止时间。",
                stop_reason="NO_FEASIBLE_PLAN",
            )

        return self._save(state, plan, script, estimate)

    def _after_failure(self, state: dict[str, Any], observation: dict[str, Any], tool: str | None) -> AgentAction:
        attempts = int((state.get("tool_attempts") or {}).get(tool or "", 0))
        if tool == "save_plan":
            if attempts < 2:
                return self._retry_save(state, "保存工具失败，按错误信息重试一次。")
            return AgentAction(
                action="stop",
                reason_code="STOP_UNRECOVERABLE",
                decision_basis=f"保存失败且重试无效：{observation.get('error', {}).get('code')}",
                stop_reason="SAVE_FAILED",
            )
        if tool == "search_materials":
            if attempts < 2:
                return self._search(state, reason="RETRY_TRANSIENT", basis="检索失败，重试一次。")
            return AgentAction(
                action="stop",
                reason_code="STOP_UNRECOVERABLE",
                decision_basis="素材检索连续失败，停止。",
                stop_reason="TOOL_FAILURE_UNRECOVERED",
            )

        candidates = [item for item in state.get("candidates", []) if item.get("available")]
        estimates = state.get("estimates") or {}
        current = state.get("selected_plan_id")
        alternative = self._alternative(candidates, estimates, current)
        if alternative:
            basis = f"{tool} 对 {current} 失败，改用替代方案 {alternative['plan_id']}。"
            return self._script(
                state,
                alternative,
                reason="TOOL_ERROR_REPLAN",
                basis=basis,
            )
        return AgentAction(
            action="stop",
            reason_code="STOP_UNRECOVERABLE",
            decision_basis=f"{tool} 失败且没有可替代方案。",
            stop_reason="TOOL_FAILURE_UNRECOVERED",
        )

    def _search(self, state: dict[str, Any], *, reason: str, basis: str) -> AgentAction:
        normalized = state.get("normalized") or {}
        return AgentAction(
            action="call_tool",
            tool_name="search_materials",
            arguments={"topic": normalized.get("topic", ""), "keywords": [], "limit": 6},
            reason_code=reason,
            decision_basis=basis,
        )

    def _script(
        self,
        state: dict[str, Any],
        plan: dict[str, Any],
        *,
        reason: str,
        basis: str,
    ) -> AgentAction:
        return AgentAction(
            action="call_tool",
            tool_name="generate_script",
            arguments={
                "plan_id": plan["plan_id"],
                "material_ids": plan.get("material_ids", []),
                "tone": "轻松种草",
                "duration_seconds": 45,
            },
            reason_code=reason,
            decision_basis=basis,
        )

    def _estimate(
        self,
        state: dict[str, Any],
        plan: dict[str, Any],
        script: dict[str, Any],
        *,
        reason: str,
        basis: str,
    ) -> AgentAction:
        return AgentAction(
            action="call_tool",
            tool_name="estimate_cost",
            arguments={
                "plan_id": plan["plan_id"],
                "material_ids": script.get("material_ids", plan.get("material_ids", [])),
                "script_id": script["script_id"],
            },
            reason_code=reason,
            decision_basis=basis,
        )

    def _save(
        self,
        state: dict[str, Any],
        plan: dict[str, Any],
        script: dict[str, Any],
        estimate: dict[str, Any],
    ) -> AgentAction:
        return AgentAction(
            action="call_tool",
            tool_name="save_plan",
            arguments={
                "plan_id": plan["plan_id"],
                "estimate_id": estimate["estimate_id"],
                "script_id": script["script_id"],
                "material_ids": script.get("material_ids", []),
            },
            reason_code="SAVE_DELIVERABLE",
            decision_basis=f"{plan['plan_id']} 估算 {estimate['total']} 在预算内，保存方案。",
        )

    def _retry_save(self, state: dict[str, Any], basis: str) -> AgentAction:
        plan_id = state.get("selected_plan_id")
        scripts = state.get("scripts") or {}
        estimates = state.get("estimates") or {}
        script = scripts.get(plan_id)
        estimate = estimates.get(plan_id)
        if not script or not estimate:
            return AgentAction(
                action="stop",
                reason_code="STOP_UNRECOVERABLE",
                decision_basis="缺少脚本或估算，无法重试保存。",
                stop_reason="SAVE_FAILED",
            )
        return AgentAction(
            action="call_tool",
            tool_name="save_plan",
            arguments={
                "plan_id": plan_id,
                "estimate_id": estimate["estimate_id"],
                "script_id": script["script_id"],
                "material_ids": script.get("material_ids", []),
            },
            reason_code="RETRY_TRANSIENT",
            decision_basis=basis,
        )

    @staticmethod
    def _candidate(candidates: list[dict[str, Any]], plan_id: str) -> dict[str, Any]:
        for item in candidates:
            if item["plan_id"] == plan_id:
                return item
        raise KeyError(plan_id)

    @staticmethod
    def _initial_plan(candidates: list[dict[str, Any]]) -> str | None:
        by_id = {item["plan_id"]: item for item in candidates}
        for preferred in ("P-STD", "P-LITE", "P-VIDEO"):
            if preferred in by_id:
                return preferred
        return candidates[0]["plan_id"] if candidates else None

    @staticmethod
    def _alternative(
        candidates: list[dict[str, Any]],
        estimates: dict[str, dict[str, Any]],
        current: str | None,
    ) -> dict[str, Any] | None:
        options = [
            item
            for item in sorted(candidates, key=lambda value: value["tier_rank"])
            if item["plan_id"] != current and item["plan_id"] not in estimates
        ]
        return options[0] if options else None
