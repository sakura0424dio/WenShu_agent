from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from src.config import ROOT_DIR
from src.domain.catalog import Catalog, money_str, stable_hash
from src.domain.clock import Clock
from src.domain.models import ToolError, ToolMetrics, ToolResult


class ToolExecutionError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


@dataclass
class ToolRuntime:
    catalog: Catalog
    state: dict[str, Any]
    clock: Clock
    plans_dir: Path


def _ok(data: dict[str, Any], started: float, warnings: list[str] | None = None) -> ToolResult:
    return ToolResult(
        status="partial" if warnings else "success",
        data=data,
        warnings=warnings or [],
        metrics=ToolMetrics(latency_ms=max(0, int((time.perf_counter() - started) * 1000))),
    )


def _fail(error: ToolExecutionError, started: float) -> ToolResult:
    return ToolResult(
        status="failed",
        error=ToolError(
            code=error.code,
            message=error.message,
            retryable=error.retryable,
            details=error.details,
        ),
        metrics=ToolMetrics(latency_ms=max(0, int((time.perf_counter() - started) * 1000))),
    )


def search_materials(args: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    started = time.perf_counter()
    try:
        topic = str(args.get("topic") or runtime.state.get("normalized", {}).get("topic", "")).strip()
        keywords = [str(item).strip() for item in args.get("keywords", []) if str(item).strip()]
        limit = max(1, min(int(args.get("limit", 6)), 12))
        if not topic:
            raise ToolExecutionError("INVALID_ARGUMENT", "search_materials 需要 topic")

        available = runtime.catalog.available_materials()
        tokens = [topic, *keywords]
        matched = [
            item
            for item in available
            if any(token in item.title or any(tag in token or token in tag for tag in item.tags) for token in tokens)
        ]
        warnings: list[str] = []
        if not matched:
            matched = available
            warnings.append("NO_EXACT_MATCH")
        materials = matched[:limit]
        candidates = runtime.catalog.candidate_plans(materials)
        available_plan_ids = [item["plan_id"] for item in candidates if item["available"]]
        return _ok(
            {
                "kind": "materials",
                "materials": [item.to_public() for item in materials],
                "candidate_plans": candidates,
                "candidate_plan_ids": available_plan_ids,
                "total_found": len(materials),
                "data_version": runtime.catalog.materials_version,
                "license": runtime.catalog.materials_raw.get("license", "synthetic-demo"),
            },
            started,
            warnings,
        )
    except ToolExecutionError as exc:
        return _fail(exc, started)
    except Exception as exc:  # noqa: BLE001 - 工具错误必须转为观察，不能冒充成功
        return _fail(ToolExecutionError("TOOL_INTERNAL_ERROR", str(exc)), started)


def generate_script(args: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    started = time.perf_counter()
    try:
        plan_id = str(args.get("plan_id") or "")
        if plan_id not in runtime.catalog.plans:
            raise ToolExecutionError("NOT_FOUND", f"方案不存在: {plan_id}")
        plan = runtime.catalog.plans[plan_id]
        material_ids = [str(item) for item in args.get("material_ids", [])]
        if not material_ids:
            material_ids = runtime.catalog.material_ids_for(plan_id)
        try:
            materials = runtime.catalog.get_materials(material_ids)
        except KeyError as exc:
            raise ToolExecutionError("NOT_FOUND", str(exc)) from exc
        if not runtime.catalog.plan_is_available(plan_id, materials):
            raise ToolExecutionError(
                "PRECONDITION_FAILED",
                f"素材不足以支撑 {plan_id}",
                details={"plan_id": plan_id, "material_ids": material_ids},
            )

        topic = str(runtime.state.get("normalized", {}).get("topic", "内容活动"))
        tone = str(args.get("tone") or "轻松种草")
        duration = int(args.get("duration_seconds") or 45)
        section_map = {
            "P-STD": ["开场钩子", "场景痛点", "装备清单", "行动号召"],
            "P-LITE": ["开场钩子", "轻量清单", "行动号召"],
            "P-VIDEO": ["开场镜头", "装备展示", "收尾行动"],
        }
        sections = [
            {
                "name": name,
                "text": f"{topic}｜{name}：以「{tone}」口吻，结合{plan.name}的素材，"
                f"控制在 {max(8, duration // len(section_map[plan_id]))} 秒内。",
            }
            for name in section_map[plan_id]
        ]
        script_id = "script_" + stable_hash(
            {
                "plan_id": plan_id,
                "topic": topic,
                "material_ids": sorted(material_ids),
                "tone": tone,
                "duration": duration,
            }
        )
        data = {
            "kind": "script",
            "script_id": script_id,
            "plan_id": plan_id,
            "plan_name": plan.name,
            "title": f"{topic} · {plan.name}",
            "tone": tone,
            "duration_seconds": duration,
            "material_ids": material_ids,
            "sections": sections,
            "word_count": sum(len(item["text"]) for item in sections),
            "quality": "full",
        }
        return _ok(data, started)
    except ToolExecutionError as exc:
        return _fail(exc, started)
    except Exception as exc:  # noqa: BLE001
        return _fail(ToolExecutionError("TOOL_INTERNAL_ERROR", str(exc)), started)


def estimate_cost(args: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    started = time.perf_counter()
    try:
        plan_id = str(args.get("plan_id") or "")
        script_id = str(args.get("script_id") or "")
        if plan_id not in runtime.catalog.plans:
            raise ToolExecutionError("NOT_FOUND", f"方案不存在: {plan_id}")
        script = runtime.state.get("scripts", {}).get(plan_id)
        if not script or script.get("script_id") != script_id:
            raise ToolExecutionError("NOT_FOUND", f"脚本不存在或不属于 {plan_id}: {script_id}")
        material_ids = [str(item) for item in args.get("material_ids") or script.get("material_ids", [])]
        normalized = runtime.state.get("normalized") or {}
        if not normalized.get("budget") or not normalized.get("deadline"):
            raise ToolExecutionError("PRECONDITION_FAILED", "输入未完成归一化")
        data = runtime.catalog.estimate(
            plan_id=plan_id,
            material_ids=material_ids,
            script_id=script_id,
            script_quality=str(script.get("quality", "full")),
            budget=Decimal(str(normalized["budget"])),
            deadline=datetime.fromisoformat(normalized["deadline"]),
            now=runtime.clock.now(),
        )
        return _ok(data, started)
    except ToolExecutionError as exc:
        return _fail(exc, started)
    except Exception as exc:  # noqa: BLE001
        return _fail(ToolExecutionError("TOOL_INTERNAL_ERROR", str(exc)), started)


def save_plan(args: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    started = time.perf_counter()
    try:
        plan_id = str(args.get("plan_id") or "")
        estimate_id = str(args.get("estimate_id") or "")
        script_id = str(args.get("script_id") or "")
        normalized = runtime.state.get("normalized") or {}
        estimate = runtime.state.get("estimates", {}).get(plan_id)
        script = runtime.state.get("scripts", {}).get(plan_id)
        if not estimate or estimate.get("estimate_id") != estimate_id:
            raise ToolExecutionError("PRECONDITION_FAILED", "估算不存在或不属于该方案")
        if not script or script.get("script_id") != script_id:
            raise ToolExecutionError("PRECONDITION_FAILED", "脚本不存在或不属于该方案")
        if not estimate.get("within_budget"):
            raise ToolExecutionError(
                "PRECONDITION_FAILED",
                "方案超预算，不能保存",
                details={"total": estimate.get("total"), "budget": estimate.get("budget")},
            )
        deadline = datetime.fromisoformat(normalized["deadline"])
        now = runtime.clock.now()
        if (deadline - now).total_seconds() < int(estimate["required_minutes"]) * 60:
            raise ToolExecutionError(
                "DEADLINE_EXPIRED",
                "剩余时间不足以完成该方案",
                details={
                    "required_minutes": estimate["required_minutes"],
                    "available_minutes": max(0, int((deadline - now).total_seconds() // 60)),
                },
            )

        stable_payload = {
            "schema_version": "plan-v1",
            "topic": normalized["topic"],
            "budget": money_str(Decimal(str(normalized["budget"]))),
            "deadline": normalized["deadline"],
            "plan_id": plan_id,
            "plan_name": estimate["plan_name"],
            "material_ids": sorted(estimate["material_ids"]),
            "script": {
                "title": script["title"],
                "sections": script["sections"],
                "word_count": script["word_count"],
                "quality": script["quality"],
                "tone": script["tone"],
                "duration_seconds": script["duration_seconds"],
            },
            "estimate": {
                "currency": estimate["currency"],
                "pricing_version": estimate["pricing_version"],
                "rules_checksum": estimate["rules_checksum"],
                "line_items": estimate["line_items"],
                "total": estimate["total"],
                "required_minutes": estimate["required_minutes"],
            },
        }
        content_hash = "sha256:" + stable_hash(stable_payload, length=64)
        persisted_plan_id = "plan_" + content_hash.split(":", 1)[1][:12]
        path = runtime.plans_dir / f"{persisted_plan_id}.json"
        already_exists = path.exists()
        if not already_exists:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "plan_id": persisted_plan_id,
                "content_hash": content_hash,
                "content": stable_payload,
                "metadata": {
                    "run_id": runtime.state.get("run_id"),
                    "saved_at": now.isoformat(),
                    "model_mode": runtime.state.get("model_mode"),
                },
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        try:
            display_path = str(path.relative_to(ROOT_DIR)).replace("\\", "/")
        except ValueError:
            display_path = str(path)
        return _ok(
            {
                "kind": "saved_plan",
                "plan_id": persisted_plan_id,
                "content_hash": content_hash,
                "path": display_path,
                "already_exists": already_exists,
                "saved_at": now.isoformat(),
                "estimate_id": estimate_id,
                "script_id": script_id,
                "plan_type": plan_id,
                "total": estimate["total"],
                "quality": script["quality"],
            },
            started,
        )
    except ToolExecutionError as exc:
        return _fail(exc, started)
    except Exception as exc:  # noqa: BLE001
        return _fail(ToolExecutionError("SAVE_ERROR", str(exc)), started)


def _partial_search(data: dict[str, Any], runtime: ToolRuntime) -> tuple[dict[str, Any], list[str]]:
    materials = [item for item in data.get("materials", []) if item.get("tier") == "basic"]
    from src.domain.catalog import Material

    pool = [
        Material(
            material_id=item["material_id"],
            title=item["title"],
            type=item["type"],
            tier=item["tier"],
            tags=list(item.get("tags", [])),
            available=bool(item.get("available", True)),
            license=item.get("license", "synthetic-demo"),
        )
        for item in materials
    ]
    candidates = runtime.catalog.candidate_plans(pool)
    return (
        {
            **data,
            "materials": materials,
            "candidate_plans": candidates,
            "candidate_plan_ids": [item["plan_id"] for item in candidates if item["available"]],
            "total_found": len(materials),
        },
        ["PARTIAL_RESULTS"],
    )


def _partial_script(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    sections = list(data.get("sections", []))
    if sections:
        sections = sections[:-1]
    return (
        {
            **data,
            "sections": sections,
            "word_count": sum(len(item.get("text", "")) for item in sections),
            "quality": "partial",
        },
        ["MISSING_CTA"],
    )


def apply_partial(tool_name: str, data: dict[str, Any], runtime: ToolRuntime) -> tuple[dict[str, Any], list[str]]:
    if tool_name == "search_materials":
        return _partial_search(data, runtime)
    if tool_name == "generate_script":
        return _partial_script(data)
    return data, [f"PARTIAL_{tool_name.upper()}"]


TOOLS: dict[str, Callable[[dict[str, Any], ToolRuntime], ToolResult]] = {
    "search_materials": search_materials,
    "generate_script": generate_script,
    "estimate_cost": estimate_cost,
    "save_plan": save_plan,
}


def run_tool(name: str, args: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    handler = TOOLS.get(name)
    if handler is None:
        return ToolResult(
            status="failed",
            error=ToolError(code="UNKNOWN_TOOL", message=f"未知工具: {name}"),
        )
    return handler(args, runtime)
