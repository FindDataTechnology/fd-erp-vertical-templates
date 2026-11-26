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
| `crossborder` | `shopify-store` | `shopify_admin` | `get_order`, `list_orders`, `list_inventory_items`, `get_inventory_item`, `list_payouts` | 订单 + 库存 + 结算；`list_payouts` 为 change `wire-ecommerce-merchant-channels`（已归档，见 spec `ecommerce-merchant-channel-wiring`） 新增的只读动作（GraphQL `shopifyPaymentsAccount.payouts`，需 `read_shopify_payments_payouts`）；动作未上线前运行时报 `ACTION_NOT_FOUND`，矩阵把 settlements 记缺口 |
| `crossborder` | `tiktok-store` | `tiktok_shop` | `list_orders`, `list_settlements`, `list_inventory` | TikTok Shop 三腿只读；待商家授权 + 平台应用审核，未过审前矩阵为 `unmapped` |
| `crossborder` | `merchant-erp-store` | `merchant_erp`（按厂商落地为实名 provider，如 `wangdiantong`） | `list_orders`, `list_settlements`, `list_inventory` | 国内三腿；**库存腿唯一来源**，国内三方对账缺此腿不成立 |
| `supply-chain` | `midplatform-file` | `file-service` | `create_upload_url`, `confirm_upload`, `get_download_url`, `list_files` | 供应商发票影像件：预签名直传 → 确认 → 下载/列表（establish-ai-native-ecommerce-stack 3.2）；owner 域隔离 `erp:<tenant>:sp-invoice` |
| `supply-chain` | `platform-notify` | `inapp-notify` | `send_broadcast` | 对账单推送（底座端内消息通道）；留痕=网关广播账本，客户确认仍走 UE 确认门（3.2） |

> 2026-11-10 起本表的承担动作必须与能力矩阵同步：`list_payouts` 未在目录落地前，
> 矩阵 `shopify.settlements` 保持 `unmapped`（可接未接），不得对外说「已支持收款对账」。

## 已知缺口（如实记录，不写进声明）

| 面 | 谁需要 | 目录现状 | 处置 |
| --- | --- | --- | --- |
| 外卖结算单 / 佣金明细 | 餐饮连锁 | `meituan` 仅有 `query_travel`（酒旅），无商户结算动作 | 不声明该绑定；连接器侧新增动作后再加（沿用"拼多多不声明"的同一原则） |
| Shopify payouts / 余额 | 跨境电商 | `shopify_admin` **已有** `list_payouts`（GraphQL `shopifyPaymentsAccount.payouts`，需 `read_shopify_payments_payouts`；2026-11-10 落地） | 动作在，但**无真实店读取记录**，矩阵 `shopify.settlements` 仍为 `unmapped`；商户未启用 Shopify Payments 时该店结算面不适用（与「没有收款」区分）。升格要一次真实读取并留痕 |
| 门店 POS 收银流水 | 餐饮连锁 | 目录无任何 POS 收银 provider | 用角色绑定 `pos`：客户选自己已连通的系统；候选为空时状态面如实说"先去连一个"。**接入骨架已立**：`open-connector-mt/docs/pos-skeleton-contract.md`（动作面/验收判据先行，实现待锚点客户） |
| 淘宝/天猫/京东订单 | 跨境电商 | 目录无这些平台 | 用角色绑定 `cn-commerce`；**接入骨架已立**：`open-connector-mt/docs/cn-commerce-skeleton-contract.md`；拼多多明确不留槽位。国内首选电商 ERP 通道（`merchant_erp`，三腿齐），平台直连按锚点客户逐个上 |

## 什么时候更新这张表

- **新增/修改一条固定 provider 的绑定**：必须同时填本表，否则 pytest 红（缺对位）。
- **换 provider**：改 provider 列，并在目录里确认动作仍成立（第二条门会核对）。
- **连接器侧新增动作**：把"已知缺口"里的面挪进对位表，再改包声明——顺序不能反。
