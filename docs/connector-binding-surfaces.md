# 连接器绑定的面级对位表

> **为什么需要这张表**：paas 的目录覆盖检查是**类级**的——只看这个 provider 有没有
> `read` / `write` **类**的动作。所以一个 provider 只要有任意一个 read 动作，一条声明
> "只读"的绑定就会被判 `satisfied`，并给客户授权一个**可能根本不对应他需要的那个面**的令牌。
>
> 2026-11-08 实测抓到两例，都是我们自己的包（已修）：
> - `meituan` 写进"外卖结算"——目录里 meituan 唯一的 read 是 `query_travel`（酒旅/机票）；
> - `shopify` 写进"订单/收款/库存"——目录里 `shopify` 的 13 个 read **全是博客/页面**；
>   订单与库存在 `shopify_admin` 里，而 payouts 一个动作都没有。
>
> 所以**每条固定 provider 的绑定都要在这里点名"哪个动作承担这个面"**，由
> `tests/test_connector_binding_providers.py` 核对：表里的动作必须真的存在于该 provider
> 的目录清单。角色绑定（`providerRole: true`）不在此表——它的面由客户选的系统决定。
>
> 核对数据来自两处：本机有 `open-connector-mt` checkout 时直接读目录（最新），没有就读入库快照
> `tests/fixtures/connector-binding-actions.json`（由 `scripts/refresh_connector_catalog_ids.py` 生成）。
> 所以 **CI 里这道门不会跳过**——`.github/workflows/gates.yml` 每次 push/PR 都跑全套 pytest。
> 目录侧动作变化时，刷新快照并随包一起提交。

## 对位表

格式：`包目录 | 绑定 key | provider | 承担动作（逗号分隔）| 说明`

| 包目录 | 绑定 key | provider | 承担动作 | 说明 |
| --- | --- | --- | --- | --- |
| `supply-chain` | `kingdee-cloud` | `kingdee` | `query_records`, `get_record`, `submit_records`, `audit_records`, `push_records` | 凭证/往来读写；read 三个 + write 四个（allocate/audit/push/submit），金蝶的 destructive 动作不在授权面内 |
| `supply-chain` | `payments-merchant` | `wechatpay` | `download_bill`, `query_transaction` | 商户侧结算流水/交易查询；备选 `alipay` 的 `download_settlement_bill`, `query_trade` 同面 |
| `restaurant-chain` | `payments-merchant` | `wechatpay` | `download_bill`, `query_transaction` | 同上；备选只有 `alipay`（`meituan` 只有 query_travel，已剔除） |
| `crossborder` | `shopify-store` | `shopify_admin` | `get_order`, `list_orders`, `list_inventory_items`, `get_inventory_item` | 订单 + 库存；**payouts 无动作，故不承诺** |

## 已知缺口（如实记录，不写进声明）

| 面 | 谁需要 | 目录现状 | 处置 |
| --- | --- | --- | --- |
| 外卖结算单 / 佣金明细 | 餐饮连锁 | `meituan` 仅有 `query_travel`（酒旅），无商户结算动作 | 不声明该绑定；连接器侧新增动作后再加（沿用"拼多多不声明"的同一原则） |
| Shopify payouts / 余额 | 跨境电商 | `shopify_admin` 无 payout/balance 动作 | 不承诺收款对账；连接器侧新增后可加回 |
| 门店 POS 收银流水 | 餐饮连锁 | 目录无任何 POS 收银 provider | 用角色绑定 `pos`：客户选自己已连通的系统；候选为空时状态面如实说"先去连一个"。**接入骨架已立**：`open-connector-mt/docs/pos-skeleton-contract.md`（动作面/验收判据先行，实现待锚点客户） |
| 淘宝/天猫/京东订单 | 跨境电商 | 目录无这些平台 | 用角色绑定 `cn-commerce`；同上。**接入骨架已立**：`open-connector-mt/docs/cn-commerce-skeleton-contract.md`；拼多多明确不留槽位 |

## 什么时候更新这张表

- **新增/修改一条固定 provider 的绑定**：必须同时填本表，否则 pytest 红（缺对位）。
- **换 provider**：改 provider 列，并在目录里确认动作仍成立（第二条门会核对）。
- **连接器侧新增动作**：把"已知缺口"里的面挪进对位表，再改包声明——顺序不能反。
