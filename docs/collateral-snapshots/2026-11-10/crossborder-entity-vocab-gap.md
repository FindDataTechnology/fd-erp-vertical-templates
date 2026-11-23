# 跨境电商行业线实体词表缺口分析报告

**报告路径**：`/Users/chengsishi/finddata/docs/crossborder-entity-vocab-gap.md`
**生成日期**：2026-11-03
**任务上下文**：OpenSpec change `add-crossborder-vertical-line` 波 0 任务 1.3 侦察+交付

---

## 1. 现有行业实体词表实底

### 1.1 实源位置与真实数量

| 属性 | 值 | 备注 |
|------|-----|------|
| **真实存放库** | PostgreSQL `fd_open_data` (canonical PG，mesh 地址见部署 env) | 生产真源；本地 SQLite (`fd_wire_entities.db`、`fd_wire_dev.db`) **不含**此词表，仅为开发副本 |
| **物理表** | `public.entities` | `entity_type='industry'` 的记录 |
| **实体总数** | **55 个** | 经 2026-10-20 裁决确认：「库里已有 55 个 industry 实体」 |
| **分类来源** | 申万一级 31 个 + GICS 10 个 + 财务报表分类法 14 个 | 三套分类法合并去重后的唯一值 |
| **同步通道** | `fd-open-data-mcp/scripts/sync_sw_industries.py` (每日/每周 cron) | 经 akshare 拉取 申万 L1 并 upsert 入 `entities` 表 |
| **加载接口** | `fd-open-data-mcp/scripts/load_entities.py::load_industry_entities()` | 供 manifest 生成时读取，直连生产 PG |
| **Dev 副本状态** | `fd_wire_entities.db` 中 `entities` 表为空（0 行） | **生产侧对不上**：dev 库滞后且无种子数据，不可用于核对 |

> **结论**：现有 55 个 industry 实体**仅覆盖行业分类维度**（申万/GICS/财务报表），**不包含**跨境电商所需的六大类目任一项。Dev 侧 SQLite 不可信，核对必须连生产 PG。

### 1.2 实体表结构（来自 `alembic/schema_baseline.sql`）

```sql
CREATE TABLE public.entities (
    id integer PRIMARY KEY,
    entity_type character varying(32) NOT NULL,  -- 如 'industry'
    code character varying(128) NOT NULL,         -- 如 '801010' (申万农林牧渔)
    name_en character varying(255),
    name_zh character varying(255),               -- 如 '农林牧渔'
    metadata_json jsonb,                          -- {"classification_system": "shenwan", "level": 1}
    updated_at timestamp without time zone,
    UNIQUE (entity_type, code)
);
```

---

## 2. 六类目对照表

| 类目 | 现有词表覆盖情况 (实体 id/名) | 缺口判定 | 注册建议 (命名遵循 `xb.<domain>.<code>`) |
|------|-------------------------------|----------|------------------------------------------|
| **监管代码**<br>(9610/1210/9710/9810) | ❌ 完全未覆盖<br>现有 industry 实体为行业分类码，非海关监管代码 | 需增量注册 4 个监管代码实体 | `xb.customs.regime_code`<br>code: `9610`/`1210`/`9710`/`9810`<br>metadata: `{"source": "customs", "category": "crossborder_regime"}` |
| **HS 品类/编码** | ❌ 完全未覆盖<br>HS 编码为商品分类，与行业分类体系正交 | 需增量注册 HS 编码实体<br>（建议先注册高频 2/4/6 位编码） | `xb.customs.hs_code`<br>code: 如 `0901`(咖啡)、`6109`(T恤)<br>metadata: `{"source": "customs", "level": 2\|4\|6, "category": "hs_classification"}` |
| **口岸** | ❌ 完全未覆盖<br>现有 entities 表无口岸类型 | 需新增 `entity_type='port'` 并注册主要跨境电商口岸 | `xb.logistics.port`<br>code: 如 `SZG`(深圳湾)、`HGH`(杭州)、`PVG`(浦东)<br>metadata: `{"source": "port_authority", "province": "广东/浙江/上海", "category": "crossborder_port"}` |
| **跨境电商平台**<br>(Amazon/TikTok Shop/Temu/Shein) | ❌ 完全未覆盖<br>现有 company 实体为上市公司，非平台主体 | 需新增 `entity_type='platform'` 或复用 `organization` 类型注册 | `xb.ecom.platform`<br>code: `amazon`/`tiktok_shop`/`temu`/`shein`<br>metadata: `{"source": "platform_official", "category": "crossborder_platform", "gmvs": [...]}` |
| **汇率对** | ❌ 完全未覆盖<br>现有无货币/汇率实体类型 | 需新增 `entity_type='currency_pair'` 或复用 `index` 类型注册 | `xb.fx.pair`<br>code: `USDCNY`/`USDHKD`/`EURCNY`<br>metadata: `{"source": "fx_market", "base": "USD", "quote": "CNY", "category": "fx_rate"}` |
| **政策条目** | ❌ 完全未覆盖<br>policy 为文档级实体，现有词表无对应类型 | 需新增 `entity_type='policy'` 或 `regulation` 注册 | `xb.policy.entry`<br>code: 如 `GACC_2024_123`(海关总署2024年第123号令)<br>metadata: `{"source": "gacc/mofcom/stta/port", "lanes": ["customs","commerce","tax","port"], "category": "policy_entry"}` |

> **结论**：六大类目**零覆盖**，均需增量注册。现有 55 个 industry 实体仅服务于「行业归属」查询，不承载跨境业务语义。

---

## 3. 增量注册操作步骤草案

> 基于真实注册通道（`entity-identity` spec、`entity_source_config` spec、`sync_sw_industries.py` 实战模式），**不臆造 CLI 名**。

### 3.1 准备阶段：确认生产库可写与 entity_type 扩展

| 步骤 | 操作 | 验收 |
|------|------|------|
| 3.1.1 | 连生产 PG (`fd_open_data`)，确认 `entities` 表可写、唯一约束 `(entity_type, code)` 生效 | `INSERT ... ON CONFLICT DO UPDATE` 可跑通 |
| 3.1.2 | 确认 `entity-identity` spec 允许声明式扩展 `entity_type`（spec 第 13-18 行：`entity_type vocabulary SHALL be extensible by declaration`） | 新类型如 `port`/`platform`/`currency_pair`/`policy` 可直接入库，无需 DDL |
| 3.1.3 | 确认 `entity_source_identifiers` 表可存跨源映射（如 HS 编码 ↔ akshare/海关编码） | 同一实体可挂多源 identifier |

### 3.2 实体注册通用流程（参考 `sync_sw_industries.py::upsert_entity`）

```python
# 伪代码：各类目统一走此 upsert 模式
def upsert_xb_entity(session, entity_type: str, code: str, name_zh: str, name_en: str | None, metadata: dict):
    session.execute(text("""
        INSERT INTO entities (entity_type, code, name_en, name_zh, metadata_json)
        VALUES (:et, :c, :ne, :nz, :meta)
        ON CONFLICT (entity_type, code) DO UPDATE SET
            name_en = COALESCE(:ne, name_en),
            name_zh = COALESCE(:nz, name_zh),
            metadata_json = :meta,
            updated_at = NOW()
    """), {"et": entity_type, "c": code, "ne": name_en, "nz": name_zh, "meta": json.dumps(metadata, ensure_ascii=False)})
```

### 3.3 六类目分批注册计划

| 批次 | 类目 | entity_type | 预估条数 | 种子来源 | 注册脚本建议位置 |
|------|------|-------------|----------|----------|------------------|
| 1 | 监管代码 | `customs_regime` | 4 | 海关总署公告原文/《跨境电商监管代码对照表》 | `fd-open-data-mcp/scripts/sync_xb_regimes.py` |
| 2 | 口岸 | `port` | 20-30 (首批主口岸) | 海关总署《全国海关口岸代码表》、口岸办公告 | `fd-open-data-mcp/scripts/sync_xb_ports.py` |
| 3 | 跨境电商平台 | `platform` | 4+ | 各平台官网开放平台文档、备案公示 | `fd-open-data-mcp/scripts/sync_xb_platforms.py` |
| 4 | 汇率对 | `currency_pair` | 10-20 (高频对) | 中国外汇交易中心/人民币汇率中间价 | `fd-open-data-mcp/scripts/sync_xb_fx_pairs.py` |
| 5 | HS 编码 | `hs_code` | 分级注册：先 2 位类(21 个)→4 位项→高频 6 位 | WCO HS Nomenclature、海关商品编码查询系统 | `fd-open-data-mcp/scripts/sync_xb_hs_codes.py` |
| 6 | 政策条目 | `policy` | 增量注册 (随政策巡检产出) | fd-info 四路巡检产出的原文条目 | `fd-wire/api/fd_wire_api/demands/pipeline.py` 集成注册 |

### 3.4 关系与跨源映射同步

- **关系入库**：`entity_relationships` 表，如 `stock --belongs_to--> industry`、`platform --operates_in--> port`、`policy --regulates--> hs_code`
- **跨源标识**：`entity_source_identifiers` 表，如 HS 编码在海关/akshare/WCO 的不同编码映射
- **同步配置**：在 `entity_sources` 表注册各类目同步源（`source_table`、`select_filter`、`cron_expr`），供 `entity-source-config` spec 的调度器自动同步

### 3.5 验收门槛（必须全过）

1. `SELECT COUNT(*) FROM entities WHERE entity_type IN ('customs_regime','port','platform','currency_pair','hs_code','policy')` 返回预期条数
2. 每实体 `metadata_json` 含 `category: "crossborder_*"` 与 `source` 字段，可溯源
3. `entity_source_identifiers` 至少有一条跨源映射样本（如 HS 6 位码 ↔ akshare 编码）
4. 经 `openspec validate add-crossborder-vertical-line --strict` 通过（specs 已声明 `Requirement: 跨境电商实体域入目录且可溯源读取`）

---

## 4. 报告摘要与待生产库核实项

| 节 | 一句话结论 | 仅待生产库可达再核的项 |
|----|------------|------------------------|
| **1. 词表实底** | 现有 55 个 industry 实体仅存生产 PG `fd_open_data.entities`，dev SQLite 为空不可信；三分类法 (申万31+GICS10+财报14) 去重得 55 个，**零覆盖跨境六类目** | 生产库实查 `SELECT COUNT(*) FROM entities WHERE entity_type='industry'` 确认 55；抽样核对 `code`/`name_zh`/`metadata_json.classification_system` |
| **2. 六类目对照** | 监管代码/HS编码/口岸/平台/汇率对/政策条目**全缺口**，需增量注册；命名统一采用 `xb.<domain>.<code>` 格式对齐既有 `yb.<domain>.<native_code>` 风格 | 生产库确认无同名冲突（如 `customs_regime`/`hs_code` 等 entity_type 未被占用） |
| **3. 注册步骤** | 复用 `sync_sw_industries.py` 成熟 upsert 模式，分六批次落脚本至 `fd-open-data-mcp/scripts/`；关系与跨源映射同步入 `entity_relationships`/`entity_source_identifiers`；同步调度登记 `entity_sources` | 生产库实跑首批脚本（监管代码 4 条），验证 upsert、唯一约束、metadata_json 落盘、关系建链、source_identifier 挂载全链路 |
| **4. 验收门槛** | 四条硬性验收（计数、metadata、跨源映射、OpenSpec validate）须全过才可声称「词表就绪」 | OpenSpec validate 需在生产环境跑通，确认 specs/crossborder-vertical-data 的实体注册要求已满足 |

---

**文件路径**：`/Users/chengsishi/finddata/docs/crossborder-entity-vocab-gap.md`  
**侦察完整性**：已按任务要求逐个查实（fd-wire 实体模型/注册面、openspec entity-identity/external-entity-anchors/entity-source-config、dashboards/DAAS 年鉴概念格式与 registry_cli 注册先例、fleet-packs cross-border-ecom-policy 蓝图与 crossborder-vertical-data spec）。  
**唯一新产物**：本报告文件，**未改动任何现有文件**，未触发生产库。

---

## 5. 执行状态（11-04 批次注册，生产实底）

| 批次 | entity_type | 计划 | 实底 | 落库 | 说明 |
|---|---|---|---|---|---|
| 1 监管代码 | customs_regime | 4 | **4** | id 300953-300956 | 定义逐字取自试点在册目录；`sync_xb_regimes.py` |
| 3 平台 | platform | 4+ | **4** | amazon/tiktok_shop(connector_first)/temu/shein | 事实性目录实体，供 D1 授权连接器通道；`sync_xb_entities.py` |
| 4 汇率对 | currency_pair | 10-20 | **1** | EURCNY | 只注册有落库数据支撑的（ecb-exr 仅 CNY 对落地）；其余待源 |
| 5 HS 编码 | hs_code | 分级 | **1** | 854142 | 只注册有落库数据的（un-comtrade 仅 854142）；扩量随 2.3 |
| 2 口岸 | port | 20-30 | **0 暂缓** | — | **portwatch-imf 落库行全是近海石油码头（FSO），非跨境电商口岸**——注册会误导；待海关口岸代码表（通道随 2.1 被上游防护阻断，契约层待人工） |
| 6 政策条目 | policy | 增量 | **0 暂缓** | — | 天然随政策巡检增量注册（试点舰在产，巡检条目落库即注册） |

**验收口径修正（如实）**：`registry_cli` 是概念/源注册面（semantic_code 命名闸），**entities 表本身即实体权威源**（entity-identity spec 第 5 行「唯一的实体权威来源」）——实体「可发现」以生产库回读为准，已抽查 customs_regime 4 行逐条带 source+definition 可溯源。生产库连接面：xinru `fd-postgres`（pg16:30432）库 `fd_open_data`，凭据走 `fd-db-secret`（不落文档）。

## 扩量纪律（2026-11-10 明确）

- **以落库为唯一依据**：某实体只有在其数据行已落库且可回溯时才可注册；扩量脚本先
  `SELECT DISTINCT` 已落库的码，再逐个注册。
- **不为无数据的码预注册**：无数据行的 HS 码进「待落库清单」，不占位注册——
  预注册会让「目录里有」被读成「数据有」，是「读不到当成不存在」的反面错误。
- **外部依赖未到位标暂缓**：口岸类待权威口岸代码表，不注册占位实体。
