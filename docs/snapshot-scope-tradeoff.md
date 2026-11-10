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
下面是**真实生成**的完整 diff（由临时 clone 里改完 `git diff` 得到，非手写），
已在真仓用 `git apply --check` 验证可干净套用。

```diff
diff --git a/scripts/build_connector_status.py b/scripts/build_connector_status.py
index 1c83dce..0e86c29 100644
--- a/scripts/build_connector_status.py
+++ b/scripts/build_connector_status.py
@@ -79,14 +79,21 @@ def provider_facts() -> tuple[dict[str, dict], str]:
 
 
 def refresh_facts_fixture() -> int:
-    """从实时连接器仓重写事实快照（本地刷新用；CI 不调用）。"""
-    providers = sorted(p.stem for p in (CONNECTOR_REPO / "catalog" / "apps").glob("*.json"))
-    facts = {
-        p: {"catalog": True, "executor_tests": executor_test_count(p)} for p in providers
-    }
-    # 绑定里提到的、但目录没有的角色名也要留一行，否则快照会"看起来都齐"
+    """从实时连接器仓重写事实快照（本地刷新用；CI 不调用）。
+
+    只写**模板已引用**的 provider：这份快照回答的是「CI 要相信哪些 provider 的事实」，
+    不是「目录里有谁」。覆盖不全由 `uncovered_bindings()` 在派生时断言兜住。
+    """
+    catalog = {p.stem for p in (CONNECTOR_REPO / "catalog" / "apps").glob("*.json")}
+    facts: dict[str, dict] = {}
     for b in bindings():
-        facts.setdefault(str(b["provider"]), {"catalog": False, "executor_tests": 0})
+        provider = b.get("provider")
+        if not provider or str(provider) in facts:
+            continue
+        facts[str(provider)] = {
+            "catalog": str(provider) in catalog,
+            "executor_tests": executor_test_count(str(provider)) if str(provider) in catalog else 0,
+        }
     body = {
         "_comment": (
             "open-connector-mt 的 provider 事实快照。CI 只有本仓 checkout，读不到连接器仓；"
```

落地步骤（两行）：

```bash
# 1) 把上面 fenced diff 原样存成文件（编辑器另存即可），然后校验它确实匹配当前源码
git apply --check /tmp/curate-facts-fixture.patch

# 2) 套用
git apply /tmp/curate-facts-fixture.patch
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

### 5.3 落地顺序与回滚命令（裁决通过后照此执行）

**落地（4 步，顺序不能换）**

```bash
cd fd-erp-vertical-templates

# 0) 先备份快照（回滚要用；不依赖 git 操作）
cp tests/fixtures/connector-provider-facts.json /tmp/facts-full.bak

# 1) 套用改动 A（脚本必须先改，否则第 2 步又写回全量）
git apply --check /tmp/curate-facts-fixture.patch && git apply /tmp/curate-facts-fixture.patch

# 2) 用刷新入口重写快照（只写被引用的 8 项；这一步会删掉其余 1567 个键）
python3 scripts/build_connector_status.py --refresh-fixture

# 3) 派生状态面 + 跑门（顺序同 check_all：先真源后派生）
python3 scripts/build_connector_status.py
python3 scripts/check_all.py
```

预期：第 2 步输出 `wrote …（8 个 provider）`；快照 `39 行 / 887 B`；
`check_all` 15 道门全绿；`集成 0 连接 / 9 未接通 / 501 挂账 20` 不变。

**回滚（任一步出错都能整体退回，不用 `git checkout --`）**

```bash
# 代码：逆向套用同一份 patch（-R 就是 revert，不会碰其它文件）
git apply -R /tmp/curate-facts-fixture.patch

# 快照：用第 0 步的备份覆写回去
cp /tmp/facts-full.bak tests/fixtures/connector-provider-facts.json

# 重点：快照回滚后必须重新派生状态面，否则 connector-status.json 仍是精选下算出来的
python3 scripts/build_connector_status.py
python3 scripts/check_all.py      # 应回到全量下的绿
```

两点注意（都是踩过的）：

1. **别用 `git checkout -- <file>` / `git restore`** 做回退——它们会连同别人未提交的改动一起丢掉。用 `git apply -R` 与 `cp` 备份。
2. 回滚只还原文件是**不够**的：`connector-status.json` 是派生件，必须重跑派生，否则会留下「精选下算出的状态」配「全量快照」的混合态，`对接三态` 门会以「与派生不一致」报红——那是真实的不一致，不是抖动。

**只改快照不改脚本的后果**（说明为什么 A 与 B 必须同批）：下次任何人跑 `--refresh-fixture`，又会把 1575 项写回来，diff 白改一次。

### 5.4 5.3 的演练结果（2026-11-10，临时副本 `.probe-flow`，跑完已删）

不是只做语法检查——按 5.3 的顺序**原样执行了一遍**，且 patch 是**从本文档的 fenced 块里抽出来**的
（证明文档里的 diff 可被提取、可直接套用，不是装饰）：

| 步骤 | 命令 | 结果 |
|---|---|---|
| 抽 patch | 从本文 ```` ```diff ```` 块提取 | 33 行，`git apply --check` 通过 |
| 0 备份 | `cp … /tmp/facts-full.bak` | ok |
| 1 套用 | `git apply --check && git apply` | 已套用 |
| 2 刷新 | `--refresh-fixture` | `wrote …（8 个 provider）`；**39 行 / 887 B** |
| 3 派生+门 | `build_connector_status.py` + `check_all` | 15 道门**全绿** |
| 回滚·代码 | `git apply -R` | 已逆回滚 |
| 回滚·快照 | `cp /tmp/facts-full.bak …` | 回到 **6307 行** |
| 回滚·重派生 | `build_connector_status.py` + `check_all` | 15 道门**全绿** |
| 回滚后工作树 | `git status --porcelain` | 仅剩本文档差异（代码与快照均回到 HEAD 状态） |

结论：落地与回滚都是可执行的、可整体退回的；回滚不依赖 `git checkout --`。
