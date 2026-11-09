# 跨境电商行业线·环节供给清单 v1（1.1 最终版）

> 本清单是行业线「覆盖到哪」的对外唯一口径（spec: crossborder-vertical-data/全链条环节供给清单）。
> 状态词：**在产**=可查可订；**已点亮**=源自动运行中（见基线报表）；**本波在建**=静默合入待人工点亮；**未点亮**=单元在仓/待门；**不可得**=须商家授权（内圈闸 D1）。
> 数据实底：`docs/crossborder-source-baseline.md`（2026-11-03 运营库快照）；词表实底：`docs/crossborder-entity-vocab-gap.md`。

## 外圈（数据供给面）

| 环节 | 承载源/通道 | 状态 | 备注 |
|---|---|---|---|
| 政策合规 | fd-info cn-gov 四路（海关总署/商务部含厅局/税务/口岸） | **在产** | 试点舰队 10-26 真跑全链通（巡检 52 条）；主题词 9610/1210/9710/9810 |
| 贸易统计 | mofcom-data（进出口/服务贸易/利用外资三口径） | **已点亮** | 最新期 2026-08，月更 8 日跑 |
| 贸易统计·品类 | un-comtrade（HS6×国别流向） | **已点亮（种子级）** | 仅 1 组合 9 行；扩量=任务 2.3（免费档 500 次/日自节流） |
| 技贸壁垒 | wto-eping（TBT/SPS 通报流） | **已点亮** | 日更，通报至 10-30 |
| 关税/HS 税率 | tariff-hs-rates（新单元） | **本波在建** | 种子三码 854142/851762/610910 验收；撞验证码，待人工/OCR；mof 税则面在册未点亮另议 |
| 跨境电商零售统计 | customs-xborder-retail（新单元） | **本波在建** | 海关总署月度零售进出口（9610/1210 口径）；上游 JSL 防护 412/504，待人工开白名单/鉴权 |
| 物流·港口 | portwatch-imf | **已点亮** | 日更 3800 行；上游冻结窗 ~1 周（既有认知：冻更长属源特性） |
| 物流·景气 | chinawuliu-logistics | **已点亮** | LPI 月更，最新 2026-09=51.5% |
| 汇率结算 | pboc-monetary / pboc-inclusive / ecb-exr（fred 在册） | **已点亮** | 月/周频；CNY 面 |
| 采购成本 | mysteel/inmetals/worldsteel/usda/kitco | **已点亮（他线复用）** | 大宗价成本卡，跨境选品可直读 |

## 内圈（商家授权通道——D1 闸）

> 内圈状态以**能力矩阵**为唯一真源（`fd-erp-vertical-templates/templates/crossborder/capability-matrix.json`）。
> 状态词三态：**可得**（有证据的真实读取）/ **需一次接入**（通道可接但尚未接通，`unmapped`）/ **不提供**（确认拿不到，`unavailable`）。
> 行内锚点由 `fd-erp-vertical-templates capability-check` 机器校验，与矩阵不一致即红（2026-11-10 起）。

| 环节 | 通道 | 状态 | 备注 |
|---|---|---|---|
| 店铺经营·跨境（订单） | Shopify 经 `shopify_admin` 读面 <!-- capability:shopify.orders=unmapped --> | **需一次接入** | provider 动作在目录，但无真实店读取记录（真店 POC 未开，现仅合成 fixture） |
| 店铺经营·国内（订单） | 商家电商 ERP（旺店通/聚水潭类）<!-- capability:merchant_erp.orders=unmapped --> | **需一次接入** | 国内库存腿唯一来源，优先实现 |
| 平台直连（淘宝/天猫/京东） | 淘宝 TOP、京东开放平台 <!-- capability:taobao.orders=unmapped --> | **需一次接入** | 连接器目录 1570 家 provider 内无这三家（ADR 0002：先有客户再实现） |
| 结算面（payouts/账单） | 待补 `shopify_admin.list_payouts` 等 <!-- capability:shopify.settlements=unmapped --> | **需一次接入** | 上游 Shopify Admin GraphQL 有该面；现行 REST 结算直连将被撤下 |
| 库存面 | 同上 <!-- capability:shopify.inventory=unmapped --> | **需一次接入** | 三方对账缺此腿不成立；不得以「零差异」冒充已核对 |
| 拼多多 | 不立 provider <!-- capability:pinduoduo.orders=unavailable --> | **不提供** | 上游接口 schema 缺失；不声明绑定、不留槽位 |
| 选品热销（平台侧） | — | **不提供** | 无通道提供该类动作；不属能力矩阵四条交易流，故不占锚点 |
| 售后口碑（平台侧） | — | **不提供** | 同上 |

## 智能作业面（五层的上三层）

| 层 | 项 | 状态 |
|---|---|---|
| 技能 | 政策盯梢三技能（试点在册）+ 值守四技能（crossborder-presets） | 前者**在产**；后者**已落盘**（4 个 SKILL.md 落盘 finddata-skills/crossborder-presets/） |
| Agents | 监视/合成/产出 3 岗（试点 pack）+ 值守四岗 pack | 前者**在产**；后者**已落盘**（fleet-packs/crossborder/crossborder-watch-roles/manifest.json + 4 短技能，校验器通过） |
| Teams | 政策盯梢集群 1 舰 | **在产**；物流盯梢/合规分流/行业监测=波 3 蓝图（5.2/5.3） |

## 实体词表域（E 报告落地）

| 类目 | 现有词表覆盖 | 注册建议 | 状态 |
|---|---|---|---|
| 监管代码 (9610/1210/9710/9810) | ❌ 零覆盖 | `customs_regime` | **已注册 4 条**（id 300953-300956，定义逐字取试点在册目录） |
| HS 品类/编码 | ❌ 零覆盖 | `hs_code` | **已注册 1 条**（854142，仅落库数据支撑者；扩量随 2.3） |
| 口岸 | ❌ 零覆盖 | `port` | **暂缓**（portwatch 落库行是近海石油码头非跨境口岸；待海关口岸代码表） |
| 跨境电商平台 | ❌ 零覆盖 | `platform` | **已注册 4 条**（amazon/tiktok_shop[首发]/temu/shein，供 D1 授权连接器通道） |
| 汇率对 | ❌ 零覆盖 | `currency_pair` | **已注册 1 条**（EURCNY，仅落库数据支撑者；其余待源） |
| 政策条目 | ❌ 零覆盖 | `policy` | **暂缓**（天然随政策巡检增量注册，试点舰在产） |

> 词表实底：生产库 55 个 industry 实体（申万31+GICS10+财报14），**零覆盖跨境六类目**；dev SQLite 为空，**核对须连生产 PG**。
> 11-04 批次注册实底：4 类共 **10 实体**落生产 `entities`（customs_regime 4 / platform 4 / hs_code 1 / currency_pair 1），抽查逐条带 source+definition 可溯源；port/policy 两类按实底如实暂缓（详见 `docs/crossborder-entity-vocab-gap.md` §5）。

## 交付面

| 项 | 状态 |
|---|---|
| SaaS（柏讯控制台+看板） | 波 1 数据落目后成域（2.4/2.5） |
| 私有化整机包（vertical-appliance-delivery） | 波 4（6.1-6.4），跨境线做首发租户；载体规格见 spec |

---

## 发布状态（与台账一致，2026-11-10）

> 口径来源：`fd-erp-vertical-templates/releases/`——`ledger.jsonl`（版本与摘要）、
> `READINESS.md`（上架就绪）、`ENTRY-MATRIX.md`（对外入口）。以下为同一时点的摘录，
> 台账一变这里就要跟着改。

| 项 | 状态 | 说明 |
|---|---|---|
| 连接器 pack v1.5.3 | **draft（待上架）** | 已过发布门（能力面/清单/摘要/版本单调），四语齐全；未在市场发布 |
| 科目映射 pack v1.1.2 | **draft（待上架）** | 同上；映射只影响呈现，替换不改账 |
| 对账舰队模板 v1.1.2 | **draft（待上架）** | 同上；巡检/核对/拍板三岗 |
| 跨境与国内五张看板 | **built（产物就绪，未上线）** | 本地 spec+HTML 齐，尚未部署到站点；未上线不列入任何试用入口 |
| 私有化整机包 | v1.2.0 出包 VERIFY PASS | 含能力矩阵；真机验收待环境 |

**能力实底不变**：内圈 0 格可得，拼多多 4 格「不提供」，其余 24 格「需一次接入」。
对外表述以 `capability-matrix.json` 为准——上表任何「待上架」都不得读作「已支持」。

---

## 后续波次锚点（OpenSpec tasks 对齐）

| 波次 | 关键任务 | 备注 |
|------|----------|------|
| **波 1** | 2.3 un-comtrade 种子组合扩量（免费档 500/天） | 2.4 实体注册 6 批次落脚本 2.5 看板 2-3 张落 finalize 闸 |
| **波 2** | 3.1 跨境数据集工具面声明（allowTools⊆在册集） | 3.2 商家授权连接器通道占位（首发=TikTok Shop，11-03 裁决） |
| **波 3** | 4.3 pack 上架（作者=在册 creators、限流窗口、五语覆盖） | 5.1 政策盯梢舰复跑真跑 5.2/5.3 物流/合规/行业监测蓝图→铸造→首轮真跑 |
| **波 4** | 6.1-6.4 私有化载体泛化（构建期守卫/三档分层/增量通道/断公网真机验收） | 跨境线做首发租户真机验收 |
| **收口** | 7.1 售前话术同步（BossAI「不存在」→「外圈在产+内圈授权通道」） | 7.2 `openspec validate --strict` 零错误 → archive |
