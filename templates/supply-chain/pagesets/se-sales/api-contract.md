# SE 业务员台页面集 · 后端 API 契约（pack: supply-chain-se-sales v0.1.0）

> pack 定义：`templates/supply-chain/pagesets/se-sales/manifest.json`。
> 对应 change `add-supply-chain-vertical` tasks 6.3（页面集）与 6.5（配套后端面）。
> **同源纪律：本契约全部端点与 MCP 工具面同源数据（同一内核与编排层），无平行取数路径。**
> 契约空间声明同 UE（`ue-commerce/api-contract.md` §0），以下不再重复：鉴权/数据域/错误形态/WS 复用/同源对位全部一致；差异仅在本端 requiredGrants 与代理身份维度。

## 0. 本端特有约定

- 代理身份：`POST /se/orders/proxy` 等代客操作 MUST 在请求与服务端日志、业务单据三处留业务员身份；「代客」不改变数据域（仍限本人名下客户）。
- 拍板席位：预警处置的确认/拒绝走与 owner 席位同一确认门实现（账本内核 confirm/reject proposal 同源），移动端不引入第二套拍板状态机。

## 1. 工作台

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/se/workspace` | — | 名下客户数/待办/预警数/今日拜访计划聚合卡 | 页面集授权 |

## 2. CRM 域（功能点：CRM 线索拜访）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/se/customers` | 关键词/分页 | 名下客户列表（信用占用/未决对账摘要） | `crm.read` |
| GET | `/erp/supply-chain/v1/se/customers/{customerId}` | — | 客户 360：档案/近单/对账状态/额度占用（账本 query_balance 同源） | `crm.read` |
| GET | `/erp/supply-chain/v1/se/customers/{customerId}/orders` | 分页 | 名下该客户订单 | `crm.read` + `order.write.proxy` |
| GET | `/erp/supply-chain/v1/se/leads` | 状态/分页 | 线索池（本人） | `crm.read` |
| POST | `/erp/supply-chain/v1/se/leads` | 线索要素 | 建线索 | `crm.visit.write` |
| POST | `/erp/supply-chain/v1/se/visits` | 客户/线索引用、纪要、`location?`（可选） | 拜访登记 | `crm.visit.write` |
| GET | `/erp/supply-chain/v1/se/visits` | 期间 | 拜访记录 | `crm.visit.write`（读本人） |

## 3. 代客下单域（功能点：代客下单；确认门）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| POST | `/erp/supply-chain/v1/se/orders/proxy` | `customerId` + 行集 + 收货信息 | 受理结果：与 `POST /ue/orders` 同一取价引擎与信用校验链路（账本额度占用同源），代理身份留痕；额度不足可解释拒绝 | `order.write.proxy` |
| GET | `/erp/supply-chain/v1/se/orders` | 客户/状态/期间 | 名下订单列表 | `order.write.proxy` |
| GET | `/erp/supply-chain/v1/se/orders/{orderId}` | — | 订单详情（同 UE 契约形状） | 同上 |

## 4. 预警处置域（功能点：预警处置；确认门·拍板席位）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/se/alerts` | 状态/级别 | 投递到席位的阈值预警（tasks 7.2 预警→处置任务闭环的移动呈现） | `alert.handle` |
| POST | `/erp/supply-chain/v1/se/alerts/{alertId}/handle` | `decision: accept/reject`、`note?` | 处置回执：确认门语义（提交→服务器确认→拒绝回显→超时兜底）；拍板与 owner 席位同源生效，拒绝留痕不落账 | `alert.handle` |

## 5. 对账协助域

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/se/statements` | 状态 | 名下客户对账单状态总览 | `statement.confirm.proxy` |
| POST | `/erp/supply-chain/v1/se/statements/{statementId}/push` | 渠道（端内消息） | 推送回执；客户确认仍走 UE 端确认门，SE 端不可代拍 | `statement.confirm.proxy` |

## 6. 开票审核域（spec: erp-invoice-audit-queue；确认门）

> 权限列用 `alert.handle`：该 grant 在本端守的是「对一条 pending 的客户请求作处置」，开票审核
> 是同一动作类型。未新开 `invoice.audit`，避免同一语义出现两个权限码（既有先例：契约外对账
> 代理复用 `statement.confirm`）。状态唯一出口是内核确认门 `decide_event`，本服务不存状态。

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/se/invoice-requests` | `status`（缺省 `pending`；`all` 看全部）、`from`/`to`、分页 | 本人名下客户的开票申请：`requestId`/`customerId`/`title`/`taxId`/`orderId`/`statementId`/`submitted_at`/`status`；他人客户的申请不出现，也不可通过参数读到；拉不全时带完整性声明 | `alert.handle` |
| POST | `/erp/supply-chain/v1/se/invoice-requests/{requestId}/decide` | `decision: accepted/rejected`、`note`（**驳回必附**） | 拍板回执：经内核确认门写状态并留痕；已拍板再拍返回 `EVENT_STATE`（409，不静默覆盖）；他人客户与不存在同一条 404；内核不可达如实 501 + `planned_lane` | `alert.handle` |

## 7. AI 助手入口

复用底座 chat 面（`pages/chat/index`）与 WS 契约；经营问答/客户口径与页面同源；动作类输出经确认门。无独立 AI 端点。

## 8. 功能点覆盖对账

| 功能域（tasks 6.3 枚举） | 承载页面 | 状态 |
|---|---|---|
| 代客下单 | se-order-proxy / se-order-track / se-customer-detail | 覆盖 |
| CRM 线索拜访 | se-customer-list / se-lead-visit / se-customer-detail | 覆盖 |
| 预警处置（拍板席位移动呈现） | se-alert-inbox / se-alert-handle | 覆盖 |
| 开票审核（spec: erp-invoice-audit-queue，开票申请逐条拍板） | se-invoice-audit | 覆盖 |
| AI 助手入口（spec 附加） | se-ai-assistant | 覆盖 |

锚点蓝图 SE 17 功能点按上述 3 个功能域聚合核对：**3/3 域覆盖，0 域待一期裁剪**；逐点映射待锚点清单资产化后回填（挂账）。
