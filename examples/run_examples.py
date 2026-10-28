from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

EXAMPLES_DIR = Path(__file__).resolve().parent
INPUTS_DIR = EXAMPLES_DIR / "inputs"
OUTPUTS_DIR = EXAMPLES_DIR / "outputs"
SCENARIOS_PATH = EXAMPLES_DIR / "scenarios.yaml"
RESULTS_PATH = EXAMPLES_DIR / "results.json"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def post_run(
    client: httpx.Client,
    request: dict[str, Any],
    idempotency_key: str | None,
) -> dict[str, Any]:
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    response = client.post("/v1/runs", json=request, headers=headers)
    if response.status_code != 200:
        raise RuntimeError(f"运行失败 HTTP {response.status_code}: {response.text}")
    return response.json()


def summarize(name: str, report: dict[str, Any], output_file: str) -> dict[str, Any]:
    plan = report.get("plan") or {}
    cost = report.get("cost") or {}
    incomplete = report.get("incomplete") or {}
    saved = report.get("saved_plan") or {}
    return {
        "name": name,
        "output": output_file,
        "input": f"examples/inputs/{name}.json",
        "run_id": report.get("run_id"),
        "status": report.get("status"),
        "selected_plan_id": plan.get("selected_plan_id"),
        "cost_total": cost.get("total"),
        "within_budget": cost.get("within_budget"),
        "saved_plan_id": saved.get("plan_id"),
        "already_exists": saved.get("already_exists"),
        "incomplete_code": incomplete.get("code"),
        "missing_fields": incomplete.get("missing_fields", []),
        "reused_steps": report.get("reused_steps", []),
        "trace": (report.get("trace") or {}).get("path"),
        "deduplicated": report.get("deduplicated"),
        "resume_noop": report.get("resume_noop"),
    }


def run_scenario(client: httpx.Client, scenario: dict[str, Any]) -> list[dict[str, Any]]:
    base_name = scenario["name"]
    request = scenario["request"]
    key = scenario.get("idempotency_key")
    written: list[dict[str, Any]] = []

    if scenario.get("repeat"):
        first_name = scenario.get("first_name", f"{base_name}-first")
        second_name = scenario.get("second_name", f"{base_name}-second")
        input_file = INPUTS_DIR / f"{first_name}.json"
        write_json(input_file, request)
        first = post_run(client, request, key)
        write_json(OUTPUTS_DIR / f"{first_name}.json", first)
        written.append(summarize(first_name, first, f"examples/outputs/{first_name}.json"))

        second = post_run(client, request, key)
        write_json(OUTPUTS_DIR / f"{second_name}.json", second)
        written.append(summarize(second_name, second, f"examples/outputs/{second_name}.json"))
        return written

    if scenario.get("resume"):
        parent_name = scenario.get("parent_name", f"{base_name}-parent")
        child_name = scenario.get("child_name", f"{base_name}-child")
        write_json(INPUTS_DIR / f"{parent_name}.json", request)
        parent = post_run(client, request, key)
        write_json(OUTPUTS_DIR / f"{parent_name}.json", parent)
        written.append(summarize(parent_name, parent, f"examples/outputs/{parent_name}.json"))

        resume_body: dict[str, Any] = {}
        write_json(INPUTS_DIR / f"{child_name}.json", resume_body)
        response = client.post(f"/v1/runs/{parent['run_id']}/resume", json=resume_body)
        if response.status_code != 200:
            raise RuntimeError(f"续跑失败 HTTP {response.status_code}: {response.text}")
        child = response.json()
        write_json(OUTPUTS_DIR / f"{child_name}.json", child)
        written.append(summarize(child_name, child, f"examples/outputs/{child_name}.json"))
        return written

    write_json(INPUTS_DIR / f"{base_name}.json", request)
    report = post_run(client, request, key)
    write_json(OUTPUTS_DIR / f"{base_name}.json", report)
    written.append(summarize(base_name, report, f"examples/outputs/{base_name}.json"))
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="通过 HTTP API 生成 examples 实际输出与 Trace")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    with SCENARIOS_PATH.open("r", encoding="utf-8") as handle:
        manifest = yaml.safe_load(handle) or {}
    scenarios = manifest.get("scenarios", [])
    if not scenarios:
        print("scenarios.yaml 中没有场景", file=sys.stderr)
        return 1

    results: list[dict[str, Any]] = []
    with httpx.Client(base_url=args.base_url, timeout=args.timeout) as client:
        health = client.get("/healthz")
        health.raise_for_status()
        print("health:", json.dumps(health.json(), ensure_ascii=False))
        for scenario in scenarios:
            print(f"running {scenario['name']} ...")
            results.extend(run_scenario(client, scenario))

    write_json(
        RESULTS_PATH,
        {
            "generated_at": datetime.now().astimezone().isoformat(),
            "base_url": args.base_url,
            "health": health.json(),
            "scenarios": results,
        },
    )
    print(f"wrote {len(results)} reports to {OUTPUTS_DIR}")
    print(f"summary: {RESULTS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
