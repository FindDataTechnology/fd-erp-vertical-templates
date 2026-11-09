# fd-erp-vertical-templates

ERP 行业模板层（OpenSpec change `add-erp-vertical-templates` 的实施产物）：
「一个内核 + 行业模板工厂」落到两个已点名行业——**跨境电商**与**餐饮连锁**。

行业差异全部上移为**模板资产四件套**（连接器 pack + 舰队蓝图 + 科目映射 pack +
报表 preset）；账本内核（[fd-erp-ledger-mcp](../fd-erp-ledger-mcp)）保持行业无关。

## 边界铁律（boundary.py 机械化审计，随测试全绿）

1. **零旁路账本**：模板层不存任何账务事实（余额/分录/科目余额表）——一切账务
   经内核 8 工具（`submit_proposal` / `confirm_proposal` / `query_balance` /
   `query_entries` / ...）。
2. **零直连库表**：本包不 import 任何 DB 驱动，不出现内核表名 SQL。
3. **回写仅经内核白名单车道**：对账结论只以 proposal（pending）形态回写；
   确认门（`confirm_proposal`）只属拍板席位；回写科目限白名单
   （应收/在途/收入/费用），资金类分录禁入机器车道。
4. **对外部系统零写入**：连接器只有 read 面。

## 目录

```
fd_erp_vertical_templates/
  manifest.py       四件套清单 schema（erp-vertical-template/v1，pack v2 兼容）
  mapping.py        科目映射 pack 契约（呈现层重算；替换映射不改账）
  boundary.py       模板边界审计器（AST + JSON 静态证明四条边界）
  kernel_client.py  内核 8 工具同形协议（MCP 工具面为生产形态）
  stores.py         餐饮门店主数据 + 确认车道（非账务参考数据，不落内核）
  connectors/       shopify / tiktok_shop / merchant_erp / cn_commerce / pos / payments
                    （出网单口=连接器运行时 runtime_client；BYOK，只读滩头）
  runtime_client.py 连接器运行时客户端（POST /v1/actions/<provider>.<action>，
                    模板层不拼 vendor URL、不发 HTTP、不读凭证）
  capability.py     电商能力矩阵（平台×流三态 + 证据/复核窗 + 对账降级/报表腿派生）
  capability_check.py 矩阵 ↔ 产物 ↔ 对外文档锚点的一致性门
  release.py        发布门与台账（同版本不同摘要拒、版本回退拒、同摘要幂等跳过）
  recon/            three_way（四类差异）+ crossborder / restaurant 驱动
                    + shares（分成对账单，零资金执行面）+ writeback（白名单车道）
scripts/
  gen_capability_artifacts.py  矩阵 → 报表 preset / 连接器 pack i18n / REVIEW-PENDING
  release_readiness.py         上架就绪（语言缺口/台账状态/下一步）
  market_submission.py         市场提交材料（四语 name/description/tags）
  entry_matrix.py              对外入口矩阵（built ≠ published）
templates/
  crossborder/        四件套清单 + connector-pack / recon-fleet /
                      account-mapping / report-preset
  restaurant-chain/   四件套清单 + connector-pack / dailyclose-fleet /
                      account-mapping / report-preset
```

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# 边界审计（只读，零写路径）
.venv/bin/fd-erp-vertical-templates audit
.venv/bin/fd-erp-vertical-templates validate --template templates/crossborder
.venv/bin/fd-erp-vertical-templates capability-check --template templates/crossborder
.venv/bin/fd-erp-vertical-templates release-check --template templates/crossborder \
    --ledger releases/ledger.jsonl

# 测试（e2e 需真实 Postgres，DB 约束层不可 mock）
ERP_LEDGER_DATABASE_URL="postgresql://..." .venv/bin/pytest -q
```

## 内核侧配套（本 change 任务 1.1 落地于 fd-erp-ledger-mcp）

- 迁移 `0002_universal_accounts.sql`：通用科目最小集预设（12 科目，v1）+
  逐租户幂等种子函数。
- CLI：`fd-erp-ledger-mcp seed-accounts [--tenant X | --all-tenants]`。
- MCP 工具面保持 8 工具不变（surface selfcheck 双向断言通过）。

## 如实守界（不得对外宣称越界能力）

- 拼多多店铺接入当前不提供（上游接口 schema 缺失）；WooCommerce 在出现合格
  开源 MCP server 前不接入。
- Shopify 真实店读写暂缓（crm-board 2026-10-30 裁决），待商家自带凭证逐店上线。
- POS 厂商适配器逐个上线，首个厂商由首单客户决定；未适配厂商如实报出 +
  导出文件替代通道；不以人工补录冒充自动接入。
- 收款渠道 live 面以实测为准（未实测不宣称）。
- 资金归集/代收代付/分账执行不提供（二清红线）：分成只出双方可核对的
  分成对账单与核对指令数据，划转由品牌方经持牌通道执行。
- 中央厨房/采购/BOM/排班/会员等重域当前不提供（后续渐进模板资产）。
- 谦面市场 live 上架、registry 推送属独立部署步骤（挂账）——本包为
  publish-ready 形态（同 erp-ledger-kernel-pack 先例）。
- **能力面以 `templates/crossborder/capability-matrix.json` 为准**：内圈当前
  0 格可得、拼多多 4 格「不提供」、其余 24 格「需一次接入」。要升格须有
  provider 动作 + 一次真实读取记录（缺一不可）；`available` 不是「写了代码」的意思。
- Shopify payout 动作（`list_payouts`）已落地但**尚无真实店读取**，结算面仍按
  「需一次接入」表述；商户未启用 Shopify Payments 时该面不适用（与「没有收款」区分）。

## appliance 三档（fd-appliance/tenants）

- `crossborder/` v1.2.0：三舰 + 对账舰队 + 模板四件套 + 能力矩阵（11 组件）。
- `restaurant-chain/` v1.0.0：日结舰队 + 模板四件套（4 组件，新建租户）。
- 出包验证：`python3 -m appliance_packer build tenants/<tenant> --out <dir>`
  → VERIFY PASS（跨境租户 2026-11-10 实跑：components=11）。
