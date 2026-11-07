# 四端页面集 × 壹座 miniapp 底座 · 跨仓交接材料

> 对应 OpenSpec change `add-supply-chain-vertical` tasks 6.1/6.6 的跨仓交接面（tasks 6.2–6.5 的 pack 契约本体在 `templates/supply-chain/pagesets/`）。
> 交接方向：finddata 根（本仓，ERP 线契约）→ paas 仓（壹座，底座抽取与页面集实施）。
> 客户中性纪律：本文不出现具体客户名，统称「锚点客户」。核验日期：2026-11-07。

## 一、壹座底座复用点清单（现状文件路径，均已实地核验）

页面集 MUST NOT 另起前端框架或设计系统（spec：底座复用边界）。以下为 paas/miniapp 现状中与四端页面集直接相关的复用点：

### 1.1 Taro4 + React 工程底座

- 工程：`paas/miniapp/`（package name `@platform/miniapp`）；Taro `4.2.1`（@tarojs/* 全家桶）+ React `18.3`，webpack5-runner，TypeScript。
- 工程配置：`paas/miniapp/config/`、`paas/miniapp/project.config.json`、`paas/miniapp/tsconfig.json`。
- 页面注册：`paas/miniapp/src/app.config.ts`（现有 pages：chat / login / bind-guide / share / cron / resources / command）。页面集落地即在 `pages` 数组追加按端分组的页面路径（建议命名空间 `pages/<surface>/...`，如 `pages/ue/home`）。
- 运行时层：`paas/miniapp/src/lib/runtime.ts`（token 生命周期、WS 客户端与 `wsSend`、REST 封装）、`src/lib/taro-http.ts`、`src/lib/taro-socket.ts`——页面集全部网络面经此层，禁止旁路直连。

### 1.2 扫码绑定登录

- 实现：`paas/miniapp/src/lib/bind-scan.ts`（Taro.scanCode 封装，取消不视为失败）、`src/lib/bind-qr.ts`（payload 解析）；页面：`src/pages/login/index`、`src/pages/bind-guide/index`。
- 承接 spec：paas 仓 `openspec/specs/mp-bind-guide`、`miniprogram-auth`、`device-pairing-auth`。
- 复用点：四端 identity（客户联系人/业务员/供应商/承运商）统一走扫码绑定；CP 端提货打卡的作业码扫码复用 `bind-scan` 同一扫码实现（见 `cp-carrier/api-contract.md` §4）。废止重造登录的任何方案。

### 1.3 Daylight 设计系统

- 规范：`paas/miniapp/DESIGN.md`（"The Daylight Pocket"：纸面底、白色 hairline 卡、唯一饱和色 Pocket Blue、不做暗色模式）；该文档自称「WeChat-native dialect of Platform, documented separately from the web workbench（`paas/DESIGN.md`, "The Night Workbench"）」。
- 应用注记：`paas/miniapp/src/app.config.ts`（自定义导航注释）、`src/app.css`。
- 复用点：页面集组件与文案语气按 DESIGN.md 定稿；ERP 侧不得引入第二套色板/卡片体系。AI 建议类输出的「建议」标注样式沿 Pocket Blue 的语义纪律。

### 1.4 确认门交互模式

- 底座形态：「提交→服务器确认→拒绝回显→超时兜底」；交互卡实现 `paas/miniapp/src/components/QuestionCard.tsx`（ask_user_question 工具块的小程序呈现：选项/多选/卡内输入/取消；解决后静态摘要、历史回放同形态；提交经 `wsSend` answer_question，多端先答先得）。
- 承接 spec：paas 仓 `openspec/specs/action-gate`、`permission-mode-selection`。
- 复用点：四端全部 confirmGate 页面（UE 订单/对账、SE 预警拍板、SP 接单/对账、CP 运单/运费）复用该模式与 QuestionCard 同构组件；移动端拍板与 owner 席位同源生效（账本内核 confirm/reject proposal 同一面），超时/失败 MUST NOT 假报成功。

### 1.5 chat = AI 助手入口

- 实现：`paas/miniapp/src/pages/chat/index`（底座主面，Taro + `@platform/core` store）；流式/附件/历史等 spec 见 paas 仓 `chat-*` 系列与 `chat-ui-shell`。
- 复用点：四端 AI 助手入口即底座 chat 面的端内入口（spec：AI 助手入口）；经集群/舰队面接经营问答/推荐/预警处置，与页面同源取数（MUST NOT 建平行取数路径）；AI 动作类输出必须经确认门。

### 1.6 提审合规经验

- 底座经验面：paas 仓 `openspec/specs/mp-demo-sandbox`（为微信审核场景设「无账号演示沙箱」：审核员/新用户真聊、独立一次性实例、无个人授权弹窗、限额可解释——`mp-demo-mode`/`mp-demo-sandbox` 两 spec）、`miniprogram-auth`（openid/unionid 侧数据纪律）、`mp-bind-guide`。
- 复用点：四端提审材料（类目选择、隐私声明、演示账号/沙箱路径、审核员体验路径）沿底座既有做法整理；页面集新增采集项（定位/影像，CP/SE 端可选采集）MUST 在隐私声明中列明并过审后再上。
- 注意：底座 DEMO 沙箱面是「平台演示」形态；ERP 端提审如需演示模式，按 `mp-demo-mode` 同构另立，不得把生产 ERP 数据暴露给审核路径。

## 二、页面集实施需在 paas 仓另立的事项清单

paas 仓有独立 openspec 根（`paas/openspec/`，specs 目录已含 `vertical-packs`、`pack-authoring`、`bundle-manifest`、`miniprogram-client` 等）。以下事项在 paas 侧开 change，本仓只持契约（tasks 6.1/6.5 的跨仓注记）：

1. **底座模板抽取**（tasks 6.1）：从 `paas/miniapp` 抽出「Taro4 工程 + 登录绑定 + 消息 + Daylight + 确认门」可复用底座层，定义 `pageset-base/v1` 兼容契约（四端 manifest 的 `base.minBaseContract` 已引用此名，paas 侧落名后回填）。
2. **页面集装载机制**：pack 定义（本仓 `templates/supply-chain/pagesets/*/manifest.json`）如何被 paas 端消费——按租户授权组合动态出页面（spec：页面集 pack 化与授权组合；未授权页面集端内不可见）。可对位 paas 既有 `pack-installation`/`pack-visibility`/`vertical-packs` 机制，但 ERP 页面集是「页面级」pack，与既有「skill+MCP+agent」组合 pack 不同构，需 paas 侧裁定复用还是新立。
3. **四端页面实施**：`ue-commerce` / `se-sales` / `sp-supplier` / `cp-carrier` 四个 pack 的页面落地（页面清单与 API 消费面见各 pack 的 manifest.json 与 api-contract.md）。
4. **配套后端 REST/WS 面**（tasks 6.5）：各 api-contract.md 声明的 `/erp/supply-chain/v1/*` 端点与 WS 事件实施；与 MCP 工具面同源（账本内核 8 工具、计费引擎、编排层），网关实况如需调整路径前缀，SHALL 经本仓 change 修订契约。
5. **授权模型对位**：`requiredGrants`（如 `order.write.proxy`、`statement.confirm`）接入 paas 权限/角色面（`role-system`、`auth`），并落实「页面集未授权→端内不可见」。
6. **提审与合规延伸**（tasks 6.6）：按 §1.6 整理四端提审材料；新增采集项的隐私声明增补。

## 三、BYOK 凭证零落库在移动端的延伸要求（tasks 6.6 / spec：合规与凭证零落库）

- 契约本体：BYOK 与凭证零落库——凭证经环境变量注入面交接，不落库、不出日志（账本内核 byok 纪律，`fd-erp-ledger-mcp` byok 面）。
- 移动端延伸（spec：移动端凭证纪律 Scenario）：
  1. 商家在移动端完成外部系统授权（如金蝶第三方应用授权、支付渠道商户授权）时，凭证**只进连接器凭证面**（萬星连接器 custom_credential 字段），不在小程序 storage、日志、报表、截屏面出现；
  2. 移动端触发连接器调用的链路是「端→壹座后端→连接器」token 链，端上只持会话 PAT（底座既有），**不持任何外部系统凭证**；
  3. 验收判据：四端任一页面的网络抓包与本地存储均不含外部系统凭证字段；日志审计（连接器 + 壹座后端）凭证字段零出现（redact 纪律，对齐连接器 `Kingdee login` 实现的 secret redact）。

## 四、交接核对清单

| # | 事项 | 本仓工件 | paas 侧动作 |
|---|---|---|---|
| 1 | 底座兼容契约 | 四 pack `base.minBaseContract: pageset-base/v1` | 抽取底座层并落契约名，回填本仓 manifest |
| 2 | 四端页面集 | `templates/supply-chain/pagesets/{ue-commerce,se-sales,sp-supplier,cp-carrier}/` | 按 manifest 页面清单实施 |
| 3 | 后端契约 | 各 pack `api-contract.md` | 实施 `/erp/supply-chain/v1/*`，同源纪律不变 |
| 4 | 授权组合 | 各 pack `authorization` 段 | 接入 paas 权限面，未授权不可见 |
| 5 | 提审合规 | 本文 §1.6 | 按 `mp-demo-*`/`miniprogram-auth` 经验整理材料 |
| 6 | BYOK 延伸 | 本文 §三 | 移动端凭证纪律验收（抓包/存储/日志三面） |

## 五、挂账项（如实）

1. 锚点客户 310 点逐条映射：功能域级覆盖已在各 pack 落账（UE 7 域 / SE 3 域 / SP 4 域 / CP 4 域全数覆盖）；310 点清单资产化后回填各 manifest `blueprint` 段做逐点核对。
2. 锚点客户 WMS 品牌与接口形态未定（design Open Questions），影响 SP/CP 页面中「外部执行状态回流」的实施细节（契约面不受影响）。
3. `pageset-base/v1` 为本仓占位名，paas 侧抽取落地后终裁命名。
4. 四端是否一期全上属客户侧范围决策（design D8），本 pack 不预设裁剪。
