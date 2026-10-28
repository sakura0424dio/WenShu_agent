# 01 · 需求分析与成功标准

## 1. 用户

| 项目 | 内容 |
|---|---|
| 用户 | 内容运营人员 |
| 使用场景 | 在有限预算与截止时间内，为某个主题准备一份可执行的内容活动方案 |
| 已有材料 | `data/` 中的合成素材、工具费用与耗时表、方案策略 |
| 使用方式 | 通过 HTTP API 提交主题、预算、截止时间，获得计划、工具结果、费用与保存方案 |

用户不需要理解 Agent 内部状态；但报告必须让用户看懂：做了什么、花了多少、哪些步骤失败、为什么没有完成、还差什么。

## 2. 目标

在预算与截止时间约束内，完成一份**可执行、可复算、已保存**的内容活动方案：

1. 选中方案有成功脚本与成功估算；
2. 费用不超过预算，工期不超过剩余时间；
3. `save_plan` 真正落盘；
4. 报告中的步骤状态与工具真实返回一致；
5. 若有工具失败或部分成功，如实标注，不把失败步骤写成成功；
6. 无法完成时，返回明确缺口，而不是只报错或伪造完成。

“完成”不是“HTTP 200”，而是上述业务条件同时成立。

## 3. 范围

### 3.1 范围内

- 单 Agent、单次内容活动规划；
- 四类本地工具：素材检索、脚本生成、成本估算、方案保存；
- 预算/截止时间约束下的方案选择、改选与停止；
- 缺输入、非法输入、无可行方案、工具失败、部分成功、达到上限等异常终态；
- 运行报告、JSONL Trace、幂等执行、内容寻址保存、失败续跑；
- FastAPI HTTP 接口与 pytest 验收测试；
- Mock 与 DeepSeek 两种模型入口。

### 3.2 范围外

- 前端界面、账号体系、线上部署与多租户；
- 多 Agent 协作；
- 真实素材下载、版权采购、真实广告投放；
- 生产级队列、并发调度、监控告警；
- 对真实模型输出质量做基准评测。

## 4. 输入与约束

### 4.1 必填输入

| 字段 | 规则 |
|---|---|
| `topic` | 去空白后不能为空；最长 200 字 |
| `budget` | 必须为正数，最多两位小数；单位 CNY |
| `deadline` | 合法 ISO 8601，必须带时区，且晚于当前时钟 |

**缺失值绝不猜默认值。** 缺预算或截止时间时，不调用模型或工具，直接返回 `MISSING_REQUIRED_INPUT` 与 `missing_fields`。

非法值（如预算为 0、截止时间无时区）返回 `INVALID_INPUT`，与缺失输入分开报告。

### 4.2 工具约束

| 工具 | 输入 | 输出/约束 |
|---|---|---|
| `search_materials` | `topic`, `keywords`, `limit` | 合成素材、候选方案、可用素材 ID；可能返回 `NO_EXACT_MATCH` 或部分结果 |
| `generate_script` | `plan_id`, `material_ids`, `tone`, `duration_seconds` | 确定性脚本、`script_id`、质量；素材必须属于该方案等级 |
| `estimate_cost` | `plan_id`, `material_ids`, `script_id` | 只按 `pricing.json` 计算；输出明细、合计、预算、余额、所需/可用分钟 |
| `save_plan` | `plan_id`, `estimate_id`, `script_id`, `material_ids` | 复核预算/截止时间/引用；按内容哈希保存，返回 `plan_id`、`content_hash`、`already_exists` |

工具统一返回 `success / partial / failed`；`failed` 必须含错误代码、消息、是否可重试。工具异常不得被吞掉或改写成成功。

### 4.3 执行上限

- 默认 `max_steps=12`、`max_tool_calls=8`；硬上限 20/12。
- 达到步数、工具调用次数或运行超时，必须返回 `incomplete` 与明确 `gap`。
- 达到上限后不得继续调用模型或工具。
- 模型可主动 `stop`，必须记录 `stop_reason`。

## 5. Agent 决策要求

必须形成完整循环：

```text
模型选择动作 → guard 校验 → 执行工具 → 观察结果 → 模型调整下一步或结束
```

至少覆盖三类观察：

| 观察 | 期望调整 |
|---|---|
| 成本超限 | 换更便宜的可用方案；全部不可行则停止并给缺口 |
| 工具错误 | 修正/重试一次、换方案或明确停止；失败步骤保留 |
| 部分成功 | 基于可用部分继续，但任务/终态标注 `partial` 与 caveat |

以下三种不算完成 ReAct：

- 只有固定条件分支；
- 只有自动重试；
- 只有预设决策日志。

本项目用 Mock 规则策略保证离线可复现，但 Mock 只验证流程与异常路径，不证明真实模型决策效果；真实模式由 DeepSeek 提供。

## 6. 输出与终态

### 6.1 正常输出

- 任务计划与活动任务状态；
- 每一步的工具、参数、返回摘要与状态；
- 素材、脚本、估算与保存结果；
- 费用合计、预算余额、所需/可用时间；
- 已保存方案的 `plan_id`、内容哈希、相对路径；
- 决策记录与 Trace。

### 6.2 终态

| 终态 | 含义 |
|---|---|
| `completed` | 方案已保存，预算与截止时间均满足，无保留事项 |
| `completed_with_caveats` | 方案已保存，但存在工具失败/部分结果等保留说明 |
| `incomplete` | 未完成；必须含 `code`、原因、缺口、已完成/失败步骤、下一步建议 |

`incomplete` 不得只返回“失败”两个字；必须说明缺什么、卡在哪一步、哪些结果已保留。

## 7. 成功标准

### 7.1 验收点

| 编号 | 场景 | 通过判定 | 证据 |
|---|---|---|---|
| A-01 | 正常完成 | 预算与截止时间内产出完整交付 | `examples/outputs/01-normal.json` |
| A-02 | 缺预算/截止时间 | 不猜值，明确缺失项与未完成原因 | `examples/outputs/02-missing-input.json` |
| A-03 | 工具失败 | 观察后重规划或明确停止；失败不成功化 | `examples/outputs/03-tool-failure.json`、`06-partial-result.json` |
| A-04 | 预算超限 | 改选或停止；费用与工具返回一致 | `examples/outputs/04-budget-overrun.json` |
| A-05 | 重复执行 | 行为明确、可解释、无矛盾状态 | `examples/outputs/08-repeat-first.json`、`08-repeat-second.json` |
| A-06 | 参考场景 10/12/8 | 12 超预算 → 8 方案 → 估算 → 保存 | `examples/outputs/04-budget-overrun.json` |
| A-07 | 无可行方案 | 未完成 + 缺口；失败步骤不成功化 | `examples/outputs/05-no-feasible.json` |
| A-08（加分） | 失败步骤续跑 | 从失败点继续，已成功结果复用 | `examples/outputs/07-resume-parent.json`、`07-resume-child.json` |
| 上限 | 工具调用上限 | 返回明确未完成状态并保留成功步骤 | `examples/outputs/09-tool-call-limit.json` |

### 7.2 必须成立的不变量

1. `status=completed` 当且仅当：选中方案存在、最终估算成功、预算内、时间可行、保存成功、活动任务成功/部分成功。
2. 任一 `task.status=succeeded` 必须有对应成功工具结果；`failed` 不得对应成功结果。
3. 下游任务不能在上游未成功/部分成功时被标成成功。
4. `cost.total` 必须等于 `estimate_cost` 明细之和，可仅凭 `pricing.json` 复算。
5. 模型提供的金额不能影响估算结果。
6. 工具失败后原 Step 保留 `failed`；重试产生新 Step，`attempt` 递增。
7. 换方案后旧分支保留为 `succeeded/partial/failed + superseded`，不得删除或改成成功。
8. 达到上限必返回 `incomplete` 与 `gap`。
9. 续跑子运行必须含 `parent_run_id`、`reused_steps`，复用步骤不再次实际执行；父运行失败步骤不得计入 `reused_steps`。
10. 相同幂等键返回相同 `run_id`；不同 payload 冲突。

### 7.3 非功能标准

| 类别 | 要求 |
|---|---|
| 可复现 | 固定时钟、确定性 Mock、合成数据；成本可手算 |
| 安全 | 不提交真实密钥、隐私数据、真实素材；`config.yaml`、`.env`、`storage/` 忽略 |
| 可解释 | 每个决策有 `reason_code` 与简短依据；每个任务有证据 Step |
| 可运维 | `/healthz` 暴露版本、模型模式、数据版本；Trace 可按运行查询 |
| 可测试 | 默认测试不依赖网络或密钥；API 测试使用临时数据库 |
| 范围控制 | 不做界面、部署、多 Agent；不引入外部队列或数据库 |

## 8. 验收方式

```bash
pip install -r requirements.txt
pytest -q
python -m uvicorn src.api.app:app --host 127.0.0.1 --port 8000
python examples/run_examples.py --base-url http://127.0.0.1:8000
```

评审可直接检查：

1. `examples/outputs/04-budget-overrun.json` 中 P-STD `12.00` 与 P-LITE `8.00`；
2. `examples/outputs/03-tool-failure.json` 中失败 Step 仍为 `failed`，终态为 `completed_with_caveats`；
3. `examples/outputs/07-resume-child.json` 中 `reused_steps` 与 Trace 的 `inherited_step`；
4. `examples/test-results.txt` / `test-results.xml` 的测试结果；
5. `examples/traces/` 中每一步的工具参数、返回摘要、后续动作与决策依据。
