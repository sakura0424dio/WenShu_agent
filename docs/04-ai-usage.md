# 04 · AI 辅助内容、个人修改及验证

## 1. AI 辅助范围

本项目在 AI 辅助下完成。AI 参与了：

- 需求拆解、架构设计与 `docs/00-design.md` 草稿；
- Python 代码实现（`src/`）；
- pytest 测试（`tests/`）；
- 示例场景、HTTP 运行脚本、Mock 与真实 DeepSeek 输出整理（`examples/`）；
- README 与交付文档；
- 代码复查与缺陷修正（状态真实性、续跑、幂等、部分成功、Trace 等）。

AI 不替代提交者的业务判断。价格规则、方案命名、文案模板、真实模型效果均需人工复核。

## 2. AI 生成/修改的主要内容

| 文件/区域 | AI 参与 | 人工确认点 |
|---|---|---|
| `docs/00-design.md` | 需求映射、架构、状态、工具、流程、测试设计草稿 | 是否与最终实现一致 |
| `src/domain/*` | 校验、Catalog、Decimal 计价、固定时钟 | 成本规则、价格数据 |
| `src/tools/base.py` | 四类工具、失败/部分成功、内容哈希保存 | 保存内容与路径语义 |
| `src/graph/*` | LangGraph 节点、条件边、守卫、终态推导 | 上限、依赖与“失败不洗白”不变量 |
| `src/agents/*` | Mock 规则策略、DeepSeek 适配器 | 真实模型提示词与 API 行为 |
| `src/services/*` | 运行编排、Trace、报告、幂等、续跑 | 续跑复用范围与用户可见语义 |
| `src/api/*` | FastAPI 路由、错误处理、健康检查 | HTTP 状态码与字段契约 |
| `tests/*` | 18 个验收/API 测试 | 断言是否符合业务预期 |
| `examples/*` | 场景清单、生成脚本、实际输出与 Trace | 运行日期、固定时钟、真实执行 |
| `README.md`、`docs/01`、`02`、`03`、`05` | 说明、方案、评估、复盘草稿 | 提交信息（题号/URL/SHA） |

## 3. 人工输入与修改

### 3.1 已确认的人工输入

| 项 | 内容 |
|---|---|
| 项目要求 | 提交者提供 `requirement.md` 中的功能、验收点与提交约束 |
| 实现方式 | 要求分阶段实现，先验证基础功能再完善 |
| 模型选择 | 要求使用 DeepSeek，并提供集中配置填写 API Key 与模型参数 |
| 安全约束 | 不得提交真实密钥或隐私数据 |
| 本地配置 | 提交者在本地 `config.yaml` 填写 Key 与模型名（未进入仓库） |

### 3.2 提交者需要补充的人工修改

如果提交者对代码、提示词、数据或文档做了额外人工修改，请在本节补充：

| 文件/区域 | 人工修改内容 | 验证方式 |
|---|---|---|
| 待填写 | 待填写 | 待填写 |

当前仓库中的代码与示例由 AI 生成并执行验证；提交者仍应逐项审查后再提交，不能仅凭本文档判断正确性。

## 4. 迭代中的关键修正

以下是实现过程中可核对的 AI 修正，均有测试或示例证据：

1. 增加 `guard` 节点，避免模型直接调用工具或提前 `finish`；`AgentAction` 扩展为 `call_tool | finish | stop`。
2. 统一 `caveats` reducer，避免节点返回完整旧列表导致重复追加。
3. 修复 Mock 在“所有方案都已估算”后仍反复换方案的循环；改为明确 `stop(NO_FEASIBLE_PLAN)`。
4. 修复保存内容哈希包含运行字段导致重复执行无法去重的问题；哈希只覆盖稳定方案语义。
5. 增加续跑状态复制、`inherited_from_parent`、`from_parent_run`、`reused_steps`、`inherited_step` Trace；失败步骤保持原状态。
6. 修复续跑 Trace 的归属问题：继承步骤不再因 `decision_id` 重复而混入子运行的 `tool_result`。
7. 增加 `Idempotency-Key` 冲突/进行中处理、父运行单子运行唯一索引。
8. 增加 `RUN_TIMEOUT`、工具调用/步数上限的结构化 `gap`。
9. 修复 `estimate` 时间可行性复用问题：可用分钟每次按当前时钟重算。
10. 增加 DeepSeek 缺 Key 时的 `UnavailableModel`，让服务可启动并返回 `MODEL_UNAVAILABLE`，而不是启动失败或 500。
11. 输出中移除本地绝对路径，保存路径改为仓库相对路径。
12. 真实模型验证时发现：模型会直接选最低价 P-LITE，跳过参考场景要求的 P-STD；在提示词中明确“先评估 P-STD，超预算再降级”。
13. 真实续跑时发现：模型把父运行的历史保存失败误认为本次重试已耗尽；在 `resume_context` 中增加“新尝试、失败注入已清除、可重新调用”的说明，并在上下文中标记步骤来源。
14. Trace 的 `tool_result` 增加 `decision_id` 与显式 `next_action`，让“后续动作”可直接核对。

## 5. 验证方式

AI 实际执行的验证：

```bash
python -m compileall -q src examples tests
pytest -q
```

结果：18 passed。

其他验证：

- 启动真实 FastAPI 服务，运行 `examples/run_examples.py` 生成 9 个场景、11 份报告、10 份 Mock Trace；
- 使用真实 DeepSeek `deepseek-flash` 通过 HTTP 执行 9 组场景、11 份报告、9 份 Trace，结果保存在 `examples/real-deepseek/`；
- 检查 `examples/outputs/04-budget-overrun.json` 与真实输出中的 12 → 8；
- 检查 `examples/outputs/03-tool-failure.json` 及真实输出中的失败步骤仍为 `failed`；
- 检查 `examples/outputs/07-resume-child.json` 与真实续跑输出的 `reused_steps`、`from_parent_run` 和 Trace；
- 检查示例与 README 中无绝对本地路径、无 `sk-` 真实密钥；
- 用临时数据库运行 API 测试，避免污染示例。

## 6. 未验证或仅有限验证的部分

- **真实 DeepSeek 已冒烟，但不是确定性回归**：使用 `deepseek-flash` 完成 9 组场景，单次 5–17 秒；模型输出仍可能随版本、提示词和网络变化，默认测试继续使用 Mock。
- **真实素材/版权/生产部署**：不在范围内，数据全部为合成。
- **并发与长时运行**：只做了单进程同步验证，没有压测。
- **人工业务判断**：价格规则、方案命名、文案模板是否符合真实业务，需要提交者确认。
- **真实模型 Trace 格式**：`examples/real-deepseek/` 保留当时原始事件，没有后加的 `next_action` 字段；其后续动作由紧随的 `model_action` 表达。

## 7. 提交前人工确认

1. 填写 README 顶部的题号、GitHub URL、最终 commit SHA；
2. 如果对代码/文档做了额外人工修改，在第 3.2 节补充“修改内容 + 验证方式”；
3. 确认真实模型验证记录中的 `config.yaml`、`.env` 和 Key 未进入 Git，也未被粘贴到文档或示例输出；
4. 确认 `config.yaml`、`.env`、`storage/` 未被提交；
5. 如为私有仓库，按题目要求给指定评审账号授权；
6. 提交前重新运行 `pytest -q`，确认结果仍为 18 passed。

AI 输出可能包含错误或与真实 API 行为不一致之处；本项目通过测试、固定数据和示例输出降低风险，但不能替代人工审查。
