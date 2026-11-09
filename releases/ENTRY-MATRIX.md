# 对外入口矩阵（派生件，勿手改）

> 台账：`ledger.jsonl` · 试用列表只收 `published`。

| 入口 | 类型 | 版本 | 状态 | 进试用列表 | 备注 |
|---|---|---|---|---|---|
| 跨境电商连接器包 | pack | v1.4.0 | draft | 否 | — |
| 跨境电商科目映射包 | pack | v1.1.0 | draft | 否 | — |
| 跨境电商对账舰队 | fleet | v1.1.0 | draft | 否 | — |
| 跨境电商·外圈贸易画像 | board | — | built | 否 | 产物齐（spec+html） |
| 跨境电商·政策热度 | board | — | built | 否 | 产物齐（spec+html） |
| 跨境电商·物流压力 | board | — | built | 否 | 产物齐（spec+html） |
| 国内电商·快递物流 | board | — | built | 否 | 产物齐（spec+html） |
| 国内电商·网上零售（缺位板） | board | — | built | 否 | 产物齐（spec+html） |

## 口径

- 市场单元（pack/fleet）状态取台账最新一行：`draft` = 未发布，`published` = 已上架。
- 看板三态：`unpublished`（无产物）/ `built`（产物就绪，未上线）/ `published`（已上线，需 `releases/deployments.jsonl` 里的 live 记录）。
- **本地产物 ≠ 客户点得开**：没有部署记录一律不入试用列表。
- 未发布入口**不得**出现在任何试用列表；上架后由台账翻 `published` 再刷新本表。
