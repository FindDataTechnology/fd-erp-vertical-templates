# 非版本化文档快照（2026-11-10）

本目录**不是**这些文档的主副本。主副本在工作区根下的非 git 目录：

| 快照文件 | 主副本位置（不在任何 git 仓） |
|---|---|
| `ecommerce-solution-handoff.md` | `finddata/docs/ecommerce-solution-handoff.md` |
| `crossborder-supply-map.md` | `finddata/docs/crossborder-supply-map.md` |
| `bossai.md` | `finddata/docs/presales-yichang/bossai.md` |
| `collateral-solution-00-方案说明.md` | `finddata/collateral-solution/00-方案说明.md` |
| `demo-guide-crossborder.md` | `finddata/collateral-erp/demo-guide-crossborder.md` |

## 为什么要放在这里

`finddata/` 根不是 git 仓（`git rev-parse` 在 `docs/`、`collateral-solution/`、
`collateral-erp/` 均返回非仓库）。这些文件承载了本次电商改造的**对外口径**
（能力三态的说法、发布状态、演示的合成数据标注），却没有任何版本历史——
一旦被覆盖就无法回答「上个月对客户说的是什么」。

所以把**当时的确切内容**复制进本仓留档：有 diff 可比、有提交可追。改主副本后
重跑复制即可；本目录内容以主副本为准，不做反向编辑。

## 与代码的关系

这些快照里出现的能力表述必须与 `templates/crossborder/capability-matrix.json` 一致。
机器校验入口：

```bash
python3 -m fd_erp_vertical_templates.cli capability-check --template templates/crossborder
```

锚点（`<!-- capability:platform.flow=status -->`）指向的文档是主副本路径，因此
**改主副本后要同步本快照**，否则 `capability-check` 与快照会分叉。
