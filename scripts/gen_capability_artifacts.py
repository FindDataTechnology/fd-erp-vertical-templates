#!/usr/bin/env python3
"""由能力矩阵生成派生资产（change `truth-ecommerce-capability-surface`（已归档，见 spec `ecommerce-capability-matrix`） 任务 2.1/2.2）。

派生关系是单向的：**矩阵 → 产物**。产物里带 `_generated_from.fingerprint`，
测试比对指纹与当前矩阵是否一致——矩阵改了而产物没重生成，测试即红。

用法：
  python3 scripts/gen_capability_artifacts.py               # 就地重生成
  python3 scripts/gen_capability_artifacts.py --check       # 只检查是否同步（CI 用）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fd_erp_vertical_templates.capability import (  # noqa: E402
    STATUS_AVAILABLE,
    STATUS_UNAVAILABLE,
    CapabilityMatrix,
    load_matrix,
)

TEMPLATE_DIR = REPO / "templates" / "crossborder"
MATRIX_PATH = TEMPLATE_DIR / "capability-matrix.json"
PRESET_PATH = TEMPLATE_DIR / "report-preset" / "preset.json"
PACK_PATH = TEMPLATE_DIR / "connector-pack" / "pack.json"

#: 报表段键 → 矩阵流。未列出的段（差异汇总/映射看板）不绑腿，永远渲染。
SECTION_FLOW = {
    "orders": "orders",
    "payments": "settlements",
    "inventory": "inventory",
}


def _dump(path: Path, body: dict) -> None:
    path.write_text(json.dumps(body, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")


REVIEW_PENDING_PATH = TEMPLATE_DIR / "REVIEW-PENDING.md"


def render_review_pending(matrix: CapabilityMatrix) -> str:
    """待补复核清单：每个 unmapped 格一条，带原因与下一步。

    这份清单是「为什么现在还不能宣称」的对外解释来源；矩阵变它就变。
    """
    rows = matrix.review_pending()
    due: list[dict] = []
    for (platform, flow) in sorted(matrix.cells):
        cell = matrix.cells[(platform, flow)]
        if cell.status == "unavailable" and cell.measured_at is None:
            due.append({"platform": platform, "flow": flow,
                        "basis": "从未复核", "reason": cell.reason})
    lines = [
        "# 待补复核清单（能力矩阵）",
        "",
        f"> 由 `scripts/gen_capability_artifacts.py` 从 `capability-matrix.json` 生成，"
        f"请勿手改（矩阵指纹 `{matrix.fingerprint()}`）。",
        f"> 生成时刻的矩阵状态：{matrix.summary()}。",
        "",
        f"## 未接通（unmapped）：{len(rows)} 格",
        "",
        "| 平台 | 流 | 原因 | 下一步 |",
        "|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['platform']} | {r['flow']} | {r['reason']} | {r['next_action']} |")
    if due:
        lines += ["", f"## 待复核的「不提供」（unavailable）：{len(due)} 格", "",
                  "| 平台 | 流 | 依据 | 原因 |", "|---|---|---|---|"]
        for d in due:
            lines.append(f"| {d['platform']} | {d['flow']} | {d['basis']} | {d['reason']} |")
    lines += ["", "## 口径纪律", "",
              "- `unmapped` = 可接未接；对外说「需一次接入」，不说「不支持」。",
              "- `unavailable` = 确认拿不到；对外说「不提供」，不给接入日期。",
              "- 升格为可得要两半证据：provider 动作存在 + 一次真实读取记录。", ""]
    return "\n".join(lines)


def _platform_status_summary(matrix: CapabilityMatrix) -> dict[str, dict]:
    out = {}
    for key in matrix.platform_keys():
        flows = {}
        for flow in ("orders", "settlements", "inventory", "refunds"):
            cell = matrix.cells.get((key, flow))
            flows[flow] = {
                "status": cell.status if cell else "unmapped",
                "reason": cell.reason if cell else "矩阵缺格（未登记）",
            }
        out[key] = {
            "display_zh": matrix.platform_meta(key).get("display_zh", key),
            "display_en": matrix.platform_meta(key).get("display_en", key),
            "connector": matrix.platform_meta(key).get("connector"),
            "flows": flows,
        }
    return out


def _leg_capability(matrix: CapabilityMatrix) -> dict[str, dict]:
    """每条腿在各平台的可得性（preset 自证用）。"""
    out = {}
    for leg, flow in SECTION_FLOW.items():
        by_platform = {}
        for key in matrix.platform_keys():
            cell = matrix.cells.get((key, flow))
            by_platform[key] = cell.status if cell else "unmapped"
        out[leg] = {
            "flow": flow,
            "by_platform": by_platform,
            "supplied_by_any": any(s == STATUS_AVAILABLE for s in by_platform.values()),
        }
    return out


#: 跨境报表指标口径（单一出处：对账驱动 + 内核查询面）。缺它 preset 根本加载不了
#: ——这是改造前就存在的缺口（`reports.load_report_preset` 要求 metric_calibers）。
METRIC_CALIBERS = [
    {"key": "order_count", "label_zh": "订单笔数", "label_en": "Order count",
     "definition": "对账期内平台订单流笔数（按平台单号去重，重复推送只计一次）",
     "definition_source": "recon driver: reconcile_orders_payments（连接器只读面 → 订单流）",
     "grain": "store × day"},
    {"key": "order_amount", "label_zh": "订单金额", "label_en": "Order amount",
     "definition": "对账期内订单原币金额合计；本位币折算仅作呈现且须带汇率出处",
     "definition_source": "recon driver: reconcile_orders_payments（订单流 amount 字段）",
     "grain": "store × day"},
    {"key": "payout_count", "label_zh": "结算笔数", "label_en": "Payout count",
     "definition": "对账期内平台结算（payouts）笔数；结算面不可得时该指标不可得，不以订单笔数顶替",
     "definition_source": "recon driver: fee_report（结算流 payouts）",
     "grain": "store × day"},
    {"key": "payout_amount", "label_zh": "结算金额", "label_en": "Payout amount",
     "definition": "对账期内平台结算金额合计；**不得由订单金额加总推算**（佣金/运费险/补贴只在结算侧）",
     "definition_source": "recon driver: fee_report（结算流 amount）",
     "grain": "store × day"},
    {"key": "commission", "label_zh": "平台佣金", "label_en": "Commission",
     "definition": "结算侧披露的佣金合计，独立维度不与销售流水混算",
     "definition_source": "recon driver: fee_report（raw.fees.commission）",
     "grain": "store × settlement"},
    {"key": "payment_fee", "label_zh": "支付手续费", "label_en": "Payment fee",
     "definition": "结算侧披露的支付手续费合计，独立维度",
     "definition_source": "recon driver: fee_report（raw.fees.payment_fee）",
     "grain": "store × settlement"},
    {"key": "matched_count", "label_zh": "已配对笔数", "label_en": "Matched count",
     "definition": "订单与库存状态已对齐的笔数；库存腿不可得时本指标不可得，不写 0",
     "definition_source": "recon driver: reconcile_orders_inventory（状态对齐）",
     "grain": "store × day"},
    {"key": "in_transit_count", "label_zh": "在途笔数", "label_en": "In-transit count",
     "definition": "订单已发未签收导致库存未扣减的笔数，打 needs_explanation=logistics",
     "definition_source": "recon driver: reconcile_orders_inventory（状态腿）",
     "grain": "store × day"},
    {"key": "missing_in_right", "label_zh": "缺失笔数", "label_en": "Missing",
     "definition": "左有右无的差异笔数，每条带双侧原始引用",
     "definition_source": "recon driver: align_pair（差异四类）",
     "grain": "store × pair × day"},
    {"key": "extra_in_right", "label_zh": "多余笔数", "label_en": "Extra",
     "definition": "右多出的差异笔数，每条带双侧原始引用",
     "definition_source": "recon driver: align_pair（差异四类）",
     "grain": "store × pair × day"},
    {"key": "amount_mismatch", "label_zh": "金额不符笔数", "label_en": "Amount mismatch",
     "definition": "金额不一致的差异笔数（差额 = 双侧金额之差）",
     "definition_source": "recon driver: align_pair（差异四类）",
     "grain": "store × pair × day"},
    {"key": "status_mismatch", "label_zh": "状态不符笔数", "label_en": "Status mismatch",
     "definition": "状态不一致的差异笔数（如在途 vs 已签收）",
     "definition_source": "recon driver: align_pair / align_status（差异四类）",
     "grain": "store × pair × day"},
]

#: 阈值预警 → 处置任务（口径与 supply-chain 同形：阈值规则必须能派单）。
THRESHOLD_ALERTS = [
    {"metric": "amount_mismatch", "threshold": "0", "op": "gt", "severity": "high",
     "disposal_task": "金额不符复核：逐笔对双侧原始引用，定性后走内核 proposal 车道回写（确认门在拍板席）",
     "assignee_hint": "reconciler"},
    {"metric": "status_mismatch", "threshold": "0", "op": "gt", "severity": "medium",
     "disposal_task": "状态不符复核：引用物流舰观测证据解释在途/签收差异，不在对账层自行判定",
     "assignee_hint": "reconciler"},
    {"metric": "missing_in_right", "threshold": "0", "op": "gt", "severity": "high",
     "disposal_task": "缺失笔数复核：确认收款/库存侧是否漏单，漏单挂应收（内核 proposal，科目白名单）",
     "assignee_hint": "reconciler"},
]


def generate_preset(matrix: CapabilityMatrix) -> dict:
    preset = json.loads(PRESET_PATH.read_text(encoding="utf-8"))
    legs = _leg_capability(matrix)

    for rep in preset.get("reports", []):
        for sec in rep.get("sections", []):
            leg = sec.get("key")
            if leg in legs:
                sec["leg_flow"] = legs[leg]["flow"]
                sec["availability_source"] = "capability-matrix"
                if not legs[leg]["supplied_by_any"]:
                    # 当前没有任何平台能供这条腿：保留位置，给统一原因，
                    # 但**不清 metrics**——未来某平台接上同一段即可渲染。
                    sec["unavailable_reason_template"] = (
                        "不可得：当前无通道提供该数据腿（见能力矩阵 "
                        f"{', '.join(sorted({f'{p}.{legs[leg]['flow']}' for p in matrix.platform_keys()}))}）")

    preset["leg_capability"] = legs
    preset["metric_calibers"] = METRIC_CALIBERS
    preset["threshold_alerts"] = THRESHOLD_ALERTS
    preset["capability_source"] = {
        "path": "capability-matrix.json",
        "fingerprint": matrix.fingerprint(),
        "rule": "报表腿的可得性以矩阵为准；不可得腿保留位置并标原因，禁止置 0 或省略",
    }
    preset["_generated_from"] = {
        "generator": "scripts/gen_capability_artifacts.py",
        "fingerprint": matrix.fingerprint(),
    }
    return preset


def _status_phrase(status: str) -> str:
    return {
        STATUS_AVAILABLE: "可读",
        STATUS_UNAVAILABLE: "不提供",
        "unmapped": "需一次接入",
    }.get(status, "需一次接入")


#: 市场卡要 en/ja/es/fr 四语。**状态短语由矩阵生成**，不手写——手写的英文描述
#: 已经在本次改造里过期过一次（还写着「payouts 未覆盖」「走 mcp-cn-commerce」）。
#: 各平台的长原因保留在包内 capability-matrix.json（机器可读、可追溯），
#: 描述里只给「这一档是什么意思」，不硬翻中文原因（翻错比不翻更糟）。
I18N_PLATFORMS = {
    "en": {"shopify": "Shopify", "tiktok_shop": "TikTok Shop", "taobao": "Taobao",
           "tmall": "Tmall", "jd": "JD",
           "merchant_erp": "Merchant e-commerce ERP", "pinduoduo": "Pinduoduo"},
    "ja": {"shopify": "Shopify", "tiktok_shop": "TikTok Shop", "taobao": "Taobao",
           "tmall": "Tmall", "jd": "JD",
           "merchant_erp": "事業者向けEC ERP", "pinduoduo": "Pinduoduo"},
    "es": {"shopify": "Shopify", "tiktok_shop": "TikTok Shop", "taobao": "Taobao",
           "tmall": "Tmall", "jd": "JD",
           "merchant_erp": "ERP de comercio electrónico", "pinduoduo": "Pinduoduo"},
    "fr": {"shopify": "Shopify", "tiktok_shop": "TikTok Shop", "taobao": "Taobao",
           "tmall": "Tmall", "jd": "JD",
           "merchant_erp": "ERP e-commerce marchand", "pinduoduo": "Pinduoduo"},
}

#: 状态 → 一句话含义（四语）。
I18N_STATUS = {
    "en": {"available": "ready", "unmapped": "needs one onboarding",
           "unavailable": "not offered"},
    "ja": {"available": "利用可能", "unmapped": "初回接続が必要",
           "unavailable": "提供対象外"},
    "es": {"available": "listo", "unmapped": "requiere una conexión",
           "unavailable": "no disponible"},
    "fr": {"available": "prêt", "unmapped": "connexion à faire",
           "unavailable": "non proposé"},
}

I18N_NAME = {
    "en": "Cross-border e-commerce reconciliation connector pack",
    "ja": "越境EC照合コネクタパック",
    "es": "Pack de conectores para conciliación de comercio electrónico transfronterizo",
    "fr": "Pack de connecteurs pour la réconciliation e-commerce transfrontalière",
}

I18N_TAGS = {
    "en": ["ERP", "cross-border e-commerce", "reconciliation", "connector", "BYOK"],
    "ja": ["ERP", "越境EC", "照合", "コネクタ", "BYOK"],
    "es": ["ERP", "comercio electrónico transfronterizo", "conciliación", "conector", "BYOK"],
    "fr": ["ERP", "e-commerce transfrontalière", "réconciliation", "connecteur", "BYOK"],
}

#: 描述首尾（四语同一结构：前缀 + 逐平台状态 + 尾注）。
I18N_PREFIX = {
    "en": ("Connector pack for the cross-border e-commerce reconciliation template. "
           "Capability surface follows capability-matrix.json in this pack: "),
    "ja": ("越境EC照合テンプレート用コネクタパック。能力面は同梱の "
           "capability-matrix.json に従います："),
    "es": ("Pack de conectores para la plantilla de conciliación de comercio "
           "electrónico transfronterizo. La capacidad sigue a capability-matrix.json "
           "incluido en el pack: "),
    "fr": ("Pack de connecteurs pour le modèle de réconciliation e-commerce "
           "transfrontalière. La capacité suit capability-matrix.json inclus dans le "
           "pack : "),
}

I18N_SUFFIX = {
    "en": (" Per-platform reasons and review windows are recorded in "
           "capability-matrix.json. Credentials are injected by the connector runtime "
           "(BYOK), never stored or logged; zero writes to external systems. Real-store "
           "reads open per store under the crm-board ruling of 2026-10-30."),
    "ja": (" プラットフォーム別の理由と再確認期限は capability-matrix.json に記載。"
           "資格情報はコネクタランタイム側で注入（BYOK）し、保存もログ出力もしません。"
           "外部システムへの書き込みはゼロ。実店舗の読み取りは 2026-10-30 の "
           "crm-board 決定に従い店舗ごとに開放します。"),
    "es": (" Los motivos por plataforma y las ventanas de revisión están en "
           "capability-matrix.json. Las credenciales las inyecta el runtime del "
           "conector (BYOK), sin almacenarse ni registrarse; cero escrituras a sistemas "
           "externos. La lectura de tiendas reales se abre tienda por tienda según la "
           "decisión de crm-board del 2026-10-30."),
    "fr": (" Les motifs par plateforme et les fenêtres de revue figurent dans "
           "capability-matrix.json. Les identifiants sont injectés par le runtime du "
           "connecteur (BYOK), jamais stockés ni journalisés ; aucune écriture vers des "
           "systèmes externes. La lecture des boutiques réelles s'ouvre boutique par "
           "boutique selon la décision crm-board du 2026-10-30."),
}

I18N_SEPARATOR = {"en": "; ", "ja": "；", "es": "; ", "fr": " ; "}


def _i18n_block(matrix: CapabilityMatrix, pack: dict) -> dict:
    """四语 i18n：name/tags 是静态文案，description 逐平台由矩阵当前状态生成。"""
    block: dict[str, dict] = {}
    for lang in ("en", "ja", "es", "fr"):
        parts = []
        for key in matrix.platform_keys():
            cell = matrix.cells.get((key, "orders"))
            status = cell.status if cell else "unmapped"
            display = I18N_PLATFORMS[lang].get(key, key)
            parts.append(f"{display}: {I18N_STATUS[lang][status]}")
        # 保留非本生成器负责的键（如 publish-notes 写入的 i18n.<lang>.notes）——
        # 否则每次重生成都会把发布注记抹掉，两个生成器互相删除对方的结果。
        generated = {
            "name": I18N_NAME[lang],
            "description": (I18N_PREFIX[lang]
                            + I18N_SEPARATOR[lang].join(parts)
                            + I18N_SUFFIX[lang]),
            "tags": list(I18N_TAGS[lang]),
            "skills": {s["name"]: {"description": s.get(lang) or s.get("description", "")}
                       for s in pack.get("skills") or []
                       if s.get("name")},
        }
        existing = (pack.get("i18n") or {}).get(lang) or {}
        block[lang] = {**existing, **generated}
    return block


def generate_pack(matrix: CapabilityMatrix) -> dict:
    pack = json.loads(PACK_PATH.read_text(encoding="utf-8"))
    summary = _platform_status_summary(matrix)

    lines = []
    for key, info in summary.items():
        flows = info["flows"]
        orders = flows["orders"]
        if orders["status"] == STATUS_UNAVAILABLE:
            lines.append(f"{info['display_zh']}：不提供（{orders['reason']}）")
            continue
        readable = [f for f in ("orders", "settlements", "inventory")
                    if flows[f]["status"] == STATUS_AVAILABLE]
        if not readable:
            lines.append(f"{info['display_zh']}：需一次接入（{orders['reason']}）")
        else:
            missing = [f for f in ("orders", "settlements", "inventory")
                       if flows[f]["status"] != STATUS_AVAILABLE]
            tail = f"；缺 {', '.join(missing)}" if missing else ""
            lines.append(f"{info['display_zh']}：可读 {', '.join(readable)}{tail}")

    pack["capability"] = {
        "source": "capability-matrix.json",
        "fingerprint": matrix.fingerprint(),
        "platforms": summary,
        "statement": "；".join(lines),
    }
    pack["description"] = (
        "跨境电商对账模板·连接器 pack。能力面以包内 capability-matrix.json 为准："
        + "；".join(lines)
        + "。凭证 BYOK 经连接器运行时注入，零落库零日志；对外部系统零写入。"
          "真实店读写沿 crm-board 2026-10-30 裁决逐店开通。")
    pack["i18n"] = _i18n_block(matrix, pack)
    pack["_generated_from"] = {
        "generator": "scripts/gen_capability_artifacts.py",
        "fingerprint": matrix.fingerprint(),
    }
    return pack


def main() -> int:
    ap = argparse.ArgumentParser(prog="gen_capability_artifacts")
    ap.add_argument("--check", action="store_true",
                    help="只检查产物是否与矩阵同步（不同步退出码 1）")
    args = ap.parse_args()

    matrix = load_matrix(MATRIX_PATH)
    preset = generate_preset(matrix)
    pack = generate_pack(matrix)

    if args.check:
        stale = []
        for path, body in ((PRESET_PATH, preset), (PACK_PATH, pack)):
            current = json.loads(path.read_text(encoding="utf-8"))
            if current.get("_generated_from", {}).get("fingerprint") != \
                    body["_generated_from"]["fingerprint"]:
                stale.append(str(path.relative_to(REPO)))
        if stale:
            print("产物与矩阵不同步，请重跑 gen_capability_artifacts.py：\n  "
                  + "\n  ".join(stale))
            return 1
        print(f"产物与矩阵同步：{matrix.fingerprint()}")
        return 0

    _dump(PRESET_PATH, preset)
    _dump(PACK_PATH, pack)
    REVIEW_PENDING_PATH.write_text(render_review_pending(matrix), encoding="utf-8")
    print(f"已重生成：{PRESET_PATH.relative_to(REPO)}、{PACK_PATH.relative_to(REPO)}、"
          f"{REVIEW_PENDING_PATH.relative_to(REPO)}")
    print(f"矩阵指纹：{matrix.fingerprint()}（{matrix.summary()}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
