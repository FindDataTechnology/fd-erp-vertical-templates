# ERP 对外能力清单（数据派生）

> 由 `scripts/claim_gate.py render` 从 `claims/claims.json`、`claims/planned-surfaces.json`、`publish-record.json` 与 `gaps.json` 生成，勿手改。已交付段只列有证据的能力；规划中段的条数以挂账清单为准（当前 20 条），不认文档里手写的旧数。

## 一、已交付（有证据）

- **复式事件账（agent 拟稿经确认门才成为事实；外部系统事实经导入车道按来源权威直接成为事实；事实按人工确认的过账规则派生分录，缺规则即挂待补、不猜科目；借贷必平由数据库约束强制；分录 append-only；余额确定性重算）**  
  证据：code:fd-erp-ledger-mcp/fd_erp_ledger_mcp/migrations/0001_baseline.sql；mcp_tool:submit_proposal；mcp_tool:confirm_proposal；mcp_tool:query_balance
- **stable_id 幂等去重（同源重推只落一次）**  
  证据：code:fd-erp-ledger-mcp/fd_erp_ledger_mcp/proposals.py
- **多币种：原币 + 本位币折算，汇率带 as_of 与出处，无出处拒绝**  
  证据：code:fd-erp-ledger-mcp/fd_erp_ledger_mcp/migrations/0005_billing_fx_provenance.sql；code:fd-erp-ledger-mcp/fd_erp_ledger_mcp/fx.py
- **对账只读滩头：四类差异含双侧原始引用，对外部系统零写入**  
  证据：mcp_tool:run_reconciliation；mcp_tool:get_recon_report；test:test_missing_and_extra
- **三个行业模板以四件套交付（连接器 pack / 舰队蓝图 / 科目映射 / 报表 preset），各件独立版本**  
  证据：asset:templates/crossborder/manifest.json；asset:templates/restaurant-chain/manifest.json；asset:templates/supply-chain/manifest.json
- **扩展件：取价规则 pack（规则带出处）+ 四端页面集 pack（壹座底座契约）**  
  证据：asset:templates/supply-chain/pricing-rules/pack.json；asset:templates/supply-chain/pagesets/pack.json
- **出网单口：商家数据只经连接器运行时读取，模板层不持端点、不发 HTTP、不读凭证**  
  证据：code:fd-erp-vertical-templates/fd_erp_vertical_templates/runtime_client.py；test:test_connector_modules_do_not_import_http_clients
- **连接器三态证据链（目录 provider / 执行器测试 / live 验收），缺一态即未接通**  
  证据：asset:connector-status.json；test:test_kingdee_has_catalog_but_no_executor_test
- **分成只产出双方可核对的分成对账单与核对指令，零资金执行面（二清红线）**  
  证据：code:fd-erp-vertical-templates/fd_erp_vertical_templates/recon/shares.py；test:test_share_execution_denied
- **事件计费与计费规则审批（bill_event / approve_billing_rule）**  
  证据：mcp_tool:bill_event；mcp_tool:approve_billing_rule
- **双客群租户隔离：经营数据按租户（cell）隔离，查询闸强制租户列**  
  证据：code:fd-erp-ledger-mcp/fd_erp_ledger_mcp/tenancy.py
- **三个行业连接器包已在市场（supply-chain v3 / restaurant-chain v2 / crossborder v2）**  
  证据：release:supply-chain/connector-pack；release:restaurant-chain/connector-pack；release:crossborder/connector-pack

## 二、规划中（20 条，全部来自挂账清单）

| 能力面 | 域 | 端点 | 规划车道 |
| --- | --- | --- | --- |
| supply-chain.cp | dock-slots | `GET /erp/supply-chain/v1/cp/dock-slots` | 仓库作业编排月台资源建模（时段目录 + 占用计算） |
| supply-chain.cp | freight-statements | `GET /erp/supply-chain/v1/cp/freight-statements` | 计费 read 工具面（fee_details 按承运商归集 + 规则/事件/单据/原始作业四层溯源）→ 运费单派生；确认门届时=内核自平核对 proposal（同 UE 对账契约） |
| supply-chain.cp | freight-statements | `GET /erp/supply-chain/v1/cp/freight-statements/{statement_id}` | 同 GET /cp/freight-statements |
| supply-chain.cp | freight-statements | `POST /erp/supply-chain/v1/cp/freight-statements/{statement_id}/confirm` | 运费单 read 面到位后确认门=内核自平核对 proposal（超时/失败不假报成功）；异议进处置车道 |
| supply-chain.se | leads | `GET /erp/supply-chain/v1/se/leads` | CRM 线索/拜访事件档（编排层扩展）+ 底座 chat 面线索助手 |
| supply-chain.se | leads | `POST /erp/supply-chain/v1/se/leads` | CRM 线索/拜访事件档（编排层扩展） |
| supply-chain.se | statements | `POST /erp/supply-chain/v1/se/statements/{statement_id}/push` | 底座端内消息通道（notify 车道）接入后实施推送留痕；客户确认门=UE /ue/statements/{id}/confirm（内核 proposal 车道） |
| supply-chain.se | visits | `GET /erp/supply-chain/v1/se/visits` | CRM 拜访事件档（编排层扩展） |
| supply-chain.se | visits | `POST /erp/supply-chain/v1/se/visits` | CRM 拜访事件档（编排层扩展；定位/影像可选采集随隐私声明过审启用） |
| supply-chain.sp | invoices | `GET /erp/supply-chain/v1/sp/invoices` | 同 POST /sp/invoices（上传历史与核验进度 read 面） |
| supply-chain.sp | invoices | `POST /erp/supply-chain/v1/sp/invoices` | 中台文件服务上传契约 + 发票查验/勾兑车道 + 金蝶连接器回写（contract-only） |
| supply-chain.sp | settlements | `GET /erp/supply-chain/v1/sp/settlements` | 金蝶付款/结算回写 read 面（只读，无任何支付/账户操作） |
| supply-chain.sp | statements | `GET /erp/supply-chain/v1/sp/statements` | 金蝶 provider 往来面拉取（任务 3.1）→ 账本内核计算应付 → 单据级对账单派生 + 确认门（内核自平核对 proposal，同 UE 契约） |
| supply-chain.sp | statements | `GET /erp/supply-chain/v1/sp/statements/{statement_id}` | 同 GET /sp/statements |
| supply-chain.sp | statements | `POST /erp/supply-chain/v1/sp/statements/{statement_id}/confirm` | 金额同源到位后确认门=内核自平核对 proposal（超时/失败不假报成功）；异议进处置车道 |
| supply-chain.ue | aftersale-requests | `GET /erp/supply-chain/v1/ue/aftersale-requests` | 售后处置车道（待编排层扩展） |
| supply-chain.ue | aftersale-requests | `POST /erp/supply-chain/v1/ue/aftersale-requests` | 售后处置车道（待编排层扩展） |
| supply-chain.ue | aftersale-requests | `GET /erp/supply-chain/v1/ue/aftersale-requests/{request_id}` | 售后处置车道（待编排层扩展） |
| supply-chain.ue | invoice-requests | `GET /erp/supply-chain/v1/ue/invoice-requests` | 后台审核车道 + 金蝶/线下开票通道 |
| supply-chain.ue | invoice-requests | `POST /erp/supply-chain/v1/ue/invoice-requests` | 后台审核车道 + 金蝶/线下开票通道 |

## 三、当前不可用（缺口登记 3 条）

- **银联收款渠道（结算流水只读）**：目录无 unionpay provider；模板层只能声明角色/真 id，不得以自建 HTTP 适配补齐（ADR 0002）（责任仓 open-connector-mt）
- **客户 WMS（库存快照只读，角色绑定 wms）**：目录无任何可映射到角色 wms 的 provider，角色候选集为空 → 安装该角色绑定被阻断（bindings.py）（责任仓 open-connector-mt）
- **金蝶云星空（凭证/单据同步）**：目录有 kingdee provider，但执行器目录无测试文件（*.test.ts 计数为 0）（责任仓 open-connector-mt）
