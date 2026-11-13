# 中国国别包 · 数据出处

本目录的科目表与税码**不是我们设计的**，取自 Odoo 社区版的中国本地化模块，按数据（不是代码）
搬进来。会计准则国别差异极大，自己编等于编错；这层的正确性靠"抄规范 + 逐条核对 + 可复现"。

## 来源

| 项 | 值 |
|---|---|
| 上游仓库 | `https://github.com/odoo/odoo` |
| 分支/版本 | `20.0`（社区版，LGPLv3） |
| 模块 | `addons/l10n_cn`（`depends` 仅 `base`、`account`，社区版可运行） |
| 获取日期 | 2026-11-13 |
| 许可证 | LGPL-3.0（数据文件按上游许可使用；本仓未修改其内容，只做字段归一化） |

## 逐文件

| 仓内文件 | 上游文件 | 上游行数 | 仓内条数 | sha256（上游文件） |
|---|---|---|---|---|
| `accounts-small.json` | `data/template/account.account-cn_common.csv` + `account.account-cn.csv` | 89 + 67 | 156 科目 | `c0b30f418d7e507c…` / `74a09c010f5f918e…` |
| `accounts-large.json` | `account.account-cn_common.csv` + `account.account-cn_large_bis.csv` | 89 + 197 | 286 科目 | `c0b30f418d7e507c…` / `446449e88a82322f…` |
| `tax-codes.json` | `data/template/account.tax-cn_common.csv` | 245 行（= 62 个税码 + 183 条税额分行） | 62 税码 | `9603da75785f213d…` |
| （未搬） | `data/account_tax_report_data.xml` | 4301 行 | — | `5ae9019e4ed18521…` |

完整 sha256：

```
c0b30f418d7e507cf77b5540a66ae275cef731785d3e5c58b284aeb6322b957f  account.account-cn_common.csv
74a09c010f5f918e5ea0c0d94caf244cf2617a5381a1e47f993702a5ebef6019  account.account-cn.csv
446449e88a82322f15b732b7dc2fe3b06b6ed85ac8633b7697a08c0845c4343a  account.account-cn_large_bis.csv
9603da75785f213d0b94c8b36f9ac7da41e813d580dbb8b824326a3a04e044fa  account.tax-cn_common.csv
5ae9019e4ed1852147d3235ed30a60cf3da7413cf38f246e2596e937df725c52  account_tax_report_data.xml
```

## 做了什么归一化（不改语义）

1. 只保留中文名、英文名、类别、父级、是否核销；丢掉 Odoo 内部 xmlid 与视图/演示数据。
2. `account_type`（Odoo 的 16 类）映射到内核封闭五类：`asset_*`→`asset`、`liability_*`→`liability`、
   `equity*`→`equity`、`income*`→`revenue`、`expense*`→`expense`。原值保留在 `odooType` 里可复核。
3. `parent_id` 由 Odoo xmlid 截成科目码（`l10n_cn_common_account_1002` → `1002`）。
4. 税码把 245 行折叠成 62 条：税额分行（repartition）是同一个税码的多行明细，不是独立税码。

## 核对结论与未核对项

- **已核对**：科目编码与中文名与财政部《小企业会计准则》会计科目表（会小企 01/02 表口径）一致；
  `2221 应交税费` 子树（`2221.01.01` 销项税额 / `2221.01.02` 进项税额 / `2221.01.05` 转出未交增值税 /
  `2221.02` 未交增值税）齐备；税率档 13/9/6/5/3/0 与现行增值税税率一致。
- **未核对（本版不用）**：负数税率（`-1` / `-1.5` / `-2`，对应加计抵减/差额征税）——本版计税只接
  非负税率档，用到即拒并说明未实现；`account_tax_report_data.xml` 只作参照，未接入。
- **漂移**：上游改科目表时，重新取数并更新本文与 `localization.py` 的条数断言，差异即暴露。
