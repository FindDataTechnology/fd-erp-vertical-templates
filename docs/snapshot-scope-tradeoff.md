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

