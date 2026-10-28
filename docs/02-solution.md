# 02 · 调用流程、动态决策与技术取舍

## 1. 总体架构

```text
HTTP Client
   │ POST /v1/runs, POST /v1/runs/{id}/resume
   │ GET /v1/runs/{id}, /trace, /v1/plans/{id}
   ▼
FastAPI (src/api)
   │
   ▼
RunService (src/services/runner.py)
   ├─ 输入校验 / 幂等键 / 运行记录
   ├─ Agent 选择：Mock 或 DeepSeek
   ├─ 应用层状态快照（SQLite runs 表）
   └─ 报告组装 + Trace JSONL
           │
           ▼
LangGraph StateGraph (src/graph)
  prepare → agent → guard → execute_tool → agent … → finalize
           │
           ├─ Catalog（素材/价格/策略）
           ├─ Clock（固定或系统时钟）
           └─ 四类工具（src/tools/base.py）

独立存储：
  storage/app.db                运行记录 + 状态快照 + 响应
  storage/checkpoints.sqlite3   LangGraph SqliteSaver 检查点
  storage/plans/*.json          内容寻址保存的方案交付物
  examples/traces/*.jsonl       审计 Trace
```

三层职责分离：

- **模型决策层**：只选择动作，不能写工具状态、费用或最终状态。
- **工具事实层**：只返回真实结果，不能改计划或伪造成功。
- **运行编排层**：校验、执行、状态持久化、报告与 Trace。

## 2. 端到端调用流程

### 2.1 正常请求

```text
POST /v1/runs
  → 解析并校验请求（未知字段拒绝）
  → 缺字段/非法字段：直接 finalize，返回 incomplete，steps=[]
  → 计算幂等键；命中相同 payload：返回原报告，deduplicated=true
  → 创建 run_id，初始化状态与上限
  → LangGraph:
      prepare → agent → guard → execute_tool → agent …
      → finalize
  → 写 runs 表、Trace JSONL、报告
  → 返回 HTTP 200 + 报告
```

图路由：

```text
START → prepare
prepare ──valid──→ agent
prepare ──invalid→ finalize
agent → guard
guard ──call_tool→ execute_tool
guard ──reject───→ agent
guard ──finish/stop→ finalize
execute_tool ──继续→ agent
execute_tool ──上限/终止失败→ finalize
finalize → END
```

### 2.2 每轮循环

1. `agent` 读取当前状态和最近观察，输出一个 `AgentAction`：
   - `call_tool(tool, arguments)`
   - `finish`
   - `stop(reason_code)`
2. `guard` 校验：
   - 工具名是否合法；
   - 依赖是否满足（脚本前必须有素材；估算前必须有对应脚本；保存前必须有估算）；
   - 候选方案是否可用；
   - 预算/截止时间是否可行；
   - 步数与工具调用次数是否超限。
3. 非法动作不执行，写入观察，回到 `agent` 重新决策。
4. 合法 `call_tool` 交给 `execute_tool`，这是唯一写工具事实的节点。
5. 工具结果（含 `success/partial/failed`、错误码、是否可重试、明细）作为观察回灌。
6. 模型根据观察选择下一步或结束。
7. `finalize` 按证据推导终态，不由模型直接决定。

### 2.3 失败续跑

```text
父运行 incomplete
   │ 校验：非缺输入/非法输入、非 running、状态快照存在
   ▼
创建子运行：新 run_id，parent_run_id=父，attempt=父+1
   │
   ├─ 复制 candidates/materials/scripts/artifacts/estimates/tasks
   ├─ 复制 steps；全部标 inherited_from_parent=true
   ├─ 成功/部分成功步骤另标 from_parent_run=true，计入 reused_steps
   ├─ 失败步骤保留原 failed，不改成成功，不计入 reused_steps
   ├─ 清空 last_observation、tool_attempts、forced_code、incomplete
   ├─ 清空失败注入（默认），保留 partial 相关 caveat
   └─ 新 LangGraph thread_id = child_run_id:attempt
   ▼
模型从当前状态继续：缺什么补什么，已成功步骤不重复调用
```

已完成运行再次续跑返回原报告并标 `resume_noop=true`；缺输入/非法输入不可续跑。

## 3. LangGraph：版本、职责与取舍

### 3.1 版本与使用功能

- `langgraph==1.2.18`
- `langgraph-checkpoint-sqlite==3.1.2`
- 使用：
  - `StateGraph(RunState)`；
  - `add_conditional_edges` 条件边；
  - `execute_tool → agent` 回边；
  - `SqliteSaver` 检查点；
  - `recursion_limit=80`；
  - `Annotated[list, operator.add]` reducer。

### 3.2 节点职责

| 节点 | 职责 | 是否可写工具事实 |
|---|---|---|
| `prepare` | 校验/归一化输入；恢复父运行状态；检查失败注入开关 | 否 |
| `agent` | 调用模型适配器得到一个 `AgentAction`；写决策记录 | 否 |
| `guard` | 校验工具名、依赖、`finish` 前提、步数/工具调用上限 | 否 |
| `execute_tool` | 唯一执行工具并写 Step、Task、Observation、费用、保存结果 | **是** |
| `finalize` | 按证据推导 `completed` / `completed_with_caveats` / `incomplete` | 否 |

### 3.3 对框架默认行为的修改/未使用

1. **不用预置 `ToolNode`**：预置节点难以同时表达 `partial`、失败注入、任务分支、依赖校验和“失败步骤永不洗白”；因此自建 `execute_tool`。
2. **不用默认终止条件**：业务上限（步数、工具调用、运行时间）先于图递归上限终止，返回结构化 `incomplete`。
3. **`recursion_limit=80`**：12 步业务上限下每次循环约 3 个 superstep，80 足够且能防止异常回边。
4. **状态 reducer 分层**：`steps/observations/decisions/caveats` 只追加，保留失败/重试历史；`tasks/estimates/selected_plan_id` 整体替换，避免并发追加歧义。
5. **检查点与业务续跑分离**：`SqliteSaver` 用于审计每个 superstep；A-08 的业务续跑从应用层状态快照创建新线程，避免依赖“从检查点原地恢复”的隐含语义。

选型理由：LangGraph 的显式图、条件边和状态 reducer 适合“动作—观察—再决策”的可审计循环；相比手写 while 循环，图结构更容易验证上限、回边和终态；相比多 Agent 框架，本需求明确排除多 Agent。

## 4. 动态决策

### 4.1 成本超限

`estimate_cost` 返回明细、合计、预算、余额与时间可行性。模型观察到 `within_budget=false` 时，可：

- 选择更便宜的可用方案，重新生成脚本并估算；
- 若无任何可行方案，`stop(NO_FEASIBLE_PLAN)`。

换方案时旧分支不删除：旧任务标 `superseded`，原 Step 状态不变；`tool_results.superseded_tasks` 保留可审计证据。

参考场景（预算 10）：

```text
P-STD 估算 12.00 → within_budget=false
→ 改选 P-LITE
→ 生成 P-LITE 脚本
→ 估算 8.00 → within_budget=true
→ save_plan(P-LITE)
→ finish
```

### 4.2 工具错误

工具返回 `failed` 时，模型可以：

- 修正参数后重试一次；
- 换方案；
- 明确 `stop`。

同一工具同一参数最多重试一次；`save_plan` 连续失败两次必须 `stop(SAVE_FAILED)`，避免无限重试触发工具调用上限。失败 Step 永远保留 `failed`，重试产生新 Step ID 和递增 `attempt`。

### 4.3 部分成功

工具返回 `partial` 时：

- Step/Task 标 `partial`，不伪装成 `success`；
- caveat 写入报告；
- 模型可基于可用部分继续（例如基础素材仍可生成脚本），但最终状态只能是 `completed_with_caveats`，不能是 `completed`。

### 4.4 输入缺失/非法

`prepare` 直接校验，不进入模型循环：

- 缺 `topic/budget/deadline` → `incomplete[MISSING_REQUIRED_INPUT]`，`steps=[]`；
- 预算非正/超两位小数、截止时间无时区或已过期 → `incomplete[INVALID_INPUT]`。

### 4.5 上限与停止

- `max_steps`、`max_tool_calls` 由请求在硬上限内收紧；
- 达到上限 → `incomplete[STEP_LIMIT_REACHED]` / `TOOL_CALL_LIMIT_REACHED`；
- 报告列出已完成步骤、剩余任务与缺口；
- 运行超时 → `incomplete[RUN_TIMEOUT]`；
- 模型不可用/输出非法 → `MODEL_UNAVAILABLE` / `MODEL_OUTPUT_INVALID`。

## 5. 状态真实性与任务依赖

### 5.1 事实边界

1. 只有 `execute_tool` 写工具状态。
2. `agent` 不能写 `steps`、`tasks`、`status`、费用。
3. 工具返回 `failed` → Step/Task `failed`；不自动改成功。
4. `save_plan` 成功前不能 `finish`，`guard` 会拒绝并回灌观察。
5. `finalize` 只有在“保存成功 + 活动任务成功/部分成功 + 最终估算预算/时间可行”时才返回 `completed`。
6. 换方案不删除旧 Step；旧任务保留原状态并标 `superseded`。

### 5.2 任务依赖

任务 ID 采用语义命名，依赖关系显式可读：

```text
search
  └─ script:<plan_id>
       └─ estimate:<plan_id>
            └─ save:<plan_id>
```

下游任务不能在上游未成功/部分成功时标成功；`guard` 在调用工具前再次检查依赖。

## 6. 工具与成本

| 工具 | 关键校验 | 失败/部分成功 |
|---|---|---|
| `search_materials` | `topic` 非空；limit 限制在 1–12 | 无精确匹配仍返回全部素材并标 `NO_EXACT_MATCH`；可注入 `partial` 仅保留基础素材 |
| `generate_script` | `plan_id` 存在；素材属于该方案等级且数量足够 | 素材不足/不存在 → `PRECONDITION_FAILED`/`NOT_FOUND`；可注入缺 CTA 的部分脚本 |
| `estimate_cost` | 脚本属于该方案；输入已归一化 | 只读 `pricing.json`；模型无法传价格 |
| `save_plan` | 估算/脚本 ID 匹配；预算内；截止时间足够；写文件 | 写失败 → `SAVE_ERROR`；按内容哈希幂等，重复保存 `already_exists=true` |

费用计算：

```text
total = materials + script + production
P-STD  = 5 + 3 + 4 = 12
P-LITE = 2 + 2 + 4 = 8
P-VIDEO= 7 + 4 + 4 = 15
```

金额使用 `Decimal`，对外为字符串；`estimate()` 每次按当前时钟重算 `available_minutes`。保存内容哈希只覆盖稳定方案语义（主题、预算、截止时间、方案、素材、脚本、价格规则、估算明细），排除 `run_id`、`estimate_id`、`available_minutes` 等运行字段。

## 7. 重复执行与保存

- `Idempotency-Key` 在 `runs` 表唯一；相同 key + 相同 `request_hash` 返回原响应；不同 payload → `409 IDEMPOTENCY_KEY_CONFLICT`。
- 无 key：新运行；保存工具按内容哈希去重，返回同一 `plan_id` 与 `already_exists=true`。
- 同一父运行的续跑子运行通过部分唯一索引限制为一个。
- 保存路径为仓库相对路径，报告不泄漏本地绝对路径。

## 8. 异常与终态推导

| 情况 | 终态/代码 |
|---|---|
| 缺 `topic/budget/deadline` | `incomplete[MISSING_REQUIRED_INPUT]`，`steps=[]` |
| 非法预算/时间 | `incomplete[INVALID_INPUT]`，`steps=[]` |
| 超预算/超时且无替代 | `incomplete[NO_FEASIBLE_PLAN]`，`gap.minimum_feasible_cost/budget_shortfall` |
| 保存失败且重试无效 | `incomplete[SAVE_FAILED]`，失败 Step 保留 |
| 其他工具失败无替代 | `incomplete[TOOL_FAILURE_UNRECOVERED]` |
| 达到步数/工具调用上限 | `incomplete[STEP_LIMIT_REACHED]` / `TOOL_CALL_LIMIT_REACHED` |
| 运行超时 | `incomplete[RUN_TIMEOUT]` |
| 模型不可用/输出非法 | `incomplete[MODEL_UNAVAILABLE]` / `MODEL_OUTPUT_INVALID` |
| 保存成功但有失败/部分结果 | `completed_with_caveats` |
| 保存成功且任务健康 | `completed` |

`incomplete` 包含：

- `code`、`message`；
- `missing_fields` / `invalid_fields`；
- `gap`（最低可行成本、差额、上限、剩余任务等）；
- `completed_steps`、`failed_steps`、`preserved_results`；
- `next_actions`。

## 9. 可观测性

- 每个运行写 JSONL Trace：`run_started`、`validation`、`model_action`、`tool_call`、`tool_result`、`inherited_step`、`run_finished`。
- 每条 `tool_result` 带 `decision_id` 与显式 `next_action`：
  - 正常继续：下一条动作、工具、`reason_code`；
  - 工具后直接终止：`action="end"` + 终态/错误码。
- 续跑继承步骤用 `inherited_step` + `source_run_id`；成功复用标 `reused=true`，父运行失败历史标 `reused=false`。
- 不记录模型隐藏思维过程，只记录简短 `decision_basis`。

## 10. 技术取舍

| 选择 | 替代 | 理由 |
|---|---|---|
| 同步 HTTP + 单进程 SQLite | 队列 / PostgreSQL | 工具是本地 Mock，单次秒级；零外部依赖，评审即可运行 |
| 自建 `guard` + `execute_tool` | LangGraph `ToolNode` | 需要失败注入、部分成功、依赖校验和状态真实性 |
| 应用层续跑 | 从检查点原地恢复 | 业务语义清晰，能明确标记复用步骤；检查点仍保留用于审计 |
| 确定性 Mock | 真实模型跑测试 | 测试不依赖网络/密钥，结果可复现；真实模型另行验证 |
| `Decimal` + 字符串 | float | 预算/费用可精确复算 |
| 内容哈希保存 | 随机或自增 ID | 重复执行不产生重复交付物 |
| JSON 文件数据 | 数据库表 | 可读、可 diff、便于评审复算 |

## 11. 已知实现偏差

- 设计文档中的 `tool_cache`、独立 `steps/plans` 表未实现；当前运行状态与步骤保存在 `runs.state_json`，方案以 JSON 文件落盘。续跑复用状态，不做工具级缓存。
- 检查点用于审计，不作为业务续跑的唯一来源。
- 真实 DeepSeek 需要用户提供 Key；默认测试只覆盖 Mock 与配置缺失路径。真实 `deepseek-flash` 的 9 组 HTTP 结果单独保存在 `examples/real-deepseek/`，不纳入确定性 pytest。
