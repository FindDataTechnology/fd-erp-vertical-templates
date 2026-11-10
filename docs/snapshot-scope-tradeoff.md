# 事实快照的范围：全量 1575 项 vs 精选 8 项

> 状态：**待裁决**。当前仓里是全量（1575 项）。本文只出数据与推荐，不改动任何快照。
> 实测时间 2026-11-10 19:xx，机器：本机 macOS arm64，Python 3.14 venv。

## 0. 这两个方案到底在选什么

`tests/fixtures/connector-provider-facts.json` 是 CI 侧唯一可读的「provider 事实」来源：

- 本机有 `open-connector-mt` checkout 时，`provider_facts()` 走**实时扫描**（glob 目录 + 逐 provider 数测试文件），完全不读快照；
- CI 只有本仓，只能读**快照**。

于是快照的**范围**决定了两件事：仓库体积/每次刷新的 diff 量，以及「快照与实际目录的覆盖面对不对得上」这件事有没有被断言。

两种取材：

| 方案 | 内容 | 条目 |
|---|---|---|
| **全量**（现状） | 连接器目录里所有 provider | 1575 |
| **精选** | 只保留模板 `connector_bindings` 引用到的 provider | 8 |

模板当前引用的 8 个：`carrier-portal`、`cn-commerce`、`kingdee`、`pos`、`shopify_admin`、`supplier-portal`、`wechatpay`、`wms`。

## 1. diff 行数 / 体积（实测）

| 指标 | 全量 1575 | 精选 8 | 倍数 |
|---|---:|---:|---:|
| 文件行数 | 6307 | 36 | ×175 |
| 内容字节 | 118,346 B（115.6 KB） | 550 B（0.5 KB） | ×215 |
| 单次刷新 diff | **+6269 / −17**（本次提交实测） | 数行（仅被引用项变化时） | — |

一次「模板新增一个绑定」时两者的 diff 形态：

- 全量：新 provider 一项，**其余 1574 项不动**（键排序稳定）→ 约 2~3 行；
- 精选：新 provider 一项 → 约 2~3 行。

一次「连接器目录整体变化（上游新增/下线 provider）」时：

- 全量：**整表跟着动**，可能是数百到数千行；
- 精选：只有被引用的 8 项可能动 → 通常 0 行。

## 2. CI 耗时（实测，非估算）

**解析成本**（`json.loads` 单次，200 次中位）：

| | 全量 | 精选 |
|---|---:|---:|
| `json.loads` 中位 | 1.020 ms | 0.023 ms |

**`derive()` 全流程**（强制走快照分支，20 次中位）：

| | 全量 | 精选 |
|---|---:|---:|
| `derive()` | 4.30 ms | 1.25 ms |

**门端到端** `build_connector_status.py --check`（两份副本、均无兄弟仓、只差夹具，交错 11 次）：

| | 全量 | 精选 |
|---|---:|---:|
| 中位 | 58.89 ms | 54.43 ms |
| 均值 | 60.49 ms | 57.51 ms |
| 区间 | [54.5, 71.2] | [49.2, 69.5] |

**中位差 ≈ +4.5 ms/次**，其中约 1 ms 是 JSON 解析，其余是进程启动抖动。

**`pytest tests/test_connector_status.py`**（15 条，交错 5 次）：全量 575 ms / 精选 400 ms，但全量区间 [413, 840]，方差大于差值本身——**不作为结论依据**，量级相同（数百 ms，主导项是 pytest 启动）。

**结论**：CI 耗时的真实差异在**毫秒级**，相对于整套 `check_all`（十几秒）可忽略。选范围不该拿耗时当理由。

## 3. 关键差异不是性能，是「有没有被断言」

精选方案的原始风险：模板绑定了一个不在快照里的 provider，CI 会把它派生成「目录里没有这个 provider」——把**不知道**写成**不存在**，而门照绿。

这个风险**现在已经被堵住了**：`build_connector_status.py` 新增 `uncovered_bindings()`，在 `derive()` 里断言「快照必须覆盖模板引用的每个 provider」，缺一即 `problems`（门红），并点名缺哪个、给重刷命令。配套 3 条用例（正向基线 / 删一项必红 / 快照整体缺失只报「事实不可得」），负例实测 `--check` 退出码 1。

也就是说：

- 选**精选** → diff 小，且漏项会被门拦住（不会静默）；
- 选**全量** → diff 大，漏项在结构上不可能（但每次刷新都产生数千行噪声，且包含 1567 个与本仓无关的 provider）。

两者现在**安全性等价**，差别只剩 diff 噪声与「快照里有没有与本仓无关的信息」。

## 4. 推荐

**推荐改回精选（8 项 + 随绑定增长）。**

理由，按重要性：

1. **安全性已不再由范围保证，而由断言保证**。当初选全量是为了堵静默漏项，这条已被 `uncovered_bindings()` 接住；继续用全量承担 diff 噪声，换不到额外的保障。
2. **diff 可读性**：6269 行的刷新 diff 会让「这次到底改了什么」无法在 review 里看出；精选下同类改动是数行。
3. **快照的语义更准**：这份文件回答的是「CI 需要相信哪些 provider 的事实」，不是「目录里有谁」。1567 个无关条目是噪声，且它们一变就让 diff 变脏。
4. 性能差异（毫秒级）两案都可接受，不作为依据。

若采纳，落地动作是改 `refresh_facts_fixture()` 只写被引用 provider（保持「用刷新入口、不手改生成物」的纪律），并让新断言继续兜底；快照本身随后重刷一次即可。

**保留全量的唯一情形**：如果将来打算让这份快照承担「目录清单的离线副本」职责（例如 CI 里要校验目录增删），那它就该是全量。目前它没有这个职责。

## 5. 复现方法

```bash
# 行数/字节
wc -l -c tests/fixtures/connector-provider-facts.json
```

`derive()` 的耗时必须在**同一个分支**上比才有意义：本机有连接器仓时默认走实时扫描
（实测约 102 ms，主导项是 glob 1575 个目录），而 CI 走的是快照分支。所以复现时要把
`CONNECTOR_REPO` 指到不存在的路径，强制走快照——本文第 2 节的 4.30 / 1.25 ms 就是这样测的：

```python
import importlib.util, json, pathlib, timeit
spec = importlib.util.spec_from_file_location("bcs", "scripts/build_connector_status.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

full = pathlib.Path("tests/fixtures/connector-provider-facts.json")
data = json.loads(full.read_text(encoding="utf-8"))
ref = sorted({b["provider"] for b in m.bindings() if b.get("provider")})
curated = pathlib.Path("curated.json")
curated.write_text(json.dumps(
    {"providers": {k: data["providers"][k] for k in ref}}, ensure_ascii=False, indent=1) + "\n",
    encoding="utf-8")

def in_snapshot_mode(path):
    m.CONNECTOR_REPO = pathlib.Path("/nonexistent-xyz")   # 强制走快照分支
    m.FACTS_FIXTURE = path
    return timeit.timeit(m.derive, number=20) / 20 * 1000

print("full   ", round(in_snapshot_mode(full), 2), "ms")
print("curated", round(in_snapshot_mode(curated), 2), "ms")
```

门端到端（`--check`）与 pytest 的对比都在**两份仓副本**上做，且两份都必须没有兄弟仓
（否则一侧走实时、另一侧走快照，量到的就不是夹具差异）。交错采样若干轮取中位，
避免机器漂移被记到夹具头上。

精选版夹具的生成方式（不落盘到仓内，仅用于对比）：

```python
ref = sorted({b["provider"] for b in m.bindings() if b.get("provider")})
json.dump({"providers": {k: full["providers"][k] for k in ref}}, ...)
```


## 5.1 补丁方案：改回精选（**未提交**，供评审）

> 本节只是补丁文本，**没有应用到仓库**；快照当前仍是全量 1575 项。
> 已在工作区同级的临时副本（`.probe-curated`，跑完即删）里完整验证过，结果见 5.2。

### 改动 A：`scripts/build_connector_status.py` —— 刷新入口只写被引用项

只改 `refresh_facts_fixture()`：把「遍历目录全部 provider」换成「遍历模板绑定」。

```diff
 def refresh_facts_fixture() -> int:
-    """从实时连接器仓重写事实快照（本地刷新用；CI 不调用）。"""
-    providers = sorted(p.stem for p in (CONNECTOR_REPO / "catalog" / "apps").glob("*.json"))
-    facts = {
-        p: {"catalog": True, "executor_tests": executor_test_count(p)} for p in providers
-    }
-    # 绑定里提到的、但目录没有的角色名也要留一行，否则快照会"看起来都齐"
-    for b in bindings():
-        facts.setdefault(str(b["provider"]), {"catalog": False, "executor_tests": 0})
+    """从实时连接器仓重写事实快照（本地刷新用；CI 不调用）。
+
+    只写**模板已引用**的 provider：这份快照回答的是「CI 要相信哪些 provider 的事实」，
+    不是「目录里有谁」。覆盖不全由 `uncovered_bindings()` 在派生时断言兜住。
+    """
+    catalog = {p.stem for p in (CONNECTOR_REPO / "catalog" / "apps").glob("*.json")}
+    facts = {}
+    for b in bindings():
+        provider = b.get("provider")
+        if not provider or provider in facts:
+            continue
+        facts[str(provider)] = {
+            "catalog": provider in catalog,
+            "executor_tests": executor_test_count(provider) if provider in catalog else 0,
+        }
```

保留原样、**不要动**的部分：`body` 里的 `_comment`（仍说明刷新命令）、`source`、`count`
（`count` 自动等于 `len(facts)`，会随之为 8）、`sort_keys=True`。

### 改动 B：快照文件本身

不手改。落地时跑一次 `python3 scripts/build_connector_status.py --refresh-fixture`，
由改动 A 之后的新逻辑重写整份文件。等价效果（**要删的键**）：

- 删除：当前 `providers` 中除下列 8 个之外的全部键，共 **1567 个**；
- 保留 8 个（`count` 字段随之改为 8）：
  `carrier-portal`、`cn-commerce`、`kingdee`、`pos`、`shopify_admin`、
  `supplier-portal`、`wechatpay`、`wms`。
- 文件形态：6307 行 / 118,346 B → **39 行 / 887 B**（实测，见 5.2）。

这 8 个键的来源是 `bindings()`（读 `templates/*/connector-pack/pack.json#connector_bindings`），
不是手抄清单——新增绑定后重刷即自动纳入。

### 改动 C：三条用例的对应关系（**无需修改**，但它们是为这一刻写的）

| 用例 | 在全量下 | 改精选后 | 它守的是什么 |
|---|---|---|---|
| `test_snapshot_covers_every_provider_the_templates_reference` | 通过（1575 ⊇ 8） | 通过（8 ⊇ 8，**恰好相等**） | 正向基线：引用集 ⊆ 快照集；口径与派生同源（都取 `bindings()`） |
| `test_missing_snapshot_coverage_breaks_the_gate` | 通过（删一项即点名） | 通过（**这条才是精选的安全网**） | 机制：快照少一个被引用 provider → `uncovered_bindings()` 点名 + `derive()` 报 problem |
| `test_missing_snapshot_entirely_reports_unavailable_not_missing_coverage` | 通过 | 通过 | 快照整体缺失时只报「事实不可得」，不重复刷覆盖面（避免噪音双报） |

三条用例都只依赖 `bindings()` 与 `FACTS_FIXTURE`，不依赖快照规模，所以改动 A/B 不需要动测试。
换句话说：**精选方案的安全性由第 2 条用例 + `derive()` 里的断言保证，不由快照范围保证**。

### 5.2 验证结果（临时副本，未提交）

在 `finddata/.probe-curated`（真仓的完整副本，位置保证能看到 `../open-connector-mt`）里：

1. 应用改动 A → `--refresh-fixture` 输出 `wrote …（8 个 provider）`，文件 **39 行 / 887 B**；
2. `check_all`：**15 道门全绿**；
3. `build_connector_status.py --check`：**退出码 0**，`OK: 三态与派生一致；连接 0 / 未接通 9 / 501 挂账 20`；
4. `pytest tests/test_connector_status.py`：**15 passed**；
5. 负例：从精选快照删掉 `kingdee`（剩 7 项）→ `--check` **退出码 1**，点名
   `事实快照缺模板已引用的 provider：kingdee`；还原后退出码 0。

结论：改精选不会削弱任何现有断言，漏项仍被门拦住。
