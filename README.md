# 内容任务规划 Agent

在预算与截止时间内，由 Agent 通过四类工具完成一份可保存的内容活动方案；工具失败、预算超限、输入缺失时如实报告缺口，不把失败步骤洗成成功。

> 提交信息（提交前由提交者补全）
>
> - 题号：`<待填写>`
> - GitHub URL：`<待填写>`
> - 最终 commit SHA：`<待填写>`

## 1. 环境要求

| 项 | 要求 / 验证值 |
|---|---|
| Python | 3.10+；当前验证环境 3.10.11 |
| 操作系统 | Windows 11；命令使用 Git Bash，Linux/macOS 可直接替换激活路径 |
| 依赖 | 见 `requirements.txt` / `pyproject.toml` |
| LangGraph | 1.2.18 |
| FastAPI | 0.162.0 |
| 模型 | 默认 Mock（离线）；可选 DeepSeek `deepseek-flash` |
| 数据 | `data/` 中全部为合成素材、合成价格与策略 |

主要依赖：

```text
langgraph==1.2.18
langgraph-checkpoint-sqlite==3.1.2
fastapi==0.162.0
uvicorn==0.54.0
pydantic==2.14.1
langchain-openai
pytest==9.2.0
```

## 2. 安装

```bash
python -m venv .venv
source .venv/Scripts/activate          # Windows Git Bash
# source .venv/bin/activate            # Linux / macOS
pip install -r requirements.txt
```

配置文件：

```bash
cp config.example.yaml config.yaml
```

`config.example.yaml` 默认使用 `mode: "mock"`，不填 Key 也能完整运行。`config.yaml`、`.env`、`storage/` 均已被 `.gitignore` 忽略。

## 3. 运行

### 3.1 启动服务

```bash
python -m uvicorn src.api.app:app --host 127.0.0.1 --port 8000
```

健康检查：

```bash
curl -s http://127.0.0.1:8000/healthz
```

预期返回模型模式、LangGraph 版本与数据版本，例如：

```json
{"status":"ok","model_mode":"mock","clock_mode":"fixed","langgraph_version":"1.2.18","data_versions":{"materials":"materials-v1","pricing":"pricing-v1","policy":"policy-v1"}}
```

### 3.2 发起一次正常规划

```bash
curl -s -X POST http://127.0.0.1:8000/v1/runs \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: demo-normal-v1" \
  -d '{"topic":"春季露营装备种草","budget":20,"deadline":"2026-11-05T18:00:00+08:00","options":{"model_mode":"mock","max_steps":12,"max_tool_calls":8}}'
```

预算 10 的参考场景 10/12/8：

```bash
curl -s -X POST http://127.0.0.1:8000/v1/runs \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: demo-budget-overrun-v1" \
  -d '{"topic":"春季露营装备种草","budget":10,"deadline":"2026-11-05T18:00:00+08:00","options":{"model_mode":"mock","max_steps":12,"max_tool_calls":8}}'
```

预期：P-STD 估算 `12.00` 超预算，改选 P-LITE，估算 `8.00` 并保存，终态 `completed`。

### 3.3 运行测试

```bash
pytest -q
pytest --junitxml=examples/test-results.xml
```

当前结果：**18 passed**（A-01～A-08、部分成功、工具调用上限、Trace、API、幂等、续跑）。

## 4. 评审入口

建议按以下顺序评审：

1. **先看测试**：`pytest -q`，不依赖网络或密钥。
2. **再看示例索引**：`examples/results.json`，列出 9 组场景、11 份实际报告及其 `run_id`。
3. **逐项核对验收点**：

| 验收点 | 必看文件 | 预期观察 |
|---|---|---|
| A-01 正常完成 | `examples/outputs/01-normal.json` | `completed`，P-STD，合计 12.00，方案已保存 |
| A-02 缺预算/截止时间 | `examples/outputs/02-missing-input.json` | `MISSING_REQUIRED_INPUT`，缺 `budget`，`steps=[]` |
| A-03 工具失败 | `examples/outputs/03-tool-failure.json`、`06-partial-result.json` | S002 保持 `failed`；改选 P-LITE；partial 保留 caveat |
| A-04 预算超限 | `examples/outputs/04-budget-overrun.json` | P-STD 12.00 超预算 → P-LITE 8.00 → 保存 |
| A-05 重复执行 | `examples/outputs/08-repeat-first.json`、`08-repeat-second.json` | 同一 `run_id`，第二次 `deduplicated=true` |
| A-06 参考场景 10/12/8 | 同 A-04 | 先估算 12，再改选 8，再估算并保存 |
| A-07 无可行方案 | `examples/outputs/05-no-feasible.json` | `NO_FEASIBLE_PLAN`，最低可行 8.00，差额 3.00 |
| A-08 失败续跑 | `examples/outputs/07-resume-parent.json`、`07-resume-child.json` | 父 `SAVE_FAILED`；子 `attempt=2`，复用 S001–S005，S008 保存成功 |
| 工具调用上限 | `examples/outputs/09-tool-call-limit.json` | `TOOL_CALL_LIMIT_REACHED`，保留成功步骤并列出剩余任务 |

4. **核对 Trace**：`examples/traces/*.jsonl` 中每个 `tool_result` 都带 `decision_id`、步骤、工具结果摘要和显式 `next_action`；续跑用 `inherited_step` 标明 `source_run_id`。
5. **核对真实模型记录**：`examples/real-deepseek/`，与 Mock 分开存放，不混用、不互相冒充。
6. **看文档**：`docs/03-evaluation.md` 引用上述示例、Trace 和测试结果。

## 5. 核心能力

- **完整 ReAct 循环**：`prepare → agent → guard → execute_tool → agent … → finalize`。模型每轮只输出一个动作，工具观察回灌，下一轮依据观察调整。
- **动态决策**：成本超限换方案；工具失败后修正/重试、换方案或明确停止；部分成功基于可用结果继续并标注 caveat；全部不可行时给出最低成本与预算差额。
- **四类工具**：素材检索、脚本生成、成本估算、方案保存。费用只来自 `data/pricing.json`，模型无权填金额。
- **状态与事实分离**：只有 `execute_tool` 能写工具状态、费用、工期和任务状态；失败步骤永远保留 `failed`。
- **可复算成本**：`Decimal` + 字符串金额。P-STD=12、P-LITE=8、P-VIDEO=15。
- **可解释重复执行**：`Idempotency-Key` 精确复用；保存按内容哈希去重；相同输入重跑得到同一 `plan_id`。
- **失败续跑（加分）**：从父运行创建子运行，复用成功步骤，失败步骤保留历史；`reused_steps` 与 Trace 可核对。
- **审计 Trace**：JSONL 记录决策、工具参数、返回摘要、错误、后续动作与续跑继承关系。
- **DeepSeek 接入**：OpenAI 兼容接口，配置集中；已用 `deepseek-flash` 完成 9 组真实 HTTP 场景；未填 Key 时服务仍可启动并返回 `incomplete[MODEL_UNAVAILABLE]`。

**Mock 免责声明**：Mock 是本地规则策略，只验证流程、异常处理与状态一致性，不证明真实大模型的决策效果。真实模型效果由 `examples/real-deepseek/` 单独记录，且仍可能随模型版本变化。

## 6. API

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/v1/runs` | 同步执行一次规划；`Idempotency-Key` 可选 |
| `GET` | `/v1/runs/{run_id}` | 查询运行报告 |
| `GET` | `/v1/runs/{run_id}/trace` | 查询结构化 Trace |
| `POST` | `/v1/runs/{run_id}/resume` | 从失败点续跑，创建子运行 |
| `GET` | `/v1/plans/{plan_id}` | 查询已保存方案 |
| `GET` | `/healthz` | 健康检查、模型模式、LangGraph 版本、数据版本 |

业务未完成（缺输入、超预算、工具失败、达到上限）返回 **HTTP 200**，body 中为 `status=incomplete` 与 `incomplete` 原因；只有协议错误、未知资源或服务内部错误使用 4xx/5xx。

关键响应字段：

- `status`：`completed` / `completed_with_caveats` / `incomplete`
- `plan`：选中方案与活动任务
- `steps`：工具调用历史（参数、状态、尝试次数、错误、是否复用）
- `tool_results`：素材、脚本、估算、保存结果、被替代任务
- `cost` / `time`：明细合计、预算、余额、所需/可用分钟
- `saved_plan`：`plan_id`、内容哈希、相对路径、`already_exists`
- `incomplete`：错误代码、原因、缺口、已完成/失败步骤、建议
- `decisions`：每轮动作、`reason_code`、简要依据
- `trace`：Trace 路径与决策数量

### 6.1 重复执行

同一 `Idempotency-Key` + 相同 payload：返回原运行，`deduplicated=true`。不同 payload：`409 IDEMPOTENCY_KEY_CONFLICT`。

不传幂等键、相同输入再次提交：创建新 `run_id`，但保存工具按内容哈希返回同一 `plan_id`，`already_exists=true`。

### 6.2 失败后续跑

```bash
curl -s -X POST http://127.0.0.1:8000/v1/runs/<parent_run_id>/resume \
  -H "Content-Type: application/json" \
  -d '{"failure_injection":[]}'
```

子运行保留 `parent_run_id`、`attempt`、`reused_steps`；已成功步骤不再执行，失败步骤保留原记录，新尝试产生新 `step_id`。已完成运行再次续跑返回原报告，`resume_noop=true`。

## 7. 配置 DeepSeek

编辑 `config.yaml`：

```yaml
model:
  mode: "deepseek"
  provider: "deepseek"
  api_key: "sk-你的密钥"
  base_url: "https://api.deepseek.com"
  name: "deepseek-flash"
  temperature: 0
  timeout_seconds: 30
  max_tokens: 1200
```

也可以用环境变量覆盖（优先级高于配置文件）：

```bash
export MODEL_MODE=deepseek
export DEEPSEEK_API_KEY=sk-...
export DEEPSEEK_BASE_URL=https://api.deepseek.com
export DEEPSEEK_MODEL=deepseek-flash
```

- `config.yaml`、`.env` 已被 `.gitignore` 忽略；**不要提交真实密钥**。
- `.env` 会被自动加载，但不会覆盖已经存在的环境变量。
- 未填 Key 时，HTTP 服务仍能启动；运行返回 `incomplete[MODEL_UNAVAILABLE]`，不会崩溃或伪造成功。
- 真实模型决策质量取决于提示词、模型版本和网络；仓库默认用 Mock 保证测试可复现。每次运行只在 `options.model_mode` 或配置中切换模式，不会把 Mock 结果标成真实模型结果。

## 8. 示例与验收材料

`examples/` 中的请求、响应、Trace 均由 `examples/run_examples.py` 通过 HTTP 实际生成，不是手写伪造。

启动服务后重新生成：

```bash
python -m uvicorn src.api.app:app --host 127.0.0.1 --port 8126
python examples/run_examples.py --base-url http://127.0.0.1:8126
```

示例使用固定时钟 `2026-11-01T09:00:00+08:00`、截止时间 `2026-11-05T18:00:00+08:00`，因此 `available_minutes=6300` 可复现。请在 `config.yaml` 中设置同样的 `demo.fixed_now`，或设置 `APP_FIXED_NOW`。

目录：

```text
examples/
├─ scenarios.yaml            # 9 组场景清单
├─ run_examples.py           # 通过 HTTP 生成实际输出/Trace
├─ inputs/                   # 11 份实际请求
├─ outputs/                  # 11 份实际响应
├─ traces/                   # 10 份 Mock 运行 Trace
├─ results.json              # Mock 场景索引
├─ real-deepseek/            # 真实 DeepSeek 的输入、输出、Trace、索引
└─ test-results.txt / .xml   # pytest 结果
```

真实 DeepSeek 验证记录单独放在 `examples/real-deepseek/`：使用 `deepseek-flash` 通过 HTTP 执行同样的验收场景，包含实际输入、报告、Trace 和 `results.json`。它与 Mock 示例分开保存，避免把规则策略结果当成真实模型结果。详见 [`examples/real-deepseek/README.md`](examples/real-deepseek/README.md) 与 [`docs/03-evaluation.md`](docs/03-evaluation.md)。

## 9. 成本规则

`data/pricing.json`（合成价格，仅用于本项目复算）：

| 方案 | 素材 | 脚本 | 制作 | 合计 | 工期 |
|---|---:|---:|---:|---:|---:|
| P-STD | 5.00 | 3.00 | 4.00 | **12.00** | 180 分钟 |
| P-LITE | 2.00 | 2.00 | 4.00 | **8.00** | 120 分钟 |
| P-VIDEO | 7.00 | 4.00 | 4.00 | **15.00** | 300 分钟 |

估算工具返回明细、合计、预算、余额、所需/可用分钟。模型只能引用估算结果，不能自行填金额；保存前执行器再次核对预算、截止时间、脚本与估算引用。

## 10. 安全与数据

- `data/` 中全部为合成素材、合成价格，不含真实客户数据。
- 不提交真实 API Key、`.env`、`config.yaml`、`storage/`。
- 真实模型模式只发送必要的主题、预算、截止时间与工具摘要，不发送本地密钥或文件路径到模型。
- 日志/响应不包含完整密钥；`healthz` 只暴露模式、版本与数据版本。

## 11. 已知限制

- 仅支持单机同步执行；没有队列、并发限流或生产级权限系统。
- 业务续跑使用应用层状态快照 + 新 LangGraph 线程，不是从 SQLite 检查点原地 `resume`。
- 没有实现工具级结果缓存；续跑只复用已成功的运行状态。
- 默认测试与 `examples/outputs/` 是确定性 Mock；真实 DeepSeek 结果单独保存，两者不混用。
- 示例依赖固定时钟；若当前时间晚于示例截止时间，需要设置 `APP_FIXED_NOW` 或改用未来截止时间，不能通过修改价格“凑”通过。
- `GET /v1/plans/{plan_id}` 只查询本服务保存目录中的方案，不提供列表或删除接口。

## 12. 文档索引

- [项目设计](docs/00-design.md)
- [需求分析与成功标准](docs/01-requirements.md)
- [方案与 LangGraph 说明](docs/02-solution.md)
- [测试、实际结果与限制](docs/03-evaluation.md)
- [AI 使用说明](docs/04-ai-usage.md)
- [复盘](docs/05-retrospective.md)
