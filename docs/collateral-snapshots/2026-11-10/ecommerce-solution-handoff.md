# 电商方案 · 交接页（2026-11-10）

> 给下一个接手的人/会话：不用重读四个 change 的全部规格，先看这一页。
> 口径纪律：**没证据不升格、没执行不说已上线、没 packId 不编 id**。

## 一句话现状

能力面与通道两层的代码已落地并可验证；看板与商业化两层做到「产物就绪、待上架」。
四件 change 里 B 已归档，A/C/D 各自卡在一类**外部输入**上，不算失败，算等条件。

## 四件 change

| change | 进度 | 状态 | 卡在哪 |
|---|---|---|---|
| `truth-ecommerce-capability-surface` | 21/21 | **已归档**（`openspec/changes/archive/2026-11-10-…`） | — |
| `wire-ecommerce-merchant-channels` | 17/21 | 合法、可继续 | 锚点客户 / 真实读取记录 |
| `publish-ecommerce-boards-and-supply` | 7/18 | 合法、可继续 | 生产库 + `fd-industry-data` |
| `ship-ecommerce-commercial` | 12/14 | 合法、可继续 | 市场发布身份 / 隔离环境 |

## 未完成项的重启条件（逐条给「输入 → 第一步」）

### A · `wire-ecommerce-merchant-channels`（剩 4 条）

| 任务 | 需要的输入 | 拿到后第一步 |
|---|---|---|
| 3.1 `tiktok_shop` provider | TikTok 应用审核通过 + 一家商家授权 | 在 `open-connector-mt/src/providers/tiktok_shop/` 按 `docs/tiktok-shop-skeleton-contract.md` 填 definition/actions/executors/runtime，然后 `generate:registry` + `typecheck` |
| 4.1 `merchant_erp` provider | 锚点客户确定所用 ERP 厂商（旺店通/聚水潭/…） | 一厂商一 provider 立项（ADR 0002），按 `docs/merchant-erp-skeleton-contract.md` 实现三腿；厂商差异走配置映射 |
| 3.3 矩阵升格 `tiktok_shop` | 一次真实读取并留测量记录（时间/动作/结果形状） | 把矩阵格的 evidence 加一条 `runtime_read`，重跑 `gen_capability_artifacts.py`，`capability-check` 应绿 |
| 5.1 矩阵升格（逐通道） | 同上 | 同上；**缺真实读取记录就不升格**，这是 B 归档进主规格的硬规矩 |

已知红线：**不要**为想象接口写 provider（ADR 0002 明文）；**不要**在没有真实读取的情况下把矩阵写 `available`。

### C · `publish-ecommerce-boards-and-supply`（剩 11 条）

| 任务组 | 需要的输入 | 拿到后第一步 |
|---|---|---|
| 3.x un-comtrade 扩量 | 生产 PG 写权限 + 免费档 500 次/日配额 | 扩组合矩阵（HS × 伙伴国 × 年份），按 `(reporter, period, cmd_code, flow_code, partner)` 幂等键落库，断点续跑 |
| 4.x 两个人工门 | `tariff-hs-rates` 需过验证码/OCR；`customs-xborder-retail` 需白名单/鉴权 | 先解锁源，再把供给清单行从「人工门未开」改为「已点亮」并写解锁证据 |
| 5.x 词表扩量 | 生产 `entities` 表读权限 | HS 词表按「已落库数据为唯一依据」扩；口岸类仍标注暂缓（缺权威代码表） |

看板段（1.x/2.x）已完成，五张公开板在 `dashboards/`，产物级守卫 `python3 public_projection.py --check <spec>`。

### D · `ship-ecommerce-commercial`（剩 2 条）

| 任务 | 需要的输入 | 拿到后第一步 |
|---|---|---|
| 2.2/2.3 实际上架 | 原包作者身份（市场只允许原作者发布）+ 五语覆盖 | 用 `releases/MARKET-SUBMISSION.md` 逐语言填后台；拿到 packId 后 `release-check` 会把状态从 `draft` 改 `published`，入口矩阵随之放行 |
| 3.3 真机整栈验收 | 隔离环境 / demo 集群（含断公网验证） | 按 `fd-appliance` 租户 v1.2.0 部署，验四项：组件可运行、矩阵可查、双轨各跑一次、缺腿如实 |

## 本轮产出的可复跑门（都进 CI，别当文档看）

```bash
cd fd-erp-vertical-templates
python3 -m fd_erp_vertical_templates.cli capability-check --template templates/crossborder   # 矩阵 ↔ 产物 ↔ 19 锚点
python3 -m fd_erp_vertical_templates.cli release-check --template templates/crossborder --ledger releases/ledger.jsonl
python3 scripts/gen_capability_artifacts.py --check        # 矩阵改了没重生成即红
python3 scripts/entry_matrix.py --check                    # 未发布入口不得进试用列表
python3 scripts/market_submission.py --check               # 四语齐全 + tags 同长
python3 scripts/release_readiness.py --write               # 刷新 releases/READINESS.md
python3 scripts/build_connector_status.py --check          # 对接三态证据链与派生文件一致
```

## 并发提示（2026-11-10 02:26 实测）

同一时刻另有一个会话在 `fd-erp-vertical-templates` 里写「对外能力清单 / 交付门」
（`claims/`、`scripts/claim_gate.py`、`fd_erp_vertical_templates/publish.py`、
`tests/test_delivery_gates.py`，均未跟踪）。判据：文件 mtime 与当前时刻同分钟，
且 `git log` HEAD 未动。

**接手时注意**：
- 跑全量可能撞到对方**写到一半**的红（本次就遇到 4 条红，几分钟后自己变绿）——
  先看失败是否落在 `test_delivery_gates.py`，如是则不是电商线的回归。
- 两个会话共用工作树与 index：`git add` 前先 `git status` 确认只挑自己的文件；
  别人的未跟踪文件不要 add、不要 reset。
- 电商线自己的六条门见上，任一为红才是本线的问题。

## 资产指针

| 文件 | 用途 |
|---|---|
| `fd-erp-vertical-templates/templates/crossborder/capability-matrix.json` | 能力唯一真源（28 格：0 可得 / 4 不提供 / 24 需一次接入） |
| `fd-erp-vertical-templates/connector-status.json` | 对接证据链三态（catalog / executor_tests / live），由 `scripts/build_connector_status.py` 派生；与能力矩阵**互为旁证**，两者当前一致（0 连通 / 9 未接通） |
| `fd-erp-vertical-templates/templates/crossborder/REVIEW-PENDING.md` | 24 格为什么还不能宣称（自动生成） |
| `releases/ledger.jsonl` | 版本 + 矩阵摘要 + 产物摘要（只追加） |
| `releases/READINESS.md` | 上架就绪（语言/台账/下一步） |
| `releases/MARKET-SUBMISSION.md` | 贴市场后台的四语文案 |
| `releases/ENTRY-MATRIX.md` | 哪些入口能给客户（built ≠ published） |
| `collateral-erp/scripts/demo_recon_only.py` | 无内核双轨演示（`--track shopify\|domestic`） |

## 三条踩过的坑，别再踩

0. **测试全绿 ≠ 验过你的东西**：dashboards 的空图守卫把板名硬编码成花卉三张，于是新增的五张
   电商板从未被扫过。已补 `tests/test_public_spec_guards.py`（按 `*-public.spec.json` 通扫，
   并加 meta test 断言扫描集含这五张）。同一处还挖出根因：`industry_common.bar_axis()`
   默认 `scale: True`，违反本仓「柱图必须零基线」的约定——已改 False，电商两张柱图板
   （政策热度、快递运价）已重建转绿；其它线的 10 处既有产物仍 xfail（需其自有 builder 连库重建）。


1. **归档顺序**：多个 change 改同一条 requirement 时，先归档前一个再写后一个的 delta。MODIFIED 是整块替换，按归档前的规格写出的「完整块」在归档后会被判丢场景（本次 A 就因此一度非法，已补场景修好）。
2. **本地产物 ≠ 客户点得开**：看板有 spec + HTML 只算 `built`；`published` 必须有 `releases/deployments.jsonl` 里的 live 记录。入口矩阵的 `--check` 会把「未发布却进试用列表」判红。
