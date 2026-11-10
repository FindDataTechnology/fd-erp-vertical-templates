# 上架提交材料（派生件，勿手改）

> 由 `scripts/market_submission.py` 从资产 `i18n` 块导出；资产改动后重跑即得。
> 模板：`crossborder` · 语言：zh（基准）/ en / ja / es / fr
> 各平台的长原因不在此表：见随包分发的 `capability-matrix.json`（机器可读、可追溯）。

## 一览

| 资产 | 版本 | 路径 | tags 长度（逐语言） |
|---|---|---|---|
| connector-pack | v1.5.4 | `connector-pack/pack.json` | 5/5/5/5 |
| account-mapping-pack | v1.1.3 | `account-mapping/pack.json` | 4/4/4/4 |
| recon-fleet | v1.1.3 | `recon-fleet/template.json` | 4/4/4/4 |

## connector-pack · v1.5.4

> 连接器 pack：能力面逐平台由 capability-matrix.json 生成

### 中文（基准）（zh）

**name**

```
跨境电商对账连接器包
```

**description**

```
跨境电商对账模板·连接器 pack。能力面以包内 capability-matrix.json 为准：Shopify：需一次接入（provider 动作存在（动作面已核对 catalog），但无真实店读取记录（真店 POC 未开，现仅合成 fixture））；TikTok Shop：需一次接入（通道未实现：全仓仅见一句文案，无 provider、无适配器）；淘宝：需一次接入（骨架待锚点客户：目录无 provider，按 ADR 0002 先有客户再实现）；天猫：需一次接入（骨架待锚点客户（与淘宝共用同一 provider））；京东：需一次接入（骨架待锚点客户：目录无 provider）；商家电商 ERP（旺店通/聚水潭等）：需一次接入（通道未实现；国内库存腿的唯一来源，优先实现）；拼多多：不提供（上游接口 schema 缺失，当前不提供；不声明绑定、不留槽位）。凭证 BYOK 经连接器运行时注入，零落库零日志；对外部系统零写入。真实店读写沿 crm-board 2026-10-30 裁决逐店开通。
```

**tags**

```
ERP / 跨境电商 / 对账 / 连接器 / BYOK
```

### English（en）

**name**

```
Cross-border e-commerce reconciliation connector pack
```

**description**

```
Connector pack for the cross-border e-commerce reconciliation template. Capability surface follows capability-matrix.json in this pack: Shopify: needs one onboarding; TikTok Shop: needs one onboarding; Taobao: needs one onboarding; Tmall: needs one onboarding; JD: needs one onboarding; Merchant e-commerce ERP: needs one onboarding; Pinduoduo: not offered Per-platform reasons and review windows are recorded in capability-matrix.json. Credentials are injected by the connector runtime (BYOK), never stored or logged; zero writes to external systems. Real-store reads open per store under the crm-board ruling of 2026-10-30.
```

**tags**

```
ERP / cross-border e-commerce / reconciliation / connector / BYOK
```

### 日本語（ja）

**name**

```
越境EC照合コネクタパック
```

**description**

```
越境EC照合テンプレート用コネクタパック。能力面は同梱の capability-matrix.json に従います：Shopify: 初回接続が必要；TikTok Shop: 初回接続が必要；Taobao: 初回接続が必要；Tmall: 初回接続が必要；JD: 初回接続が必要；事業者向けEC ERP: 初回接続が必要；Pinduoduo: 提供対象外 プラットフォーム別の理由と再確認期限は capability-matrix.json に記載。資格情報はコネクタランタイム側で注入（BYOK）し、保存もログ出力もしません。外部システムへの書き込みはゼロ。実店舗の読み取りは 2026-10-30 の crm-board 決定に従い店舗ごとに開放します。
```

**tags**

```
ERP / 越境EC / 照合 / コネクタ / BYOK
```

### Español（es）

**name**

```
Pack de conectores para conciliación de comercio electrónico transfronterizo
```

**description**

```
Pack de conectores para la plantilla de conciliación de comercio electrónico transfronterizo. La capacidad sigue a capability-matrix.json incluido en el pack: Shopify: requiere una conexión; TikTok Shop: requiere una conexión; Taobao: requiere una conexión; Tmall: requiere una conexión; JD: requiere una conexión; ERP de comercio electrónico: requiere una conexión; Pinduoduo: no disponible Los motivos por plataforma y las ventanas de revisión están en capability-matrix.json. Las credenciales las inyecta el runtime del conector (BYOK), sin almacenarse ni registrarse; cero escrituras a sistemas externos. La lectura de tiendas reales se abre tienda por tienda según la decisión de crm-board del 2026-10-30.
```

**tags**

```
ERP / comercio electrónico transfronterizo / conciliación / conector / BYOK
```

### Français（fr）

**name**

```
Pack de connecteurs pour la réconciliation e-commerce transfrontalière
```

**description**

```
Pack de connecteurs pour le modèle de réconciliation e-commerce transfrontalière. La capacité suit capability-matrix.json inclus dans le pack : Shopify: connexion à faire ; TikTok Shop: connexion à faire ; Taobao: connexion à faire ; Tmall: connexion à faire ; JD: connexion à faire ; ERP e-commerce marchand: connexion à faire ; Pinduoduo: non proposé Les motifs par plateforme et les fenêtres de revue figurent dans capability-matrix.json. Les identifiants sont injectés par le runtime du connecteur (BYOK), jamais stockés ni journalisés ; aucune écriture vers des systèmes externes. La lecture des boutiques réelles s'ouvre boutique par boutique selon la décision crm-board du 2026-10-30.
```

**tags**

```
ERP / e-commerce transfrontalière / réconciliation / connecteur / BYOK
```

## account-mapping-pack · v1.1.3

> 科目映射 pack：呈现层口径，替换映射不改账

### 中文（基准）（zh）

**name**

```
跨境电商科目映射包
```

**description**

```
跨境电商对账模板·科目映射 pack：行业科目对齐内核通用科目最小集（呈现层口径）。映射只影响报表与查询呈现，已落账分录零改动；替换映射不改账。映射 MUST NOT 向内核注入行业科目行（本包无任何写内核路径）。
```

**tags**

```
ERP / 跨境电商 / 科目映射 / 呈现层
```

### English（en）

**name**

```
Cross-border e-commerce account mapping pack
```

**description**

```
Account mapping pack for the cross-border e-commerce reconciliation template: aligns industry accounts onto the ledger kernel's universal minimum account set (presentation-layer caliber). The mapping only affects reports and query presentation; posted entries are untouched, and replacing the mapping does not change the books. The mapping must not inject industry account rows into the kernel (this pack has no kernel write path).
```

**tags**

```
ERP / cross-border e-commerce / account mapping / presentation layer
```

### 日本語（ja）

**name**

```
越境EC勘定科目マッピングパック
```

**description**

```
越境EC照合テンプレート用の科目マッピングパック：業界科目を帳簿カーネルの共通最小科目セットに寄せます（表示層の口径）。マッピングはレポートと照会の表示にのみ影響し、記帳済み仕訳は一切変更しません。マッピングを差し替えても帳簿は変わりません。カーネルに業界科目行を注入してはならず、本パックにカーネルへの書き込み経路はありません。
```

**tags**

```
ERP / 越境EC / 科目マッピング / 表示層
```

### Español（es）

**name**

```
Pack de mapeo de cuentas de comercio electrónico transfronterizo
```

**description**

```
Pack de mapeo de cuentas para la plantilla de conciliación de comercio electrónico transfronterizo: alinea las cuentas del sector con el conjunto mínimo universal del núcleo contable (calibre de presentación). El mapeo solo afecta a informes y consultas; los asientos ya registrados no se modifican y sustituir el mapeo no cambia los libros. El mapeo no debe inyectar cuentas del sector en el núcleo (este pack no tiene ruta de escritura al núcleo).
```

**tags**

```
ERP / comercio electrónico transfronterizo / mapeo de cuentas / capa de presentación
```

### Français（fr）

**name**

```
Pack de mapping de comptes e-commerce transfrontalière
```

**description**

```
Pack de mapping de comptes pour le modèle de réconciliation e-commerce transfrontalière : aligne les comptes du secteur sur le jeu minimal universel du noyau comptable (calibre de présentation). Le mapping n'affecte que les rapports et les requêtes ; les écritures comptabilisées restent intactes et remplacer le mapping ne change pas les livres. Le mapping ne doit pas injecter de comptes du secteur dans le noyau (ce pack n'a aucun chemin d'écriture vers le noyau).
```

**tags**

```
ERP / e-commerce transfrontalière / mapping de comptes / couche de présentation
```

## recon-fleet · v1.1.3

> 对账舰队模板：巡检/核对/拍板三岗

### 中文（基准）（zh）

**name**

```
跨境电商对账舰队（模板）
```

**description**

```
跨境电商对账舰队：巡检岗按店铺触发订单-收款-库存三方对账（内核 run_reconciliation + 模板对账驱动报告）、核对岗四类差异逐条定性并引用三舰观测证据解释（在途/签收引物流舰、供给纠偏引合规舰）、拍板岗 owner 席位对 escalate 差异拍板（确认门唯一落账）。复用跨境线既有三舰观测面（监测/物流/合规）作差异解释依据，不另建平行数据路径。平台侧零写入；核对结论回写经内核 proposal 白名单车道（确认门只在拍板席位）。
```

**tags**

```

```

### English（en）

**name**

```
Cross-border e-commerce reconciliation fleet (template)
```

**description**

```
Reconciliation fleet for cross-border e-commerce: the watcher post runs order-payment-inventory three-way reconciliation per store (kernel run_reconciliation plus the template's recon driver report); the reconciler post classifies each of the four discrepancy classes and explains in-transit or signed-off gaps by citing the logistics fleet's observations and supply corrections by citing the compliance fleet; the decider post sits at the owner seat and rules on escalated discrepancies (the confirm gate is the only write path). It reuses the existing cross-border observation fleets (monitoring, logistics, compliance) as the evidence base instead of building a parallel data path. Zero writes to the platform side; conclusions are written back only through the kernel's proposal lane.
```

**tags**

```
ERP / cross-border e-commerce / reconciliation / fleet
```

### 日本語（ja）

**name**

```
越境EC照合フリート（テンプレート）
```

**description**

```
越境EC向け照合フリート：巡回ポストが店舗ごとに注文・入金・在庫の三者照合を実行し（カーネルの run_reconciliation とテンプレートの照合ドライバレポート）、照合ポストが四類の差異を一件ずつ分類して、輸送中・受領済の差異は物流フリートの観測を、供給是正はコンプライアンスフリートの観測を引用して説明します。判定ポストは owner 席位でエスカレーションされた差異を裁定します（確定ゲートが唯一の記帳経路）。既存の越境観測フリート（監視・物流・コンプライアンス）を説明根拠として再利用し、並行するデータ経路は作りません。プラットフォーム側への書き込みはゼロ。結論の書き戻しはカーネルの提案レーンのみです。
```

**tags**

```
ERP / 越境EC / 照合 / フリート
```

### Español（es）

**name**

```
Flota de conciliación de comercio electrónico transfronterizo (plantilla)
```

**description**

```
Flota de conciliación para comercio electrónico transfronterizo: el puesto de vigilancia ejecuta la conciliación tripartita pedido-pago-inventario por tienda (run_reconciliation del núcleo más el informe del driver de conciliación de la plantilla); el puesto de conciliación clasifica cada una de las cuatro clases de discrepancia y explica las diferencias en tránsito o recibidas citando las observaciones de la flota logística y las correcciones de suministro citando la flota de cumplimiento; el puesto de decisión ocupa el asiento del propietario y resuelve las discrepancias escaladas (la puerta de confirmación es la única vía de registro). Reutiliza las flotas de observación transfronterizas existentes (monitorización, logística, cumplimiento) como base de evidencia en lugar de crear una vía de datos paralela. Cero escrituras en la plataforma; las conclusiones solo se devuelven por el carril de propuestas del núcleo.
```

**tags**

```
ERP / comercio electrónico transfronterizo / conciliación / flota
```

### Français（fr）

**name**

```
Flotte de réconciliation e-commerce transfrontalière (modèle)
```

**description**

```
Flotte de réconciliation pour l'e-commerce transfrontalière : le poste de veille exécute la réconciliation tripartite commande-paiement-stock par boutique (run_reconciliation du noyau plus le rapport du driver de réconciliation du modèle) ; le poste de réconciliation classe chacune des quatre classes d'écart et explique les écarts en transit ou réceptionnés en citant les observations de la flotte logistique et les corrections d'approvisionnement en citant la flotte de conformité ; le poste de décision siège côté propriétaire et tranche les écarts escaladés (la porte de confirmation est le seul chemin d'écriture). Elle réutilise les flottes d'observation transfrontalières existantes (veille, logistique, conformité) comme base de preuve au lieu de créer un chemin de données parallèle. Aucune écriture côté plateforme ; les conclusions ne sont réécrites que par le canal de propositions du noyau.
```

**tags**

```
ERP / e-commerce transfrontalière / réconciliation / flotte
```
