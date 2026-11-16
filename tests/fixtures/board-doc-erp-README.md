<!-- 测试夹具：fd-erp/docs/README.md（原 docs/erp/README.md）的快照副本（optimize-erp-solution 4.x）。
     CI 只有本仓 checkout，机制用例（生成块漂移/声明扫描）读这份夹具；
     「生产材料零无证据声明」那条用真文档，兄弟仓或多仓工作区在位时才跑。
     刷新：scripts/claim_gate.py render 之后 cp 过去。 -->

# fd-erp/docs — ERP 板块章程与如实口径

> OpenSpec change：`add-erp-domain`（spec：`erp-board`）task 7.3「如实口径落档」。
> 本文件是 ERP 板块对内/对外**文字口径的单一出处**，与本次 apply 的命名与守界终裁
> （`/tmp/erp-kernel-contract.md`）及 `openspec/changes/add-erp-domain/specs/erp-board/spec.md`
> 一致。官网呈现见：
> `fd-official-web/src/components/LineTabs.astro`（板块 tab）、
> `fd-official-web/src/components/ErpLanding.astro`（双语落地页）、
> `fd-official-web/src/pages/erp.astro` 与 `fd-official-web/src/pages/zh/erp.astro`（路由）、
> `fd-official-web/src/pages/products.astro` 与 `fd-official-web/src/pages/zh/products.astro`（板块入口卡）。

## 1. 板块定位与命名（章程）

- ERP 是**板块**，不是第六条产品线。它位于「一十百千万」ladder 之外，与营销/客服台板块同级（先例见 `openspec/specs/crm-board/spec.md`）。
- 板块名 **ERP**，**双语同形**：EN=ERP / 中文=ERP。板块标签 **「板块 / Board」**。
- 官网板块位：tab 用非 ladder 线标「账」，带板块标签，链接到板块落地页 `/erp`（中文 `/zh/erp`）；不使用 `products/<line>` 路由。
- **五线不变**：`PRODUCT_LINES` / `LADDER_CHARS` / `LADDER_NUMERALS` 是公共契约（`fd-official-web/src/content.config.ts`），ERP MUST NOT 成为第六线；五线命名与 slug 一律不改。本 change 未改动 `content.config.ts`。

## 2. 组合形态

- ERP 以 **「集群 + 供给包」组合** 交付：账本内核 pack + 行业舰队 + 连接器 pack + 报表 preset；**不是单体 ERP 应用 / 表单式 UI**。
- 行业差异全部上移为行业模板资产（连接器 pack + 舰队蓝图 + 科目映射 + 报表 preset），账本内核保持行业无关。
- 账本内核可如实陈述的真实能力：复式事件账；agent 拟稿经确认门才成为事实；外部系统事实经导入车道按来源权威直接成为事实；两者都按人工确认的过账规则派生分录，缺规则即挂待补、不猜科目；借贷必平由数据库约束强制；已落账分录 append-only；余额由分录确定性重算、不设直写余额字段；`stable_id` 幂等去重；多币种原币 + 本位币折算（汇率 + `as_of` + 出处，无出处汇率拒绝）；对账引擎只读滩头、四类差异含双侧原始引用、对外部系统零写入；租户列隔离 + 查询闸。

### 2.1 子模块全覆盖四态（矩阵派生，勿手改）

「全覆盖」指**决策空间**全覆盖：ERP 子模块全集里每一格都恰好一态、一据、一理由，使对外回答只出现四种答案，不存在没有判据的「暂未支持」。它并不表示内核里逐个实现了这些模块——形态仍是「集群 + 供给包」。

本节由 `fd-erp/shared/coverage-matrix.json` 生成（`scripts/claim_gate.py render`），判据是 `fd-erp/tools/check_coverage_matrix.py`；块外再手写一份四态表会被声明门判红。

<!-- claim-gate:begin:coverage-matrix -->
**子模块全覆盖四态（矩阵派生，勿手改）**：唯一出处 `fd-erp/shared/coverage-matrix.json`（21 格，2026-11-11）。
- `delivered` 已实现 9 格：总账、应收应付、多币种与汇率、对账、计费、信用额度、库存（数量面）、发票、审批流
- `template_configured` 模板可配 4 格：销售与订单、采购、仓储与物流、主数据
- `needs_onboarding` 需一次接入 4 格：成本核算、预算、固定资产、合并报表
- `not_doing` 明确不做 4 格：税务申报、资金与出纳、生产制造、人力资源

逐格口径（引用任一格时，该格边界句必须原样随之出现）：
- **总账** `delivered`——边界：复式事件账：借贷必平由 DB 约束强制、余额确定性重算、已落账 append-only、更正只走冲正。分录由事件经版本化过账规则产出，不是原始入口。
- **应收应付** `delivered`——边界：科目级应收应付与余额由分录重算；信用占用从 1300 应收余额派生。未做账龄分析、坏账计提与催收流程。
- **多币种与汇率** `delivered`——边界：分录记原币 + 本位币折算，汇率带 as_of 与出处；无出处汇率由 DB 约束拒绝。汇率事实做种走 fx_facts（可复用既有观测流）。
- **对账** `delivered`——边界：只读滩头：两源流水逐笔对齐，四类差异（缺失/多余/金额不符/状态不符）各带双侧原始引用，对外部系统零写入。可指向外部两源，也可指向内部「事件流 vs 分录流」。
- **计费** `delivered`——边界：规则版本化 + 人工审批上线 + 冲突拦截 + 事件/期间快照计费（幂等可重算）+ 红冲留痕 + 四层溯源 + 无出处费率拒绝。落账走 billing-engine 机器车道 proposal，不绕过确认门。
- **信用额度** `delivered`——边界：available = limit − 1300 应收余额（分录重算）− active 占用；占用为备忘性质不写复式分录；额度不足带明细拒绝。
- **销售与订单** `template_configured`——边界：订单与销售面在行业模板的页面集与端点里（UE 商城 23/23、SE 业务员台 10/15）；内核只记 order_placed 事件与据此产生的金额事实，不实现行业订单规则。
- **采购** `template_configured`——边界：采购单与到货通知在 SP 供应商面（实现 7/13，其余如实 501）。到货落 goods_received 事件后可投影库存并可过账。
- **库存（数量面）** `delivered`——边界：数量面由事件 + 对象投影回答：数量/单位/批次/序列号/仓库/在途均可表达，余额可全量重算并带水位；REST 面经 `GET /sp/inventory` 只读暴露该余额（按供货范围收口，不是跨供应商全表）。上游 WMS 读取仍需一次接入（provider 缺口另记，不在本矩阵）。
- **仓储与物流** `template_configured`——边界：运单、月台预约、提货打卡、签收上报在 CP 承运商面（实现 8/12，其余如实 501）。仓储作业（库位/拣货/盘点路径）不在内核。
- **发票** `delivered`——边界：只记发票类事件（invoice_requested）并参与对账；票据本体由客户既有合规渠道处理。不自研开票、不代开票据——这是边界，不是缺口。
- **税务申报** `not_doing`——边界：不自研税务申报。账内可记录发票类事件并参与对账，申报本体走客户既有渠道。｜理由：税务申报需与税局/电子发票合规路径绑定，自研即触碰代开与申报责任；在中国电子发票合规路径确定前不作为能力提供｜裁决来源：openspec/specs/erp-board/spec.md#开票税务能力挂账如实守界｜复核窗口：2027-06-30
- **资金与出纳** `not_doing`——边界：只产出对账结果与核对指令；不提供资金池、代收代付、分账执行。资金归集与分账走客户自选的持牌通道。｜理由：二清红线：平台不得触碰资金归集与分账执行｜裁决来源：openspec/specs/erp-board/spec.md#资金合规红线｜复核窗口：2027-06-30
- **生产制造** `not_doing`——边界：不提供 BOM、工艺路线、工单、MRP 运算。当前客群（跨境电商/餐饮连锁/供应链）无此诉求。｜理由：生产制造需要 BOM/工艺路线/产能一整套对象与行业知识，且与当前三个行业模板的客群不重叠；无锚点客户前不做｜裁决来源：user-decision 2026-11-11（核心条线评审：ERP 全覆盖口径定案）｜复核窗口：2027-06-30
- **成本核算** `needs_onboarding`——边界：依赖库存数量面与估值口径；数量面已交付，成本方法（移动加权/先进先出/批次实际成本）需按客户口径落地。｜需客户提供：需要客户确认成本方法口径（加权/先进先出/批次实际）与首个真实账期的期初库存；口径不同则过账规则不同
- **预算** `needs_onboarding`——边界：预算在事件内核下是「预算对象 + 期间投影」，无需新内核能力；缺的是客户预算科目体系与口径。｜需客户提供：需要客户的预算科目体系与考核维度（部门/项目/渠道）；无口径时做出来的是空表
- **固定资产** `needs_onboarding`——边界：折旧是时间驱动事件，事件内核可承载；折旧政策（年限/残值/方法）只能由客户给。｜需客户提供：需要客户固定资产台账与折旧政策；且需确认是否走客户既有资产系统（若是则本板块只读对账）
- **合并报表** `needs_onboarding`——边界：多币种与租户隔离已具备，合并需要多主体与内部交易抵销口径；内核现在是一进程一租户。｜需客户提供：需要「多主体归属」口径与内部交易抵销规则；现内核一进程一租户，多主体需先定主体模型
- **审批流** `delivered`——边界：已有一等原语：拟落账的 proposal 确认门、拟稿事件的 decide 门、计费规则与过账规则的审批上线。多级/会签/条件路由的通用审批引擎不在本板块。
- **主数据** `template_configured`——边界：通用科目表在 universal_accounts；行业科目映射在每个行业模板的 account-mapping；对象身份在 objects（sku/客户/供应商/仓库）。跨系统的物料主数据同步仍走连接器。
- **人力资源** `not_doing`——边界：不提供人事、考勤、薪酬。与 ERP 板块「经营账 + 对账」的定位无关，且薪酬涉及个人数据与合规另一套边界。｜理由：HR 是独立产品域（个人数据、考勤、薪酬合规），与经营账内核无共用原语；纳入会稀释板块边界｜裁决来源：user-decision 2026-11-11（核心条线评审：ERP 全覆盖口径定案）｜复核窗口：2027-06-30
<!-- claim-gate:end:coverage-matrix -->


## 3. 执行面采纳边界

- 经营动作的执行（发货、支付、补货、开票等）**仅经采纳的外部 MCP server** 完成；平台凭证由租户自带（BYOK）、经环境变量注入，**零落库零日志**。
- 账本内核**只做记录、对账与核对指令层**，不直接驱动任何资金流或对外资金指令。

## 4. 二清 / 开票守界文案（可直接引用的对外口径）

> **资金（二清红线）**：平台只产出对账结果与核对指令，不提供资金池、代收代付或分账执行；资金归集与分账须走客户自选的持牌通道。
>
> **开票 / 税务申报**：不自研、当前不提供（挂账）。账内可记录发票类事件并参与对账，票据本体由客户既有合规渠道处理；平台不伪造、不代开票据。
>
> **执行面**：经营动作仅经采纳的外部 MCP server 完成，凭证 BYOK 注入、零落库零日志；账本内核不驱动资金流。
>
> **双客群租户隔离**：同时服务寻数自用与外部商家/泛企业客户，经营数据按租户（cell）隔离，任一客户租户不能读取其他租户的账、单据或报表。

- 以上为 **spec 硬约束**（`erp-board` 的「资金合规红线」「开票税务能力挂账如实守界」「执行面仅采纳外部 MCP」「双客群租户隔离」），非口头承诺。
- 以下清单不再手写：由 `fd-erp-vertical-templates/claims/claims.json` 与 `publish-record.json` 派生，`fd-erp-vertical-templates/scripts/claim_gate.py` 守（写在标记块外即判红）。

<!-- claim-gate:begin:not-claimable -->
**不可宣称（数据派生，勿手改）**：以下条目由 `claims/claims.json` 与`publish-record.json` 派生，任何肯定句式出现即被 `scripts/claim_gate.py` 判红。
- `invoice.self-built` 开票自研 / 支持开票——开票自研当前不提供（挂账）：票据本体由客户既有合规渠道处理，平台不伪造不代开
- `invoice.tax-return` 税务申报——税务申报当前不提供（挂账）
- `funds.execution` 资金执行（资金池 / 代收代付 / 分账执行 / 资金归集）——二清红线：平台只产出对账结果与核对指令，资金归集与分账走客户自选的持牌通道
- `erp.monolith-ui` 单体 ERP 应用 / 表单式 UI——ERP 以「集群 + 供给包」组合交付，不是单体 ERP 应用或表单式 UI
- `live.official-web-deploy` 官网已 deploy——官网 ERP 落地页为可发布工件 + 本地校验证据；site deploy 需真实凭据，属独立发布步骤
- `live.gateway-rollout` 网关已上线——gateway rollout 需真实凭据与集群变更，属独立发布步骤
- `live.registry-write` registry 快照已写入——registry 写入属独立发布步骤，需真实凭据
- `numbers.unquoted` 任何价格 / 客户数 / 用量 / 上线日期数字——本文与官网 ERP 文案不引用任何数字；如必须引用，须先有仓内可逐字核验的出处（口径表行 ID）
<!-- claim-gate:end:not-claimable -->

- 以上 live 步骤需真实凭据，按**挂账**处理。

## 5. 与五线 / ladder 的关系

- 五条产品线（壹座 / 识律 / 柏讯 / 谦面 / 萬星）构成「一十百千万」阶梯，已封顶；ladder 字符与 numeral 水印由 `content.config.ts` 单一定义。
- ERP 与营销板块同为 **ladder 之外的板块位**：在 tab 导航里位于视觉分隔线之后、用非 ladder 线标、带板块标签、指向独立落地页。
- 判定标准：凡呈现产品线结构，ERP 以板块位出现，五线命名与 slug 无任何变化（见 `erp-board` spec「板块呈现不扰动五线」场景）。本次改动仅新增板块 tab、入口卡与落地页，未新增/改名任何 line。

## 6. 口径说明（数字与命令）

- 本文与官网 ERP 文案**不引用任何数字**（无价格、客户数、用量、上线量、日期）；沿 `docs/marketing/README.md` 的口径三红线（数字溯源 / 禁区 / 模型承诺）与双语对等纪律。如未来必须引用数字，须先有仓内可逐字核验的出处（口径表行 ID），否则不写。
- **本文不粘贴任何命令**。「文档命令与数字均为实证过」按如下方式落实：本 change 的官网侧校验全部为**本地校验**——在 `fd-official-web/` 本机运行内容完整性 gate（脚本 `scripts/check-content-integrity.mjs`），本次交付结果为 `0 hits`（通过）；该 gate 只做退役标识与课程语言判据的仓内扫描，不联网。
- 完整构建链含联网取数步骤（strapi / 仓库 / 指标 / 橱窗 / 口径等），在离线环境下不保证可完整执行，其结果不在本文虚构，**不声称已通过完整构建**。
- 发布状态不再手写：由 `publish-record.json` 派生，下块与 pack 三处注记、市场台账同源（`claim_gate.py` 与 `publish-gate` 守）。

<!-- claim-gate:begin:release-status -->
**发布状态（数据派生，勿手改）**：唯一出处 `publish-record.json`。
- `crossborder/connector-pack`：已上架 市场 v2（packId `cv0kj9mRHGrz6nNMA7glVg`，2026-11-08）
- `restaurant-chain/connector-pack`：已上架 市场 v2（packId `3RZRBdc66rKdLV5I8tuKIg`，2026-11-08）
- `supply-chain/connector-pack`：已上架 市场 v3（packId `y8gnMHFMze-Mylsd6JXpkA`，2026-11-08）
- `official-web.deploy`：未上线（挂账）——本地内容完整性 gate 通过（0 hits）；site deploy 需真实凭据，未执行
- `gateway.rollout`：未上线（挂账）——无集群变更记录
- `registry.snapshot`：未上线（挂账）——未写入 registry
- 缺口 `unionpay-no-provider`：银联收款渠道（结算流水只读）（责任仓 open-connector-mt）——对外标注不可用
- 缺口 `wms-no-provider`：客户 WMS（库存快照只读，角色绑定 wms）（责任仓 open-connector-mt）——对外标注不可用
- 缺口 `kingdee-no-executor-test`：金蝶云星空（凭证/单据同步）（责任仓 open-connector-mt）——对外标注不可用
<!-- claim-gate:end:release-status -->
