# 电商方案 · 执行项与前置条件表（跨境 + 国内双轨）

> 本文只回答两件事：**还剩哪些可执行项**，以及**每一项卡在什么前置条件上**（客户 / 库 / 身份 / 环境）。
> 不含实现，不改任何产物。数字均可按文末命令复算。

## 0. 结论先说

双轨收敛后，真正被**客户**卡住的只有一类：**国内平台直连（淘宝/天猫/京东）与商家电商 ERP 的 provider 实现**——ADR 0002 明令「先有客户再实现」，没有锚点客户就没有可写的真实接口形状。

其余各项都能内部推进，只是各自卡在不同的**非客户**前置上：生产库与配额（C1）、人工过一次验证码/开白名单（C2/C3）、原作者身份（D1）、隔离环境（D2/D3）。

也就是说：**「等客户」只挡住 1 类 3 项；「等内部资源」挡住 4 项；剩下的是口径工作，已做完。**

## 1. 现状（可核）

| 项 | 值 | 复算命令 |
|---|---|---|
| 能力矩阵 | v1.1.1，28 格 = **0 可得 / 24 需一次接入 / 4 不提供** | 见文末 `matrix` |
| 平台 | 7 个：shopify、tiktok_shop、taobao、tmall(共用 taobao)、jd、merchant_erp、pinduoduo(不立 provider) | 同上 |
| 对接三态 | **0 接通 / 9 未接通**，problems 0 | `build_connector_status.py --check` |
| 角色绑定 | 5 个候选集**全空**、全部 `install_blocked` | 见文末 `status` |
| 计划中接口 | 20 条（4 个能力域） | 同上 |
| 发布台账 | matrix 1.1.1 / connector-pack 1.5.4 / report-preset 1.1.3 / account-mapping 1.1.3 / recon-fleet 1.1.3 / manifest 1.4.4 | `releases/READINESS.md` |
| 租户包 | crossborder 1.2.1、erp 1.0.0 | `fd-appliance/tenants/*/bundle.json` |
| 门 | 15 道全绿（本仓 414 passed / 15 skipped + 看板仓 125 passed） | `scripts/check_all.py` |

**未接通的 9 项明细**（`connector-status.json`）：

| 模板 | provider | 缺 |
|---|---|---|
| crossborder | `cn-commerce`（国内店铺） | catalog · executor_tests · live |
| crossborder | `shopify_admin` | live |
| restaurant-chain | `pos` | catalog · executor_tests · live |
| restaurant-chain | `wechatpay` | live |
| supply-chain | `carrier-portal` | catalog · executor_tests · live |
| supply-chain | `kingdee` | executor_tests · live |
| supply-chain | `supplier-portal` | catalog · executor_tests · live |
| supply-chain | `wechatpay` | live |
| supply-chain | `wms` | catalog · executor_tests · live |

## 2. 前置条件表（主表）

「前置」列用四类标注：**客户** / **库** / **身份** / **环境**。验收判据一律是机器可判的（门或三态台账），不接受口头完成。

### A · 连接器与能力面（跨境内圈 + 国内直连）

| # | 可执行项 | 轨 | 前置 | 第一步 | 验收判据 |
|---|---|---|---|---|---|
| **A1** | `shopify_admin` 升 `live`（三态唯一差 live） | 跨境 | **客户**（商家自有店 + 只读凭证 BYOK）<br>**环境**（出网出口） | 商家给 `shopDomain` + Admin API token（只读 scope），各跑一次 `list_orders` / `list_payouts` 真实读 | 矩阵对应格写 `measured_at` + `runtime_read` 证据；`connector-live-acceptance.json` 记一条带证据文件；三态转 connected |
| **A2** | `taobao`（含 tmall）provider 实现 | 国内 | **客户**（锚点客户）<br>**身份**（开放平台 app key/secret + OAuth 授权） | 客户签约后按其类目权限写只读 adapter；先 orders，再 settlements | catalog + executor_tests + live 三态齐全；矩阵从 unmapped 升格 |
| **A3** | `merchant_erp` provider 实现（旺店通/聚水潭类） | 国内 | **客户**（锚点客户）<br>**身份**（ERP 开放 API 账号） | 同上；**库存腿只有这条能给**，三方对账缺它不成立 | 同上；库存格必须真读到数据才算 |
| **A4** | `tiktok_shop` provider 实现 | 跨境 | **客户**（锚点客户）<br>**身份**（Shop API 应用凭证） | 同 A2 | 同上 |
| **A5** | 5 个空角色绑定解锁（`cn-commerce` / `wms` / `carrier-portal` / `supplier-portal` / `pos`） | 双轨 | **客户**（在连接器里选定实际品牌） | 客户选定品牌 → 按该品牌流程新增 provider → 重新派生 | `role_bindings[].candidates` 非空、`install_blocked=false` |
| **A6** | 国内平台资质的合规确认（企业资质、类目授权） | 国内 | **客户**（资质主体） | 商务侧与客户确认资质归属后再谈 A2/A3 排期 | 不作为技术判据；仅作为 A2/A3 的启动前提 |

### C · 外圈数据（看板供给）

| # | 可执行项 | 轨 | 前置 | 第一步 | 验收判据 |
|---|---|---|---|---|---|
| **C1** | un-comtrade 扩量批次（**120 组合 = 1 批**，日预算 500） | 双轨 | **库**（生产库可写）<br>**身份**（免费档 key） | `scripts/comtrade_expansion_plan.py --out plan.json` → 按计划跑 → 记录完成清单 | `--check` 自洽；落库行数 = 计划组合数；断点清单可续跑 |
| **C2** | `tariff-hs-rates` 人工门解锁（HS 税率） | 跨境 | **身份**（人工过一次验证码 / OCR 取表） | 人工过一次验证码后回填；或用 OCR 取表后人工核对种子三码 854142 / 851762 / 610910 | 供给清单该行从「人工门未开」改「已点亮」并写解锁证据；门关期间不得产出该源任何行 |
| **C3** | `customs-xborder-retail` 人工门解锁（海关零售统计） | 跨境 | **身份**（IP 白名单 / 鉴权凭证） | 向数据源申请 IP 白名单或取鉴权凭证 | 同上；且**「成功为空」≠「已核对」**，空结果须显式标记 |

### D · 商业化与交付

| # | 可执行项 | 轨 | 前置 | 第一步 | 验收判据 |
|---|---|---|---|---|---|
| **D1** | 三件资产正式上架（connector-pack 1.5.4 / account-mapping 1.1.3 / recon-fleet 1.1.3） | 双轨 | **身份**（原作者 `authorEmail`） | 用原作者身份提交；四语与 tags 已在 `releases/MARKET-SUBMISSION.md` 备好 | 市场可见对应版本；台账对应行转 `published` |
| **D2** | 真机整栈验收（含断网演示） | 双轨 | **环境**（隔离机，无外网/DNS 受控） | 出包 → 隔离机启动 → 跑两轨演示 | 出包 VERIFY PASS；演示双轨均通；全程无外网依赖 |
| **D3** | 租户包交付（crossborder 1.2.1 / erp 1.0.0） | 双轨 | **环境**（目标机 docker/k3s + 许可） | `appliance build` → 交付 → 目标机落地 | 组件数与 `components.json` 一致；启动自检通过 |
| **D4** | 客户侧端到端 POC | 双轨 | **客户**（试点门店/店铺 + 人） | 以 A1 或 A2/A3 的真实读取为前提 | 真实数据跑通一次对账，差异出核对建议（**不出资金指令**） |

## 3. 依赖关系（哪些必须先解开）

```
A6 客户资质 ──▶ A2/A3 provider 实现 ──▶ A5 角色绑定解锁 ──▶ D4 端到端 POC
                      ▲
        A1（只需商家授权，比 A2/A3 轻，可先做）
C2/C3 人工门 ──▶ 看板供给完整度（与客户无关，可并行）
C1 生产库+配额 ──▶ 看板数据量（与客户无关，可并行）
D1 身份 / D2 环境 ──▶ 商业化收口（与客户无关，可并行）
```

三条硬约束：

1. **A2/A3/A4 不得在没有客户的情况下开工**（ADR 0002：不为想象的接口写 provider）。技术上「能写」不等于「该写」。
2. **A5 依赖客户选品**，不是我们替客户选；空候选集已由门阻断安装，不是靠提醒。
3. **C2/C3 的门关期间**，调度不产出该源任何行，看板按缺位呈现——不允许「假装有数据」。

## 4. 不需要外部输入、可随时做的内部项

| # | 项 | 状态 |
|---|---|---|
| I1 | 事实快照范围：全量 1575 项 vs 精选 8 项 | **待裁决**（方案与补丁见 `docs/snapshot-scope-tradeoff.md` §5.1-5.4，未落地） |
| I2 | 供给清单里「人工门未开」两行 | 已如实挂账，等 C2/C3 解锁后改状态 |
| I3 | live 验收记录填写流程 | 已备好（`connector-live-acceptance.json` + 证据文件存在性校验），等 A1 的真实证据 |
| I4 | 15 道门与覆盖面断言 | 已上线并做过负例，无需再动 |

## 5. 红线（执行任一 A/C/D 项时都不得越界）

- **禁爬淘宝/拼多多等平台前台**：只走官方开放平台/商家授权接口；拼多多因上游 schema 缺失，如实不提供。
- **二清红线**：只出核对与更正**建议**，不做资金归集/代收代付/分账执行。
- **凭证 BYOK**：客户凭证零落库、零日志；只用不存。
- **无出处汇率不折算**：差异呈现里汇率缺出处一律只出原币口径。
- **只讲可验证的话**：任何「已接通/已支持」必须有台账或门作证。

## 6. 复算命令

```bash
cd fd-erp-vertical-templates

# matrix：格数、三态分布、平台与 connector
python3 - <<'PY'
import json, pathlib
from collections import Counter
m = json.loads(pathlib.Path("templates/crossborder/capability-matrix.json").read_text())
c = Counter(cell["status"] for p in m["platforms"] for cell in p["flows"].values())
print("version:", m.get("version"), "| cells:", sum(c.values()), dict(c))
print("platforms:", [(p["key"], p.get("connector")) for p in m["platforms"]])
PY

# status：三态、未接通明细、角色绑定候选集
python3 - <<'PY'
import json, pathlib
s = json.loads(pathlib.Path("connector-status.json").read_text())
print("connected:", len(s["verdict"]["connected"]), "not_connected:", len(s["verdict"]["not_connected"]))
for i in s["integrations"]:
    print(" ", i["template"], i["provider"], i["missing"])
for r in s["role_bindings"]:
    print(" role:", r["role"], "candidates:", r["candidates"], "blocked:", r["install_blocked"])
PY

# 门
python3 scripts/check_all.py

# comtrade 批次规模
python3 -c "
import sys; sys.path.insert(0,'scripts')
from comtrade_expansion_plan import build_matrix, split_by_day, DAILY_BUDGET
c=build_matrix(); print('combos:', len(c), 'budget:', DAILY_BUDGET, 'batches:', len(split_by_day(c)))"
```
