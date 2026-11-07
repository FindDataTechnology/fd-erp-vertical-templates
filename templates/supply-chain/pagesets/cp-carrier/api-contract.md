# CP 承运商页面集 · 后端 API 契约（pack: supply-chain-cp-carrier v0.1.0）

> pack 定义：`templates/supply-chain/pagesets/cp-carrier/manifest.json`。
> 对应 change `add-supply-chain-vertical` tasks 6.4（页面集）与 6.5（配套后端面）。
> **同源纪律：本契约全部端点与 MCP 工具面同源数据（同一内核与编排层），无平行取数路径。**
> 契约空间声明同 UE（`ue-commerce/api-contract.md` §0）：鉴权/数据域/错误形态/WS 复用/同源对位一致；本端 identity 映射承运商档案与在途运单。

## 0. 本端特有约定

- 外部执行方定位：CP 端是交付编排（运单下发→月台→提货→在途→签收→结单计费）的回流面；全部回执写入与中台运单同一状态机。
- 计费同源：运费单来自计费规则引擎（tasks 7.2 结算与资金组，事件驱动 + 日终快照），CP 端只读确认，不改计费口径。
- 采集纪律：定位/影像类字段均为可选采集，采集范围沿底座隐私纪律（提审与隐私声明复用底座经验）；采集项在实施时于隐私声明中列明。

## 1. 工作台

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/cp/workspace` | — | 待接运单/今日任务（月台+提货）/异常单计数卡 | 页面集授权 |

## 2. 运单域（功能点：运单接收；确认门）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/cp/waybills` | 状态/期间/分页 | 下发给本承运商的运单列表 | `waybill.read` |
| GET | `/erp/supply-chain/v1/cp/waybills/{waybillId}` | — | 运单明细（货/仓/窗期/收发信息） | `waybill.read` |
| POST | `/erp/supply-chain/v1/cp/waybills/{waybillId}/accept` | `decision: accepted/rejected`、`rejectedNote?` | 承运回执：确认门语义；拒绝回显原因；回执驱动交付编排改派 | `waybill.accept` |

## 3. 月台预约域（功能点：月台预约）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/cp/dock-slots` | `warehouseId`、日期 | 可约月台时段 | `dock.reserve` |
| POST | `/erp/supply-chain/v1/cp/waybills/{waybillId}/dock-reservation` | `slotId` | 预约回执；与仓库作业编排联动（改约/取消走同端点语义字段） | `dock.reserve` |

## 4. 提货域（功能点：提货打卡）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| POST | `/erp/supply-chain/v1/cp/waybills/{waybillId}/pickup-checkin` | `location?`、`imageRefs?`、`scanCode?` | 打卡回执；扫码提货复用底座 bind-scan 同一扫码实现（解析目标改为作业码）；回执驱动在途状态 | `pickup.checkin` |

## 5. 签收域（功能点：签收上报）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| POST | `/erp/supply-chain/v1/cp/waybills/{waybillId}/pod` | 签收影像/回单件、异常说明? | POD 回执：驱动运单结单与计费事件（计费规则引擎同源）；异常签收进处置车道 | `pod.report` |
| GET | `/erp/supply-chain/v1/cp/waybills/{waybillId}/events` | — | 运单事件流/轨迹；与客户侧全单物流视图同源 | `waybill.read` |

## 6. 运费对账域（确认门）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/cp/freight-statements` | 期间/状态 | 运费单列表 | `freight.statement.read` |
| GET | `/erp/supply-chain/v1/cp/freight-statements/{statementId}` | — | 运费单明细（计费项四层穿透溯源：规则/事件/单据/原始作业，tasks 7.2 同源） | `freight.statement.read` |
| POST | `/erp/supply-chain/v1/cp/freight-statements/{statementId}/confirm` | `decision: confirmed/disputed`、`disputeNote?` | 确认回执：确认门语义，超时/失败不假报成功；异议进处置车道 | `freight.statement.read` |

## 7. AI 助手入口

复用底座 chat 面（`pages/chat/index`）与 WS 契约；运单/任务问答与页面同源；动作类输出经确认门。无独立 AI 端点。

## 8. 功能点覆盖对账

| 功能域（tasks 6.4 枚举） | 承载页面 | 状态 |
|---|---|---|
| 运单接收 | cp-waybill-accept / cp-track | 覆盖 |
| 月台预约 | cp-dock-reserve | 覆盖 |
| 提货打卡 | cp-pickup-checkin | 覆盖 |
| 签收上报 | cp-pod-report | 覆盖 |
| 对账（运费单，tasks 7.2 关联） | cp-reconciliation | 覆盖 |
| AI 助手入口（spec 附加） | cp-ai-assistant | 覆盖 |

锚点蓝图 CP 13 功能点按上述 4 个功能域聚合核对：**4/4 域覆盖，0 域待一期裁剪**；逐点映射待锚点清单资产化后回填（挂账）。
