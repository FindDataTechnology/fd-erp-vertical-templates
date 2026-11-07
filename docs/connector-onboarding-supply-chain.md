# 供应链垂直 · 萬星连接器立项材料（金蝶云星空 / 支付渠道 / 客户 WMS）

> 对应 OpenSpec change `add-supply-chain-vertical` tasks 3.1/3.2/3.3 的「评估与立项」面。
> 核验日期：2026-11-07。核验对象：`open-connector-mt` 仓库 `catalog/apps-index.json`（version 1，providers 计 1568）与 `catalog/apps/*.json`。核验过程只读。
> 客户中性纪律：本文不出现具体客户名，统称「锚点客户」。

## 总纪律（三节共用）

- 载体唯一（design D2）：全部外部系统对接经萬星·连接器（open-connector-mt，多租户、凭证不出库），ERP 线零自建连接器框架。
- `open-connector-mt` 有独立 openspec 根（`openspec/specs/`：multi-tenancy / tenant-auth / tenant-console / tenant-mcp）；provider 新增按其仓库流程立项，本仓只立契约与需求。
- 仓库硬约束摘录（`open-connector-mt/openspec/config.yaml`）：「Never modify src/providers/** (the 1.38M-LOC catalog belongs to upstream)」；provider 定义源在 `src/providers/<service>/definition.ts`，经 `npm run generate:catalog` 生成 `catalog/apps/*.json`（`CONTRIBUTING.md` §Adding Providers）。**新增 provider 与该硬约束存在张力**（新增即触碰 `src/providers/**` 路径面），立项时须在 open-connector-mt 侧裁定载体：上游新增（随 upstream 双周合并节奏）或分叉侧例外（design 规则要求 proposal 写明哪些 upstream 文件被触碰、为何不可避免）。

---

## 一、金蝶云星空 provider

### 1.1 目录现状核验（如实）

- Provider 文件：`catalog/apps/kingdee.json`（index 中 `file: kingdee.json`，唯一金蝶 provider，无 kingdee_k3/kingdee_cosmic 等变体）。
- displayName `Kingdee`，categories `["Finance"]`，homepageUrl `https://www.kingdee.com/cn`。
- 凭据形态：authTypes `["custom_credential"]`，6 字段——`baseUrl`（企业应用 URL，placeholder `https://erp.example.com/k3cloud/`）、`dataCenterId`（账套 ID）、`username`（集成用户）、`applicationId`（第三方应用 ID）、`applicationSecret`（secret，密码态）、`localeId`（可选，默认 2052 简体中文）。全部字段标注为在金蝶云·星空企业端配置第三方应用并授权集成用户。
- 服务端实现（`src/providers/kingdee/runtime.ts`）走星空 Web API：`Kingdee.BOS.WebApi.ServicesStub.AuthService.LoginByAppSecret.common.kdsvc` 登录，会话态（SessionId/KDSVCSessionId）。
- Action 面：**12 个 action，全部是 formId 驱动的通用单据操作**，无业务域专用 action：

| action id | operationType | 说明 |
|---|---|---|
| `kingdee.query_records` | read | 按字段/过滤/行偏移查询单据 |
| `kingdee.get_record` | read | 按内码/编号查看单据 |
| `kingdee.query_report` | read | 按表单过滤查报表 |
| `kingdee.save_record` | destructive | 保存单据（含更新） |
| `kingdee.batch_save_records` | destructive | 批量保存（保留部分成功） |
| `kingdee.draft_record` | destructive | 保存草稿 |
| `kingdee.submit_records` | write | 提交审批 |
| `kingdee.audit_records` | write | 审核 |
| `kingdee.unaudit_records` | destructive | 反审核 |
| `kingdee.push_records` | write | 按转换规则下推（源单→目标单） |
| `kingdee.allocate_records` | write | 主数据分配到目标组织 |
| `kingdee.delete_records` | destructive | 按内码/编号删除 |

- Triggers 面：`src/providers/kingdee/` 无 trigger 定义（全仓 20 个 trigger 定义分布于 airtable/feishu_app_bot/github/gitlab/gmail/googlecalendar/googledrive/googlesheets/linear/notion/one_drive/outlook/shopify/slack/stripe/telegram/woocommerce/zendesk/triggercmd），**金蝶无 webhook/轮询 trigger**。

### 1.2 覆盖面对位（tasks 3.1 需求：凭证推送 / 往来 / 发票面 / 日终双边对账）

- **凭证推送**：`push_records`（下推）+ `save_record`/`submit_records`/`audit_records` 可覆盖「源单→目标单→凭证」链路，但前提是星空侧预置了对应单据转换规则；provider 无「凭证模板/自动生成凭证」专用 action。需按锚点客户单据流逐一验证转换规则是否在客户星空实例中存在。
- **往来（应收/应付/对账）**：应收单/应付单等往来单据经 `query_records`/`save_record`/`push_records`+审核链覆盖；`query_report` 可查表单报表（对账单类报表可用性取决于客户实例报表注册）。日终双边对账（中台台账 ↔ 星空）的数据面可用 `query_records`/`query_report` 拉取，**对账计算在中台侧做**（账本内核 `run_reconciliation` 同源），非连接器职责。
- **发票面**：销售/采购发票作为单据经通用 action 保存/下推/审核可覆盖「发票回写」；**开票（税控/发票云直连）不在 provider action 面内**，如锚点客户需要直连开票，属「需上游新增 action」。
- **结论**：通用操作面对 tasks 3.1 的凭证推送/往来/发票回写**可用但不保证够用**——缺口不在 action 数量而在业务语义（转换规则、凭证模板、开票直连），立项时按锚点实例逐一验证，缺什么在 open-connector-mt 侧提「需上游新增 action」。

### 1.3 立项要点

1. 接通验证（本仓 tasks 3.1）：以第三方应用凭证接通一个验证实例，走 `query_records`→`save_record`→`submit_records`→`audit_records`→`push_records` 全链；凭证经 custom_credential 注入，验证「凭证不出库」（连接器服务端持有，移动端/编排层零接触）。
2. 需上游新增 action 的候选清单（按需提出，不预先虚构）：凭证模板自动生成、开票直连、银企直连（若锚点客户要）。
3. trigger 缺口：星空事件（单据审核完成等）无 webhook trigger；日终对账用轮询（cron 拉取）即可满足，不需要新增 trigger；若后续要事件驱动，按 open-connector-mt trigger 机制（`docs/runtime-api.md` §Provider Triggers，webhook 走 subscription/receive/reconcile）另立。

### 1.4 验收判据

- 用锚点客户的账套（或等价验证实例）完成一次端到端：查一张销售订单（query_records）→ 下推生成下游单据（push_records）→ 提交并审核（submit/audit）→ 报表查询对账数据源（query_report），全程经连接器、凭证不出库、操作留痕可查。
- 反审核/删除等 destructive action 在策略面被 `allowedActions`/`blockedActions` 正确约束（runtime token 策略，见 `docs/runtime-api.md`）。

### 1.5 跨仓流程注记

- open-connector-mt 独立 openspec 根；本节立项需求（接通验证 + 可能的 action 新增）在 open-connector-mt 侧开 change，按其 rules：proposal 含 Non-goals、说明触碰哪些 upstream 文件及为何不可避免；`TENANCY=off` 行为与 upstream 字节兼容。
- 目录里的 provider 文件（`catalog/apps/*.json`）是 `npm run generate:catalog` 的生成物，**不要手改生成文件**；改动走 `src/providers/kingdee/definition.ts`。

---

## 二、支付渠道 provider（商户侧，只读 + 回调记录）

### 2.1 目录现状核验（如实）

对 design.md Open Questions「微信支付/支付宝/银联商户侧是否已有目录 provider」的直接回答：**没有**。

- 逐文件与 index 双重检索 `wechat pay / weixin pay / alipay / unionpay / 银联 / 微信支付`：**目录无微信支付、无支付宝、无银联 provider**。命中的 `weixin_*` 均非支付：`weixin_official_account.json`（公众号）、`weixin_bot.json`、`wecom_bot.json`/`wecom_mcp.json`（企业微信）、`weixin_store.json`（微信小店，E-commerce；32 actions 为商品/订单/售后/运费模板面，其 `get_order` 读得到订单 payment 信息，但它是电商平台订单面，不是微信支付商户平台的支付通道面）。
- 已有的国际/线上 PSP provider（如实列出，均非国内三大渠道）：

| provider 文件 | displayName | auth | action 数 | read/write/destructive | 备注 |
|---|---|---|---|---|---|
| `stripe.json` | Stripe | api_key | 18 | 10/5/3 | 客户/商品/价格 CRUD+检索；**另有 `stripe.on_event` webhook trigger（Account Event，`src/providers/stripe/trigger-on-event.definition.ts`）** |
| `paypal.json` | PayPal | custom_credential(clientId,clientSecret,environment) | 14 | 6/5/3 | |
| `square.json` | Square | api_key | 6 | 4/2/0 | |
| `adyen.json` | Adyen | api_key | 6 | 6/0/0 | 纯读 |
| `checkout_com.json` | Checkout.com | api_key | 4 | 1/2/1 | |
| `paystack.json` | Paystack | api_key | 8 | 6/2/0 | |
| `razorpay.json` | Razorpay | api_key | 6 | 4/1/1 | |
| `payrexx.json` | Payrexx | api_key | 5 | 4/1/0 | |
| `payhip.json` | Payhip | api_key | 9 | 3/4/2 | |
| `btcpay_server.json` | BTCPay Server | api_key | 7 | 4/3/0 | |

- 回调/回调记录的机制面：连接器运行时支持 webhook 订阅（`docs/runtime-api.md` §Provider Triggers：subscription 建/收/对账/取消，签名校验、64KiB rawBody、回调记录归属 runtime token+connection），即「回调记录」有平台机制可落，但**前提是对应 provider 有 trigger 定义**——PSP 中仅 Stripe 有。

### 2.2 与 tasks 3.2 的对位（只读流水 + 回调记录 + 日终渠道对账差异清单）

- 需求面是微信支付/支付宝/银联商户侧：**目录无对应 provider，需新增**（立项结论，非可选项）。
- 新增面最小集：微信支付（商户平台 APIv3，证书/平台证书 + APIv3 密钥，账单 downloadbill/report query），支付宝（开放平台，商户密钥/公钥证书，对账单下载），银联（若锚点客户有）。均为**只读 + 回调记录**纪律：不发起支付指令、不碰退款写操作（资金红线 D4：在线支付=只读+回调记录+日终差异清单，对外付款走金蝶/持牌通道）。
- 若仅做国际卡收单场景（锚点客户无此需求迹象），Stripe/PayPal 现状可用——如实记录，不作为国内渠道缺位的替代。

### 2.3 立项要点

1. 在 open-connector-mt 按其 SDK/definition 流程新增 `wechatpay`/`alipay`（命名以其仓库规范为准）provider：auth 用 custom_credential（商户号/商户私钥证书/平台证书/APIv3 密钥等），action 首版限 read（账单下载、交易查询）+ trigger（支付结果回调，走既有 subscription/receive 机制做回调记录）。
2. 回调记录落库面属连接器运行时既有能力（subscription + receive），ERP 侧消费经同源数据面，不建平行取数。
3. 日终渠道对账差异清单：渠道账单（provider read）↔ 中台台账（账本内核），计算在中台，连接器只供数据。

### 2.4 验收判据

- 微信支付：以商户号接通，拉取一天账单（read），一笔真实支付的回调经 trigger subscription 落记录、签名校验通过；全程无任何写操作面被调用。
- 支付宝：同构（账单下载 + 回调记录）。
- 差异清单：渠道账单 vs 中台台账对一次日终，差异条目可解释。

### 2.5 跨仓流程注记

- provider 新增在 open-connector-mt 侧立项（其 openspec 流程，proposal 需声明触碰 `src/providers/**` 新路径的理由——新增文件而非改 upstream 文件，属硬约束的解释空间，须其仓裁定）。
- 三大渠道的官方 API 侧资质（商户号、证书、回调域名白名单）由锚点客户/商户侧提供，ERP 线不代持。

---

## 三、客户 WMS provider（无目录 provider，按连接器 SDK 新增）

### 3.1 目录现状核验（如实）

- 按文件名与内容双检索 `wms / warehouse management / warehouse / fulfillment / 3pl`：**目录无任何 WMS 类 provider**。无 ShipHero、Peoplevox、Logiwa、Infoplus、3PL Central、Fishbowl、通天晓、科箭、C-WMS 等国内外 WMS 品牌任何一种。
- 最接近的仓储/物流面 provider（均不是 WMS，如实区分）：
  - `ship_bob.json`（ShipBob，3PL 履约）：api_key，5 actions 全 read（inventory levels / channels / locations / products）——只读库存/商品面，无出入库作业指令面，不是通用 WMS。
  - `aftership.json`（9 actions）、`shippo.json`（8 actions）、`17track.json`（4 actions）：包裹追踪/面单地址面，属物流轨迹不是仓储作业。
- 结论：**客户 WMS（品牌与接口形态待锚点客户确认，design.md Open Questions 挂账中）无目录 provider 可用，必须按连接器 SDK 新增**——与 change tasks 3.3 的判断一致，核验予以坐实。

### 3.2 立项要点

1. 前置依赖：锚点客户 WMS 品牌与接口形态（HTTP API/中间表/文件交换？有无 webhook？）未定——**provider 新增工作量无法在形态确认前报价**，本节只立边界。
2. 新增边界（design D2）：载体是萬星连接器，资产归连接器项目（open-connector-mt），ERP 线出需求与验收判据，不自建对接框架。
3. 需求面（来自 erp-vertical-supply-chain spec）：出入库事件回传（入库任务完成/出库指令执行）、库存单向同步读（中台→WMS 单向写、WMS→中台事件回传；编排层 MUST NOT 直接写外部 WMS 台账）、账实差异数据源。首版 action 形态建议：read（库存查询/单据状态）+ write（出入库指令投递，仅经白名单）+ trigger（作业事件回传，若 WMS 支持 webhook；否则 poll）。
4. 接口形态是中间表/文件交换时，先确认连接器 SDK 对该形态的支持面（现行 1568 provider 以 HTTP API 为主），不支持则升级为 SDK 能力扩展项，仍在 open-connector-mt 侧立项。

### 3.3 验收判据

- 形态确认后补全本节（挂账）：以客户 WMS 测试环境完成「出库指令投递 → 作业完成事件回传 → 库存读对平」一轮闭环，账实日终一致（差异清单驱动）。

### 3.4 跨仓流程注记

- 同上：open-connector-mt 独立 openspec 根立项；proposal 按其 rules 写 Non-goals 与 upstream 触碰声明；生成目录文件不手改。
- 本仓（fd-erp-vertical-templates）侧只维护「需求 + 验收判据 + 状态」的注记，open-connector-mt 侧 change 落地后回填 3.3。

---

## 状态汇总（跨仓工件核对用，tasks 8.4 引用）

| 项 | 目录现状 | 本 change 所需 | 结论 |
|---|---|---|---|
| 金蝶云星空 | `kingdee.json` 在目录，12 通用 action，K3Cloud Web API，无 trigger | 接通验证（3.1）；专用 action 按需上游新增 | 可启动接通验证 |
| 支付渠道（微信/支付宝/银联） | **无**；国际 PSP 有 10 家（仅 Stripe 有 webhook trigger） | 商户侧只读 provider + 回调记录（3.2） | 需在 open-connector-mt 新增 |
| 客户 WMS | **无**任何 WMS provider | 按连接器 SDK 新增（3.3） | 需新增；工作量待客户接口形态确认 |
