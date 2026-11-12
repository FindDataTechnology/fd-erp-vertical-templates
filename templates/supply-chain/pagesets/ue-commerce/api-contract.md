# UE 订货台页面集 · 后端 API 契约（pack: supply-chain-ue-commerce v0.1.0）

> pack 定义：`templates/supply-chain/pagesets/ue-commerce/manifest.json`。
> 对应 change `add-supply-chain-vertical` tasks 6.2（页面集）与 6.5（配套后端面）。
> **同源纪律：本契约全部端点与 MCP 工具面同源数据（同一内核与编排层），无平行取数路径。**
> 本文件是契约（contract-only）：配套后端面未实施，实施在 paas/后端侧按各自 openspec 根另立；路径前缀 `/erp/supply-chain/v1` 为本 pack 声明的契约空间，实施时可按网关实况调整但 SHALL 经 change 修订本契约。

## 0. 通用约定

- 鉴权：壹座底座登录态（扫码绑定 → PAT，`paas/miniapp/src/lib/runtime.ts` 契约）；请求携带租户上下文；服务端按 `supply-chain.ue` 页面集授权 + 页面级 requiredGrants 校验。
- 数据域：仅本租户、本人所属客户档案；越域一律 404（不泄露存在性）。
- 错误形态：沿用底座统一错误码（machine-readable code + 人话 message，对齐 `mp-demo-sandbox` 的 code 纪律）；拒绝必须可解释（如信用不足给出占用明细）。
- WS：业务实时事件（订单状态、确认门决议回执）复用底座 WS 通道与 `answer_question` 契约（`paas/miniapp/src/components/QuestionCard.tsx` 同构），不另开 WS 面。
- 同源对位：账本侧消费账本内核 8 工具面（submit/list/confirm/reject_proposal、query_balance、query_entries、run_reconciliation、get_recon_report）；连接器侧消费萬星连接器（金蝶 provider）拉取面。

## 1. 商品域（功能点：商品检索 / 商品详情）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/ue/products` | `keyword`、`categoryId`、`page/cursor`、`sort` | 分页商品卡（标题/规格/图片/可见性）；检索面与 AI 助手商品问答同源 | `catalog.read` |
| GET | `/erp/supply-chain/v1/ue/product-categories` | — | 分类树（含禁售/限售标注） | `catalog.read` |
| GET | `/erp/supply-chain/v1/ue/products/{productId}` | — | 详情：规格、价格（取价引擎结果：客户价/活动价/券，带出处与生效期）、库存可见性 | `catalog.read` |

## 2. 下单域（功能点：下单；确认门）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/ue/cart` | — | 进货单行集 | `catalog.read` |
| POST | `/erp/supply-chain/v1/ue/cart/lines` | `productId/skuId/qty` | 行快照（含当次取价） | `catalog.read` |
| DELETE | `/erp/supply-chain/v1/ue/cart/lines/{lineId}` | — | 剩余行集 | `catalog.read` |
| POST | `/erp/supply-chain/v1/ue/cart/price-preview` | 行集 | 试算结果：行价/优惠/合计 + 信用额度预估占用；**纯读，不占用额度** | `catalog.read` |
| POST | `/erp/supply-chain/v1/ue/orders` | 行集、收货信息、备注 | 订单受理结果：经信用校验（账本额度占用记账）与订单确认；额度不足拒绝并附占用明细；订单进确认车道 | `order.write` |
| POST | `/erp/supply-chain/v1/ue/orders/{orderId}/fulfillment` | — | 触发履约编排：移交记录 → 履约计划；无移交记录（订单未确认）= 409 `WORKFLOW_STATE`，不伪造计划；已有计划直接回既有视图（幂等） | `order.write` |

确认门语义：`POST /orders` 的受理结果以「提交→服务器确认→拒绝回显→超时兜底」呈现；落账类后续（对账、结算）走账本 proposal 面（confirm/reject 同源）。

## 3. 查单域（功能点：查单）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/ue/orders` | 状态/时间段/关键词、分页 | 订单列表（状态机字段可解释） | `order.write`（读自有订单） |
| GET | `/erp/supply-chain/v1/ue/orders/{orderId}` | — | 订单详情：行/金额/履约路径（直发/仓内）/状态 | 同上 |
| GET | `/erp/supply-chain/v1/ue/orders/{orderId}/fulfillment` | — | 履约编排状态 + 运单轨迹 + 电子回单；同源交付编排面（WMS/承运商事件经连接器回流） | 同上 |

## 4. 对账域（功能点：对账单确认；确认门）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/ue/statements` | 期间/状态 | 对账单列表 | `statement.confirm` |
| GET | `/erp/supply-chain/v1/ue/statements/{statementId}` | — | 对账单明细（单据级勾对）；数据与金蝶往来面同源（金蝶 provider 拉取、中台账本内核计算） | `statement.confirm` |
| POST | `/erp/supply-chain/v1/ue/statements/{statementId}/confirm` | `decision: confirmed/disputed`、`disputeNote?` | 确认回执；确认经确认门，超时/失败不假报成功；异议进入内部处置车道 | `statement.confirm` |
| POST | `/erp/supply-chain/v1/ue/reconciliations` | `left_source`/`right_source`（来源引用）、`retry_task_id?` | 202 + `task_id` + `poll_url`：**长任务**，内核后台执行，本响应不等结果；零外部写入（只读滩头） | `statement.confirm` |
| GET | `/erp/supply-chain/v1/ue/reconciliations/{taskId}` | — | 对账报告：`queued/running/done/failed/interrupted` 与四类差异（双侧原始引用留痕） | `statement.confirm` |

## 5. 开票域（功能点：开票申请）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/ue/invoice-requests` | 状态/期间 | 申请列表 + 进度 | `invoice.write` |
| POST | `/erp/supply-chain/v1/ue/invoice-requests` | 对账单/订单引用、抬头、税号、接收方式 | 申请受理（进后台审核车道）；开票执行在金蝶/开票通道（金蝶 provider 无开票直连 action 时走线下通道，见 `docs/connector-onboarding-supply-chain.md` §1.2） | `invoice.write` |

## 6. 售后域（功能点：售后申请 / 售后进度）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| POST | `/erp/supply-chain/v1/ue/aftersale-requests` | `orderId`、行引用、类型（退/换/补）、凭证图片 | 申请单号；进售后处置车道 | `aftersale.write` |
| GET | `/erp/supply-chain/v1/ue/aftersale-requests` | 状态/期间 | 列表 | `aftersale.write`（读自有） |
| GET | `/erp/supply-chain/v1/ue/aftersale-requests/{requestId}` | — | 详情与处置进度（回传处置结果同源编排面） | 同上 |

## 7. AI 助手入口（spec：AI 助手入口）

- 复用底座 chat 面（`pages/chat/index`）与 WS 流式契约，无独立 AI 端点。
- 经集群/舰队面接经营问答、智能推荐、预警处置；答复来自与页面同源的指标口径（单一出处，tasks 7.1），不泄露其他租户数据。
- AI 建议类输出标注为建议；动作类输出必须经确认门后生效。

## 8. 功能点覆盖对账

| 功能域（tasks 6.2 枚举） | 承载页面 | 状态 |
|---|---|---|
| 商品检索 | ue-home / ue-product-search | 覆盖 |
| 商品详情 | ue-product-detail | 覆盖 |
| 下单 | ue-cart / ue-order-create | 覆盖 |
| 查单 | ue-order-list / ue-order-detail | 覆盖 |
| 对账单确认 | ue-statement-confirm | 覆盖 |
| 开票申请 | ue-invoice-apply | 覆盖 |
| 售后 | ue-aftersale-apply / ue-aftersale-detail | 覆盖 |
| AI 助手入口（spec 附加） | ue-ai-assistant | 覆盖 |

锚点蓝图 UE 25 功能点按上述 7 个功能域聚合核对：**7/7 域覆盖，0 域待一期裁剪**；逐点映射待锚点清单资产化后回填（挂账）。
