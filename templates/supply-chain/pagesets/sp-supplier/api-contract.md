# SP 供应商页面集 · 后端 API 契约（pack: supply-chain-sp-supplier v0.1.0）

> pack 定义：`templates/supply-chain/pagesets/sp-supplier/manifest.json`。
> 对应 change `add-supply-chain-vertical` tasks 6.4（页面集）与 6.5（配套后端面）。
> **同源纪律：本契约全部端点与 MCP 工具面同源数据（同一内核与编排层），无平行取数路径。**
> 契约空间声明同 UE（`ue-commerce/api-contract.md` §0）：鉴权/数据域/错误形态/WS 复用/同源对位一致；本端 identity 映射供应商档案，数据域为「本租户推送给本供应商档案」的数据。

## 0. 本端特有约定

- 外部执行方定位：SP 端是采购编排（需求汇总→采购订单→推送供应商→到货预告→入库任务）的回流面；接单/发货反馈的回执写入与采购单同一状态机，不建第二状态机。
- 资金红线：本端**零支付操作面**——对外付款走金蝶/持牌通道（design D4），SP 端只有只读结算查询。
- 发票上传面接受影像 + 结构化要素双轨；上传件进入中台文件服务（沿用底座文件上传契约），不落连接器凭证面。

## 1. 工作台

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/sp/workspace` | — | 待接单/待发货/待对账/已传票待核计数卡 | 页面集授权 |

## 2. 接单域（功能点：接单；确认门）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/sp/purchase-orders` | 状态/期间/分页 | 推送给本供应商的采购订单列表 | `po.read` |
| GET | `/erp/supply-chain/v1/sp/purchase-orders/{poId}` | — | 订单明细（行/交期/收货仓） | `po.read` |
| POST | `/erp/supply-chain/v1/sp/purchase-orders/{poId}/accept` | `decision: accepted/rejected`、`rejectedNote?` | 接单回执：确认门语义；拒绝回显原因并可发起协商；回执驱动到货预告编排 | `po.accept` |

## 3. 发货反馈域（功能点：发货反馈）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| POST | `/erp/supply-chain/v1/sp/purchase-orders/{poId}/delivery-feedback` | 行级发货明细、物流商/单号、凭证影像 | 回执：与入库任务联动（编排面同源），状态可解释 | `delivery.feedback` |
| GET | `/erp/supply-chain/v1/sp/arrival-notices` | 状态/期间 | 到货预告列表 | `delivery.feedback` |
| POST | `/erp/supply-chain/v1/sp/arrival-notices` | 订单引用、到货时间、明细 | 到货预告（可由发货反馈自动生成，本端点承载补录） | `delivery.feedback` |

## 4. 对账域（功能点：对账；确认门）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/sp/statements` | 期间/状态 | 应付对账单列表 | `statement.read` |
| GET | `/erp/supply-chain/v1/sp/statements/{statementId}` | — | 明细（单据级勾对）；与金蝶往来面同源（金蝶 provider 拉取、账本内核计算） | `statement.read` |
| POST | `/erp/supply-chain/v1/sp/statements/{statementId}/confirm` | `decision: confirmed/disputed`、`disputeNote?` | 确认回执：确认门语义，超时/失败不假报成功；异议进处置车道 | `statement.read` |

## 5. 发票域（功能点：发票上传）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| POST | `/erp/supply-chain/v1/sp/invoices` | 对账单引用、发票要素（代码/号码/日期/金额/税额）、影像件 | 受理回执：进查验/勾兑车道；通过后经金蝶连接器回写（contract-only，金蝶 provider 通用 action 覆盖面见 `docs/connector-onboarding-supply-chain.md` §1.2） | `invoice.upload` |
| GET | `/erp/supply-chain/v1/sp/invoices` | 状态 | 上传历史与核验进度 | `invoice.upload` |

## 6. 结算查询域（只读）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/sp/settlements` | 期间/状态 | 结算进度（只读）；不提供任何支付/账户操作 | `statement.read` |

## 7. AI 助手入口

复用底座 chat 面（`pages/chat/index`）与 WS 契约；单据/对账问答与页面同源；动作类输出经确认门。无独立 AI 端点。

## 8. 功能点覆盖对账

| 功能域（tasks 6.4 枚举） | 承载页面 | 状态 |
|---|---|---|
| 接单 | sp-order-accept | 覆盖 |
| 发货反馈 | sp-delivery-feedback / sp-arrival-notice | 覆盖 |
| 对账 | sp-reconciliation / sp-payment-view | 覆盖 |
| 发票上传 | sp-invoice-upload | 覆盖 |
| AI 助手入口（spec 附加） | sp-ai-assistant | 覆盖 |

锚点蓝图 SP 14 功能点按上述 4 个功能域聚合核对：**4/4 域覆盖，0 域待一期裁剪**；逐点映射待锚点清单资产化后回填（挂账）。
