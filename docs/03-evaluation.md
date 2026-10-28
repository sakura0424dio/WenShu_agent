# 03 · 测试预期、实际结果与限制

## 1. 验证环境

| 项 | 值 |
|---|---|
| 日期 | 2026-10-28 |
| 操作系统 | Windows 11 |
| Python | 3.10.11 |
| LangGraph | 1.2.18 |
| FastAPI | 0.162.0 |
| 模型模式 | Mock（默认回归）；DeepSeek `deepseek-flash`（真实调用验证） |
| 固定时钟 | `2026-11-01T09:00:00+08:00` |
| 示例截止时间 | `2026-11-05T18:00:00+08:00`，可用 6300 分钟 |
| 数据 | `materials-v1`、`pricing-v1`、`policy-v1`，全部合成 |

## 2. 测试预期

### 2.1 测试范围

| 文件 | 覆盖内容 |
|---|---|
| `tests/test_acceptance.py` | 输入、成本、预算改选、工具失败、部分成功、幂等、无解、续跑、上限、Trace |
| `tests/test_api.py` | 健康检查、HTTP 200 业务未完成、运行查询、Trace、方案查询、幂等冲突、404、完成运行续跑 no-op |
| `tests/conftest.py` | 每个用例独立临时 SQLite、临时 plans/traces，不污染 `examples/` |

### 2.2 预期行为

| 场景 | 预期 |
|---|---|
| 正常完成 | `completed`，P-STD，费用 12.00，方案保存成功 |
| 缺预算/截止时间 | `incomplete[MISSING_REQUIRED_INPUT]`，`steps=[]`，不调用模型 |
| 非法预算/时间 | `incomplete[INVALID_INPUT]`，给出 `invalid_fields` |
| 预算超限 | 观察到 12.00 > 10.00 后改选 P-LITE，最终 8.00 保存 |
| 工具失败 | 失败 Step 保留 `failed`；可重试/换方案/停止；终态如实标注 |
| 部分成功 | Task 为 `partial`，保留 caveat，终态 `completed_with_caveats` |
| 无可行方案 | `incomplete[NO_FEASIBLE_PLAN]`，最低可行成本 8.00，差额 3.00 |
| 重复执行 | 相同幂等键返回同一 `run_id`，`deduplicated=true` |
| 续跑 | 子运行复用成功步骤，失败步骤保留，新步骤 ID 继续 |
| 工具调用上限 | `incomplete[TOOL_CALL_LIMIT_REACHED]`，保留成功步骤并列出剩余任务 |
| Trace | 每一步有参数、返回摘要、后续动作与决策依据；继承步骤来源可区分 |

### 2.3 执行命令

```bash
pytest -q
pytest --junitxml=examples/test-results.xml
```

## 3. 实际测试结果

```text
..................                                                       [100%]
18 passed in 1.66s
```

可核对：

- `examples/test-results.txt`：上面这行实际输出；
- `examples/test-results.xml`：JUnit 格式，18 个用例、0 failure、0 error。

## 4. Mock 示例实际结果

Mock 示例由 `examples/run_examples.py` 通过运行中的 HTTP 服务实际生成：

```bash
python -m uvicorn src.api.app:app --host 127.0.0.1 --port 8126
python examples/run_examples.py --base-url http://127.0.0.1:8126
```

索引见 `examples/results.json`。实际结果摘要：

| 示例 | 状态 | 选中方案 | 合计 | 关键证据 |
|---|---|---|---:|---|
| `01-normal` | `completed` | P-STD | 12.00 | 4 个工具步骤全部 `success`；4 个活动任务全部 `succeeded` |
| `02-missing-input` | `incomplete` | — | — | `MISSING_REQUIRED_INPUT`，`missing_fields=["budget"]`，`steps=[]` |
| `03-tool-failure` | `completed_with_caveats` | P-LITE | 8.00 | `S002 generate_script` 保持 `failed`；改选 P-LITE 后保存 |
| `04-budget-overrun` | `completed` | P-LITE | 8.00 | P-STD 估算 12.00 超预算；P-LITE 估算 8.00 保存 |
| `05-no-feasible` | `incomplete` | P-VIDEO | 15.00 | `NO_FEASIBLE_PLAN`；最低可行成本 8.00，差额 3.00 |
| `06-partial-result` | `completed_with_caveats` | P-LITE | 8.00 | 检索 Step/Task 为 `partial`；caveat 保留 |
| `07-resume-parent` | `incomplete` | P-LITE | 8.00 | `SAVE_FAILED`；S006/S007 失败；S001–S005 成功保留 |
| `07-resume-child` | `completed` | P-LITE | 8.00 | `attempt=2`；`reused_steps=S001–S005`；仅新增 1 次保存 |
| `08-repeat-first` | `completed` | P-LITE | 8.00 | 首次运行 |
| `08-repeat-second` | `completed` | P-LITE | 8.00 | 同 `run_id`，`deduplicated=true` |
| `09-tool-call-limit` | `incomplete` | P-STD | 12.00 | `TOOL_CALL_LIMIT_REACHED`，`limit=3`，剩余任务列出 |

### 4.1 A-06 参考场景 10/12/8

`examples/outputs/04-budget-overrun.json`：

- `tool_results.estimates["P-STD"].total = "12.00"`，`within_budget=false`；
- 决策 `COST_OVER_BUDGET_REPLAN`，动作 `generate_script(plan_id="P-LITE")`；
- `tool_results.estimates["P-LITE"].total = "8.00"`，`within_budget=true`；
- 最终 `cost.total="8.00"`，`budget="10.00"`，`remaining="2.00"`；
- `saved_plan.plan_type="P-LITE"`。

费用可手算：`2.00 + 2.00 + 4.00 = 8.00`；P-STD：`5.00 + 3.00 + 4.00 = 12.00`。

### 4.2 A-03 工具失败

`examples/outputs/03-tool-failure.json`：

- `S002`：`generate_script(P-STD)`，`status=failed`，`injected=true`，错误 `TOOL_TIMEOUT`；
- `S003`：改选 `generate_script(P-LITE)`，成功；
- `plan.tasks` 中活动分支为 P-LITE 且全部成功；
- `tool_results.superseded_tasks` 中 `script:P-STD` 仍为 `failed`；
- 终态 `completed_with_caveats`，caveat 明确记录失败步骤。

没有把 S002 改成成功，也没有把 `save_plan` 之前的失败从 `steps` 删除。

### 4.3 A-02 缺输入

`examples/outputs/02-missing-input.json`：

- `status=incomplete`；
- `incomplete.code=MISSING_REQUIRED_INPUT`；
- `missing_fields=["budget"]`；
- `steps=[]`、`tool_results.estimates={}`、`saved_plan=null`；
- 没有默认预算、没有调用模型/工具。

非法截止时间、负预算、非数字预算也在 `tests/test_acceptance.py` 中验证为 `INVALID_INPUT`。

### 4.4 A-07 无可行方案

`examples/outputs/05-no-feasible.json`：

```json
{
  "code": "NO_FEASIBLE_PLAN",
  "gap": {
    "minimum_feasible_cost": "8.00",
    "budget_shortfall": "3.00",
    "minimum_required_minutes": 120,
    "evaluated_plans": [
      {"plan_id": "P-STD", "total": "12.00", "within_budget": false},
      {"plan_id": "P-LITE", "total": "8.00", "within_budget": false},
      {"plan_id": "P-VIDEO", "total": "15.00", "within_budget": false}
    ]
  }
}
```

`failed_steps=[]`，因为本轮没有工具失败；所有估算工具调用成功，但方案不可行。终态仍未完成，不把“工具成功”等同“方案成功”。

### 4.5 A-08 续跑

父运行 `examples/outputs/07-resume-parent.json`：

- `status=incomplete`，`code=SAVE_FAILED`；
- S001–S005 成功；S006、S007 两次 `save_plan` 均失败；
- `saved_plan=null`；成功结果未丢失。

子运行 `examples/outputs/07-resume-child.json`：

- `parent_run_id` 指向父运行，`attempt=2`；
- `reused_steps=["S001","S002","S003","S004","S005"]`；
- 继承步骤标 `inherited_from_parent=true`，成功复用的另标 `from_parent_run=true`；
- 仅新增 S008 一次成功的 `save_plan`，没有重跑脚本/估算；
- 终态 `completed`，保存方案 P-LITE，合计 8.00。

Trace `examples/traces/run_7a4bf71ef51f4f08.jsonl` 中包含 `inherited_step`（reused=true，source_run_id=父运行）与新的工具调用，能区分“复用”和“新执行”。继承步骤不会再作为子运行的 `tool_result` 出现，避免把历史步骤误记为本次执行；父运行失败步骤标 `reused=false`，保留历史状态。

### 4.6 A-05 重复执行

- `08-repeat-first` 与 `08-repeat-second` 的 `run_id` 相同；
- 第二次 `deduplicated=true`；
- 两次 `saved_plan.plan_id` 相同；
- 数据库中只有一条幂等键记录；
- 另有无幂等键重跑测试：新 `run_id`、同一内容哈希 `plan_id`、`already_exists=true`。

### 4.7 达到上限

`examples/outputs/09-tool-call-limit.json`：

- `TOOL_CALL_LIMIT_REACHED`；
- `gap.limit=3`、`tool_calls=3`、`steps_used=3`；
- `remaining_tasks=["script:P-LITE","estimate:P-LITE","save:P-LITE"]`；
- S001–S003 成功结果保留；没有伪造 save 成功。

## 5. 真实 DeepSeek 执行记录

在填入本地 API Key 后，使用 `deepseek-flash` 通过 HTTP 服务实际执行了 9 组场景、11 份报告。完整请求、报告、Trace 与索引见 `examples/real-deepseek/`，其中 `results.json` 记录每次 `run_id` 与文件路径。

| 场景 | 真实模型结果 | 与 Mock 的关键一致性 |
|---|---|---|
| `01-normal` | `completed`，P-STD，12.00 | 正常完成并保存 |
| `02-missing-input` | `incomplete[MISSING_REQUIRED_INPUT]`，`steps=[]` | 缺预算不调用模型 |
| `03-tool-failure` | `completed_with_caveats`，S002 保持 `failed` | 失败保留，重试后仍按预算改选 P-LITE |
| `04-budget-overrun` | `completed`，P-STD 12.00 → P-LITE 8.00 | 参考场景 10/12/8 |
| `05-no-feasible` | `incomplete[NO_FEASIBLE_PLAN]`，最低 8.00，差额 3.00 | 无解时给出缺口 |
| `06-partial-result` | `completed_with_caveats`，检索部分成功 | 保留 partial 与 caveat |
| `07-resume-parent/child` | 父 `SAVE_FAILED`；子 `completed`，复用 S001–S005 | 失败步骤不洗白，成功步骤不重跑 |
| `08-repeat-first/second` | 同一 `run_id`，第二次 `deduplicated=true` | 幂等复用 |
| `09-tool-call-limit` | `incomplete[TOOL_CALL_LIMIT_REACHED]` | 保留已成功步骤并列出剩余任务 |

真实模型在场景 04 中先评估 P-STD 得到 12.00，再按其观察改选 P-LITE 8.00，Trace 中包含 `COST_OVER_BUDGET_REPLAN` 决策。续跑场景中，子运行从父运行继承 5 个成功步骤，父运行的两次保存失败保留为历史，新尝试只新增一次成功保存。

真实目录的 Trace 保留当时运行时的原始事件，未事后改写；后续动作由紧随 `tool_result` 的 `model_action` 表达。`examples/traces/` 中重新生成的 Mock Trace 另含显式 `next_action` 字段。

## 6. 验收点映射

| 编号 | 结果 | 证据 |
|---|---|---|
| A-01 | 通过 | `examples/outputs/01-normal.json`、`examples/real-deepseek/outputs/01-normal.json`；`test_a01_normal_completion` |
| A-02 | 通过 | `examples/outputs/02-missing-input.json`、`examples/real-deepseek/outputs/02-missing-input.json`；`test_a02_*` |
| A-03 | 通过 | `examples/outputs/03-tool-failure.json`、`06-partial-result.json`；`examples/real-deepseek/outputs/03-tool-failure.json`、`06-partial-result.json`；`test_a03_*`、`test_partial_search_*` |
| A-04 | 通过 | `examples/outputs/04-budget-overrun.json`、`examples/real-deepseek/outputs/04-budget-overrun.json`；`test_a04_a06_*` |
| A-05 | 通过 | `examples/outputs/08-repeat-*.json`、`examples/real-deepseek/outputs/08-repeat-*.json`；`test_a05_*`、`test_idempotency_and_not_found` |
| A-06 | 通过 | 同 A-04，Mock 与真实模型均为 12 → 8；`test_a04_a06_*` |
| A-07 | 通过 | `examples/outputs/05-no-feasible.json`、`examples/real-deepseek/outputs/05-no-feasible.json`；`test_a07_*` |
| A-08 | 通过 | `examples/outputs/07-resume-*.json`、`examples/real-deepseek/outputs/07-resume-*.json`；`test_a08_*` |
| 上限 | 通过 | `examples/outputs/09-tool-call-limit.json`、`examples/real-deepseek/outputs/09-tool-call-limit.json`；`test_tool_call_limit_*` |

## 7. 失败、偏差与限制

### 7.1 真实 DeepSeek 验证范围

已使用本地 API Key 和 `deepseek-flash` 完成真实 HTTP 冒烟：

- 9 组场景全部按预期结束，覆盖 A-01 至 A-08 与工具调用上限；
- 单次运行通常为 5–17 秒，未触发 120 秒运行超时；
- JSON 输出可解析，未出现 `MODEL_OUTPUT_INVALID`；
- 真实模型在预算 10 的场景中先评估 P-STD 12.00，再改选 P-LITE 8.00；
- 真实续跑中，模型正确区分“父运行历史失败”和“本次新尝试”，重新保存成功。

仍需说明：

- 真实模型输出可能随模型版本、提示词和网络变化；`examples/real-deepseek/` 是特定时间点的实际记录，不是确定性回归测试；
- 默认 `pytest` 与 `examples/run_examples.py` 仍使用确定性 Mock，保证无密钥环境可复现；
- 未做并发、长时间压测或真实素材/生产部署验证；
- “真实模型不可用”路径也已验证：Key 为空时服务启动成功，运行返回 `incomplete[MODEL_UNAVAILABLE]`，不抛 500、不伪造完成。

### 7.2 与设计文档的偏差

- 设计中的 `tool_cache`、独立 `steps`/`plans` 数据表未实现。当前步骤嵌在 `runs.state_json`，方案写入 `storage/plans/*.json`。
- 检查点用于审计；A-08 续跑使用应用层状态快照创建新 LangGraph 线程，而不是从检查点原地恢复。
- 这两点不影响 A-01～A-08 的验收，但会限制大规模运行与跨进程恢复能力。

### 7.3 其他限制

- 单进程同步执行；并发请求同一幂等键会返回 `RUN_IN_PROGRESS`，但不提供分布式锁。
- 工具延迟指标在本机通常为 0 ms，不能作为性能基准。
- 固定时钟使示例可复现；若当前日期晚于示例截止时间，需要设置 `APP_FIXED_NOW` 或使用未来截止时间。
- 没有 UI、权限、速率限制、审计日志轮转。
- 真实模型提示词可能受模型版本影响；`decision_basis` 只记录简短依据，不采集思维链。

## 8. 复现建议

1. 清理 `storage/` 与 `examples/traces/`（不要删除 `examples/scenarios.yaml` 和脚本）；
2. 确认 `config.yaml` 中 `demo.fixed_now=2026-11-01T09:00:00+08:00`；
3. 启动服务并运行 `examples/run_examples.py`；
4. 运行 `pytest -q`；
5. 对比 `examples/results.json` 与本文第 4 节；
6. 如需复核真实模型，配置 DeepSeek 后参考 `examples/real-deepseek/README.md` 与 `inputs/`。

若重新生成示例，`run_id`、`saved_plan_id` 和时间戳会变化，这是运行标识而非语义差异；状态、费用与步骤关系应保持一致。
