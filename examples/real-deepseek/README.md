# 真实 DeepSeek 验证记录

本目录是 **2026-10-28** 使用真实 DeepSeek API 通过 HTTP 服务执行的实际结果，不是 Mock 输出，也不是手工编写的报告。为便于独立存放，仅将报告中的 `trace.path` 重定位到本目录的 Trace 副本；运行结果与 Trace 事件内容未改写。

## 环境

| 项 | 值 |
|---|---|
| 模型模式 | `deepseek` |
| 模型 | `deepseek-flash`（已通过 `/models` 确认账号可用） |
| Base URL | `https://api.deepseek.com` |
| 固定时钟 | `2026-11-01T09:00:00+08:00` |
| API | `POST /v1/runs`、`POST /v1/runs/{run_id}/resume` |
| 服务端口 | `127.0.0.1:8125` |

API Key 只保存在本地 `config.yaml`，未写入本目录或任何提交文件。

## 结果

| 场景 | 状态 | 方案 | 费用 | 关键证据 |
|---|---|---|---:|---|
| `01-normal` | `completed` | P-STD | 12.00 | 预算 20；四类工具全部成功并保存 |
| `02-missing-input` | `incomplete` | — | — | `MISSING_REQUIRED_INPUT`，`steps=[]`，未调用模型 |
| `03-tool-failure` | `completed_with_caveats` | P-LITE | 8.00 | 首次脚本超时保留 `failed`；重试成功后 P-STD 仍超预算，改选 P-LITE |
| `04-budget-overrun` | `completed` | P-LITE | 8.00 | P-STD 12.00 超预算 10.00，模型改选 P-LITE 8.00 |
| `05-no-feasible` | `incomplete` | P-VIDEO | 15.00 | 所有方案均超预算 5.00；最低可行 8.00，差额 3.00 |
| `06-partial-result` | `completed_with_caveats` | P-LITE | 8.00 | 检索首次返回部分结果，模型重试后继续并保留 caveat |
| `07-resume-parent` | `incomplete` | P-LITE | 8.00 | `SAVE_FAILED`；两次保存均失败，成功步骤保留 |
| `07-resume-child` | `completed` | P-LITE | 8.00 | `attempt=2`，复用 S001–S005，仅新增一次成功保存 |
| `08-repeat-first` | `completed` | P-STD | 12.00 | 首次运行 |
| `08-repeat-second` | `completed` | P-STD | 12.00 | 同 `Idempotency-Key`，`deduplicated=true`，同一 `run_id` |
| `09-tool-call-limit` | `incomplete` | P-STD | 12.00 | `max_tool_calls=3`，保留已完成步骤并列出剩余任务 |

`07-resume-child` 的关键 Trace：父运行失败步骤仍为 `failed`，继承成功步骤标 `from_parent_run=true`，新保存步骤 `S008` 的来源为子运行。

> Trace 格式说明：本目录保留真实运行时的原始事件，未事后改写。后续动作由紧随 `tool_result` 的 `model_action` 表达；仓库当前版本新生成的 Mock Trace 另含显式 `next_action` 字段，两者语义一致。

## 文件

```text
examples/real-deepseek/
├─ inputs/     # 实际 HTTP 请求体
├─ outputs/    # 实际响应报告
├─ traces/     # 实际 JSONL Trace（9 份唯一运行）
└─ results.json
```

## 复现

1. 在 `config.yaml` 中设置 `model.mode=deepseek`、`model.name=deepseek-flash` 和本地 API Key；
2. 启动服务；
3. 按 `inputs/` 中的请求体调用 API。

真实模型输出可能随模型版本变化；本目录记录的是上述时间点的实际结果，不应作为确定性回归测试。默认的 `pytest` 仍使用 Mock，保证无网络、无密钥时可重复验证。
