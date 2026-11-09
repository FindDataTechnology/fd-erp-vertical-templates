# 上架就绪清单（自动生成，勿手改）

> 模板：`crossborder` · 台账：`ledger.jsonl`
> 市场要求语言：en/ja/es/fr

| 资产 | 版本 | 台账 | 上架单元 | 语言 | 缺语言 | 待办 |
|---|---|---|---|---|---|---|
| capability-matrix | v1.1.0 | draft v1.1.0 | 否（随模板） | zh | en/ja/es/fr | 就绪 |
| connector-pack | v1.5.3 | draft v1.5.3 | 是 | en/es/fr/ja/zh | — | 走市场上架 |
| report-preset | v1.1.2 | draft v1.1.2 | 否（随模板） | zh | en/ja/es/fr | 就绪 |
| account-mapping-pack | v1.1.2 | draft v1.1.2 | 是 | en/es/fr/ja/zh | — | 走市场上架 |
| recon-fleet | v1.1.2 | draft v1.1.2 | 是 | en/es/fr/ja/zh | — | 走市场上架 |

## 口径

- 「上架单元」= 市场上单独发布的资产（pack / fleet）；能力矩阵与报表 preset 随模板分发。
- 语言缺项按 `i18n` 键判定，顶层 name/description 视为中文。
- 版本与台账一致时不能重发同内容（发布门会拒），必须 bump。
