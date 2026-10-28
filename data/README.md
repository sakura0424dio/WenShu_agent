# data 说明

本目录全部为**合成数据**，仅用于演示与测试：

- `materials.json`：虚构素材及授权信息，不含真实素材。
- `pricing.json`：虚构价格与工期，仅用于成本复算，不代表真实市场报价。
- `policy.json`：默认执行上限与方案优先级。

价格与工期通过 `estimate_cost` 工具统一计算，金额使用 `Decimal` 并保留两位小数。
