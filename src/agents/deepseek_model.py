from __future__ import annotations

import json
import re
from typing import Any

from src.agents.base import AgentContext, ModelOutputError, ModelUnavailableError
from src.config import AppConfig
from src.domain.models import AgentAction

SYSTEM_PROMPT = """你是内容活动规划 Agent，服务运营人员。
你必须在预算与截止时间约束内，通过工具完成内容活动方案。

可用工具：
1. search_materials(topic, keywords, limit)
2. generate_script(plan_id, material_ids, tone, duration_seconds)
3. estimate_cost(plan_id, material_ids, script_id)
4. save_plan(plan_id, estimate_id, script_id, material_ids)

规则：
- 每轮只输出一个动作，严格输出 JSON 对象，不要输出 Markdown 代码块或思维过程。
- 费用只能使用 estimate_cost 的返回，不能自己编造。
- 方案优先级：先评估 P-STD（标准方案）；P-STD 超预算或超截止时间时，再尝试 P-LITE，最后 P-VIDEO。不要为了避免超预算而跳过 P-STD。
- 估算超预算或超截止时间时，先尝试替代方案；没有可行方案时输出 stop。
- 工具 failed 时查看 error.code / retryable，可修正参数、换方案、明确重试一次或 stop；同一工具同一参数最多重试一次，save_plan 连续失败两次后必须 stop(SAVE_FAILED)，不要反复调用。
- 工具 partial 时可基于可用部分继续，但必须在 decision_basis 中说明缺口。
- 只有 save_plan 成功后才能输出 finish；无法完成时输出 stop。
- 若 resume_context 表明这是续跑的新尝试，父运行失败步骤只是历史，不代表本次仍会失败；本次未注入失败时应重新调用失败的工具。
- 不能把失败或部分成功说成成功；不要猜测缺失输入。
- 输出字段：
  action: "call_tool" | "finish" | "stop"
  tool_name: 工具名或 null
  arguments: 对象
  reason_code: 简短英文代码，如 INITIAL_DISCOVERY / COST_OVER_BUDGET_REPLAN / TOOL_ERROR_REPLAN / NO_FEASIBLE_PLAN / SAVE_DELIVERABLE
  decision_basis: 不超过 200 字的中文简洁依据
  stop_reason: stop 时的原因代码，否则 null
"""


class DeepSeekModel:
    mode = "deepseek"

    def __init__(self, config: AppConfig) -> None:
        if not config.model.api_key.strip():
            raise ModelUnavailableError(
                "DeepSeek API Key 未配置：请在 config.yaml 的 model.api_key 填写，"
                "或设置 DEEPSEEK_API_KEY；也可以使用 MODEL_MODE=mock。"
            )
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确错误
            raise ModelUnavailableError(f"langchain-openai 未安装: {exc}") from exc
        self.config = config
        self.llm = ChatOpenAI(
            model=config.model.name,
            api_key=config.model.api_key,
            base_url=config.model.base_url,
            temperature=config.model.temperature,
            timeout=config.model.timeout_seconds,
            max_tokens=config.model.max_tokens,
        )

    def next_action(self, context: AgentContext) -> AgentAction:
        messages = [
            ("system", SYSTEM_PROMPT),
            ("user", self._context_prompt(context.state)),
        ]
        try:
            response = self._invoke(messages)
            return self._parse(str(response.content))
        except (ModelUnavailableError, ModelOutputError):
            raise
        except Exception as exc:  # noqa: BLE001 - 网络/鉴权/服务错误统一暴露
            raise ModelUnavailableError(f"DeepSeek 调用失败: {exc}") from exc

    def _invoke(self, messages: list[tuple[str, str]]) -> Any:
        try:
            bound = self.llm.bind(response_format={"type": "json_object"})
        except Exception:  # noqa: BLE001
            bound = self.llm
        try:
            return bound.invoke(messages)
        except Exception as first_error:  # noqa: BLE001
            if bound is self.llm:
                raise ModelUnavailableError(f"DeepSeek 请求失败: {first_error}") from first_error
            # 某些 OpenAI 兼容端点不支持 response_format；降级为普通调用再试一次。
            try:
                return self.llm.invoke(messages)
            except Exception as exc:  # noqa: BLE001
                raise ModelUnavailableError(
                    f"DeepSeek 请求失败（JSON 模式与普通模式均失败）: {exc}"
                ) from exc

    @staticmethod
    def _parse(content: str) -> AgentAction:
        text = content.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                raise ModelOutputError("模型输出不是合法 JSON")
            try:
                payload = json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                raise ModelOutputError(f"模型输出 JSON 解析失败: {exc}") from exc
        try:
            action = AgentAction.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            raise ModelOutputError(f"模型动作不符合 schema: {exc}") from exc
        if action.action == "call_tool" and not action.tool_name:
            raise ModelOutputError("call_tool 缺少 tool_name")
        return action

    @staticmethod
    def _context_prompt(state: dict[str, Any]) -> str:
        scripts = state.get("scripts") or {}
        estimates = state.get("estimates") or {}
        slim = {
            "run_id": state.get("run_id"),
            "attempt": state.get("attempt"),
            "normalized_input": state.get("normalized"),
            "candidates": state.get("candidates", []),
            "selected_plan_id": state.get("selected_plan_id"),
            "materials": state.get("materials", []),
            "scripts": {
                plan_id: {
                    "script_id": item.get("script_id"),
                    "quality": item.get("quality"),
                    "material_ids": item.get("material_ids"),
                }
                for plan_id, item in scripts.items()
            },
            "estimates": estimates,
            "tasks": state.get("tasks", []),
            "last_observation": state.get("last_observation"),
            "recent_steps": [
                {
                    "step_id": item.get("step_id"),
                    "tool": item.get("tool"),
                    "status": item.get("status"),
                    "result_summary": item.get("result_summary"),
                    "from_parent_run": item.get("from_parent_run", False),
                    "source_run_id": item.get("source_run_id"),
                }
                for item in (state.get("steps") or [])[-5:]
            ],
            "resume_context": state.get("resume_context"),
            "saved_plan": state.get("saved_plan"),
            "limits": {
                "steps_used": state.get("steps_used"),
                "max_steps": state.get("max_steps"),
                "tool_calls": state.get("tool_calls"),
                "max_tool_calls": state.get("max_tool_calls"),
            },
        }
        return (
            "当前运行状态（JSON）：\n"
            + json.dumps(slim, ensure_ascii=False, indent=2)
            + "\n请只输出一个 JSON 动作。"
        )
