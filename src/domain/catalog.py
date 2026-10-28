from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from src.config import ROOT_DIR

DATA_DIR = ROOT_DIR / "data"
MONEY = Decimal("0.01")


def money(value: Decimal | str | int | float) -> Decimal:
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def money_str(value: Decimal | str | int | float) -> str:
    return format(money(value), "f")


def stable_hash(value: Any, length: int = 12) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


@dataclass(frozen=True)
class PlanTemplate:
    plan_id: str
    name: str
    tier_rank: int
    material_tier: str
    script_level: str
    production_level: str
    prices: dict[str, Decimal]
    production_minutes: int

    @property
    def total(self) -> Decimal:
        return money(sum(self.prices.values(), Decimal("0")))

    def to_public(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "name": self.name,
            "tier_rank": self.tier_rank,
            "material_tier": self.material_tier,
            "script_level": self.script_level,
            "production_level": self.production_level,
            "production_minutes": self.production_minutes,
        }


@dataclass(frozen=True)
class Material:
    material_id: str
    title: str
    type: str
    tier: str
    tags: list[str]
    available: bool
    license: str

    def to_public(self) -> dict[str, Any]:
        return {
            "material_id": self.material_id,
            "title": self.title,
            "type": self.type,
            "tier": self.tier,
            "tags": list(self.tags),
            "available": self.available,
            "license": self.license,
        }


class Catalog:
    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.data_dir = data_dir
        self.materials_raw = self._load("materials.json")
        self.pricing_raw = self._load("pricing.json")
        self.policy_raw = self._load("policy.json")

        self.materials: list[Material] = [
            Material(
                material_id=item["material_id"],
                title=item["title"],
                type=item["type"],
                tier=item["tier"],
                tags=list(item.get("tags", [])),
                available=bool(item.get("available", True)),
                license=item.get("license", self.materials_raw.get("license", "synthetic-demo")),
            )
            for item in self.materials_raw.get("materials", [])
        ]
        self.plans: dict[str, PlanTemplate] = {}
        for plan_id, raw in self.pricing_raw.get("plans", {}).items():
            self.plans[plan_id] = PlanTemplate(
                plan_id=plan_id,
                name=raw["name"],
                tier_rank=int(raw["tier_rank"]),
                material_tier=raw["material_tier"],
                script_level=raw["script_level"],
                production_level=raw["production_level"],
                prices={key: money(value) for key, value in raw["prices"].items()},
                production_minutes=int(raw["production_minutes"]),
            )

        self.materials_version = self.materials_raw.get("version", "materials-v1")
        self.pricing_version = self.pricing_raw.get("version", "pricing-v1")
        self.policy_version = self.policy_raw.get("version", "policy-v1")
        self.required_materials = int(self.policy_raw.get("required_materials_per_plan", 2))
        self.rules_checksum = hashlib.sha256(
            (data_dir / "pricing.json").read_bytes()
        ).hexdigest()

    def _load(self, name: str) -> dict[str, Any]:
        with (self.data_dir / name).open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def available_materials(self) -> list[Material]:
        return [item for item in self.materials if item.available]

    def materials_for_plan(self, plan_id: str) -> list[Material]:
        plan = self.plans[plan_id]
        items = [
            item
            for item in self.available_materials()
            if item.tier == plan.material_tier
        ]
        return items[: self.required_materials]

    def plan_is_available(self, plan_id: str, materials: list[Material] | None = None) -> bool:
        plan = self.plans[plan_id]
        pool = materials if materials is not None else self.available_materials()
        return sum(1 for item in pool if item.tier == plan.material_tier) >= self.required_materials

    def candidate_plans(self, materials: list[Material] | None = None) -> list[dict[str, Any]]:
        pool = materials if materials is not None else self.available_materials()
        result: list[dict[str, Any]] = []
        for plan in sorted(self.plans.values(), key=lambda item: item.tier_rank):
            plan_materials = [item for item in pool if item.tier == plan.material_tier]
            available = len(plan_materials) >= self.required_materials
            result.append(
                {
                    **plan.to_public(),
                    "available": available,
                    "material_ids": [item.material_id for item in plan_materials[: self.required_materials]],
                    "unavailable_reason": None
                    if available
                    else f"需要至少 {self.required_materials} 个 {plan.material_tier} 素材",
                }
            )
        return result

    def material_ids_for(self, plan_id: str) -> list[str]:
        return [item.material_id for item in self.materials_for_plan(plan_id)]

    def get_materials(self, material_ids: list[str]) -> list[Material]:
        by_id = {item.material_id: item for item in self.materials}
        missing = [item for item in material_ids if item not in by_id]
        if missing:
            raise KeyError(f"素材不存在: {', '.join(missing)}")
        return [by_id[item] for item in material_ids]

    def estimate(
        self,
        *,
        plan_id: str,
        material_ids: list[str],
        script_id: str,
        script_quality: str,
        budget: Decimal,
        deadline: datetime,
        now: datetime,
    ) -> dict[str, Any]:
        plan = self.plans[plan_id]
        materials = self.get_materials(material_ids)
        if len(materials) < self.required_materials:
            raise ValueError(f"{plan_id} 至少需要 {self.required_materials} 个素材")
        if any(item.tier != plan.material_tier for item in materials):
            raise ValueError(f"{plan_id} 需要 {plan.material_tier} 等级素材")

        line_items = [
            {"item": f"素材包 {plan.material_tier}", "amount": money_str(plan.prices["materials"])},
            {"item": f"脚本生成 {plan.script_level}", "amount": money_str(plan.prices["script"])},
            {"item": f"制作装配 {plan.production_level}", "amount": money_str(plan.prices["production"])},
        ]
        total = money(sum(plan.prices.values(), Decimal("0")))
        available_minutes = max(0, int((deadline - now).total_seconds() // 60))
        within_budget = total <= money(budget)
        within_deadline = plan.production_minutes <= available_minutes
        estimate_id = "est_" + stable_hash(
            {
                "plan_id": plan_id,
                "material_ids": material_ids,
                "script_id": script_id,
                "pricing_version": self.pricing_version,
            }
        )
        return {
            "estimate_id": estimate_id,
            "plan_id": plan_id,
            "plan_name": plan.name,
            "currency": self.pricing_raw.get("currency", "CNY"),
            "pricing_version": self.pricing_version,
            "rules_checksum": self.rules_checksum,
            "script_id": script_id,
            "script_quality": script_quality,
            "material_ids": material_ids,
            "line_items": line_items,
            "total": money_str(total),
            "budget": money_str(budget),
            "remaining": money_str(money(budget) - total),
            "within_budget": within_budget,
            "required_minutes": plan.production_minutes,
            "available_minutes": available_minutes,
            "within_deadline": within_deadline,
            "evaluated_at": now.isoformat(),
        }

    def infeasible_gap(self, estimates: dict[str, dict[str, Any]], budget: Decimal) -> dict[str, Any]:
        values = list(estimates.values())
        min_cost = min((money(item["total"]) for item in values), default=None)
        shortfall = max(Decimal("0"), min_cost - money(budget)) if min_cost is not None else None
        time_values = [int(item["required_minutes"]) for item in values]
        return {
            "budget": money_str(budget),
            "minimum_feasible_cost": money_str(min_cost) if min_cost is not None else None,
            "budget_shortfall": money_str(shortfall) if shortfall is not None else None,
            "minimum_required_minutes": min(time_values) if time_values else None,
            "evaluated_plans": [
                {
                    "plan_id": item["plan_id"],
                    "total": item["total"],
                    "required_minutes": item["required_minutes"],
                    "within_budget": item["within_budget"],
                    "within_deadline": item["within_deadline"],
                }
                for item in values
            ],
        }


catalog = Catalog()
