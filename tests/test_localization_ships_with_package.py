"""国别包必须**随包发布**（2026-11-15 生产事故的回归钉）。

事故形态（`fd-erp-vertical-api` 换片，服务整体不可用 8 分钟）：

    drivers.py:246 build_drivers → _cn_pack(settings)
    TemplateError: [INVALID_ARGUMENT] ERP_CN_CHART … 国别包缺文件：manifest.json
    ⇒ 容器 6 次重启、BackOff

根因不是数据丢了，是**路径假设只在源码树里成立**：
`fd_erp_vertical_templates/localization.py` 用
`BASE = Path(__file__).resolve().parents[1] / "localization" / "cn"`。源码树里
`parents[1]` 是仓根，`localization/` 就在那儿；`pip install` 之后 `parents[1]` 是
site-packages，而 `localization/` 当时既不是包、也没有 package-data ⇒ 不进 wheel。

**为什么本仓 1040 条测试一条都没拦住**：它们全都跑在源码树里，同一个 `parents[1]`
恰好指向仓根，数据永远在。所以这里刻意分两层：

1. `test_pyproject_declares_localization_as_package`——读配置（永远跑，毫秒级）；
2. `test_wheel_carries_localization_pack`——**真造一个 wheel**，装到临时目录，
   用子进程 `PYTHONPATH=<临时 site>`（cwd 也换成临时目录，确保仓内源码树不可见）
   加载 `CnPack()` 并通过 `verify()`。这一段复刻的正是镜像里的布局，
   也就是事故现场；只断言「源码树里读得到」等于什么都没测。

第 2 层需要 setuptools ≥68 与 wheel（本仓 dev extras 里已声明）。没有它们时
**跳过而不是假装通过**，跳过原因写清装哪两个包——静默失败正是这次事故的形态。
"""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PACK = REPO / "localization" / "cn"
WHEEL_PREFIX = "localization/cn/"


def _pyproject() -> dict:
    """读 pyproject.toml。

    用 tomllib（3.11+ 标准库）：本仓 requires-python >=3.11，不引额外依赖。
    """
    import tomllib

    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))


def test_pyproject_declares_localization_as_package() -> None:
    """配置层：`localization` 是包，且它的 `cn/` 数据是 package-data。

    这条只是地板——它证明「有人写了这两行」，不证明 setuptools 真把它们装进去了
    （那是下一条的事）。但少了它，下一条一旦因缺 setuptools 跳过，就再没有别的东西
    提醒「配置被谁删了」。
    """
    cfg = _pyproject()
    include = ((cfg.get("tool", {}).get("setuptools", {})
                .get("packages", {}).get("find", {}).get("include")) or [])
    assert any(pat.startswith("localization") for pat in include), (
        f"[tool.setuptools.packages.find].include 里没有 localization*：{include}。"
        "国别包目录不是包，就不会进 wheel——镜像里 BASE 指向的目录不存在。")

    data = (cfg.get("tool", {}).get("setuptools", {})
            .get("package-data") or {}).get("localization") or []
    assert any(pat.endswith(".json") for pat in data), (
        f"[tool.setuptools.package-data].localization 没声明 json 数据：{data}。"
        "声明成包但数据不带，wheel 里只有 __init__.py，症状与事故完全一样。")

    # 目录本身必须真是包（有 __init__.py），否则上面的 include 通配匹配不到。
    assert (REPO / "localization" / "__init__.py").is_file(), (
        "localization/ 缺 __init__.py：setuptools 的 packages.find 不会把它当包。")


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> Path:
    """造一次 wheel（模块级缓存，别每条用例都构建一遍）。

    **必须在干净副本里构建，就地构建会给出假绿**（本用例第一版就是这么错的）：
    `bdist_wheel` 打包的是 `build/lib`，而 `build/lib` 是增量目录——上一次构建
    留下的 `build/lib/localization/cn/*.json` 会被原样打进去。实测把 pyproject
    退回旧配置（`include` 只有 `fd_erp_vertical_templates*`）后，就地构建出来的
    wheel 里**仍然有**国别包，用例照样全绿，而真实 sdist 里早已没有。
    只复制源码（不复制 `build/`、`*.egg-info`、`.venv`、`__pycache__`）就断掉了
    这条污染路径。
    """
    pytest.importorskip("setuptools", reason="需要 setuptools>=68 才能构建 wheel")
    pytest.importorskip("wheel", reason="需要 wheel 才能构建 wheel")

    import shutil

    src = tmp_path_factory.mktemp("src")
    shutil.copytree(
        REPO, src / "pkg",
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "build", "dist", "*.egg-info", "__pycache__",
            ".pytest_cache", ".mypy_cache", "coverage", "tests", "scripts"),
        dirs_exist_ok=True)

    out = tmp_path_factory.mktemp("wheelhouse")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps",
         "--no-build-isolation", "-w", str(out)],
        cwd=src / "pkg", capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, (
        "构建 wheel 失败（这不等于代码坏，但必须先修好才能判国别包）：\n"
        f"stdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}")
    found = sorted(out.glob("*.whl"))
    assert found, f"pip wheel 退出 0 但没有产物：{out}"
    return found[0]


def test_wheel_carries_localization_pack(wheel: Path) -> None:
    """wheel 里必须有整份国别包——一个数据文件都不能少。"""
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())

    manifest = json.loads((PACK / "manifest.json").read_text(encoding="utf-8"))
    wanted = sorted({"manifest.json", *manifest.get("files", {})}) if isinstance(
        manifest.get("files"), dict) else ["manifest.json"]
    missing = [w for w in wanted if WHEEL_PREFIX + w not in names]

    assert not missing, (
        f"wheel 里缺国别包文件：{missing}\n"
        f"wheel 内 {WHEEL_PREFIX} 下实际只有："
        f"{sorted(n for n in names if n.startswith(WHEEL_PREFIX))}\n"
        "这就是 2026-11-15 事故：镜像里 `parents[1]/localization/cn` 缺文件，"
        "启动即 ERP_CN_CHART … 国别包缺文件。")

    # 仓里每一份 .json 都得在 wheel 里：manifest 的 files 只列数据卷，漏掉的
    # 形态（比如将来多加一份国别包）恰好不会被上一条判据看见。
    on_disk = sorted(p.name for p in PACK.glob("*.json"))
    absent = [name for name in on_disk if WHEEL_PREFIX + name not in names]
    assert not absent, f"仓内这些 .json 没进 wheel：{absent}"


def test_installed_layout_loads_pack(wheel: Path, tmp_path: Path) -> None:
    """真装真加载：抽包到临时 site，子进程 `CnPack()` —— 复刻镜像里的那一刻。

    cwd 换成**空目录**、只给 PYTHONPATH：仓内源码树不可见，`parents[1]` 只能指向
    临时 site（与 site-packages 同形）。若打包没修好，这里就是 AttributeError /
    NOT_FOUND「国别包缺文件」，与生产栈顶一字不差。
    """
    site = tmp_path / "site"
    site.mkdir()
    with zipfile.ZipFile(wheel) as zf:
        zf.extractall(site)

    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, sys\n"
        "from fd_erp_vertical_templates.localization import BASE, CnPack\n"
        "pack = CnPack()\n"                       # 默认 verify_on_load=True：连 sha 一起校验
        "print(json.dumps({'base': str(BASE), 'accounts': len(pack.accounts()),\n"
        "                  'tax': len(pack.tax_codes()),\n"
        "                  'schema': pack.verification.get('schema')}))\n",
        encoding="utf-8")

    empty = tmp_path / "cwd"
    empty.mkdir()
    proc = subprocess.run([sys.executable, str(probe)], cwd=empty,
                          capture_output=True, text=True, timeout=300,
                          env={"PYTHONPATH": str(site), "PATH": "/usr/bin:/bin"})
    assert proc.returncode == 0, (
        "从**安装后布局**加载国别包失败——这正是 2026-11-15 生产事故的形态：\n"
        f"stdout:\n{proc.stdout[-3000:]}\nstderr:\n{proc.stderr[-4000:]}")

    info = json.loads(proc.stdout.strip().splitlines()[-1])
    assert info["base"].startswith(str(site)), (
        f"BASE 没落在安装目录里（{info['base']}），这条用例失去了意义")
    assert info["accounts"] == 156, f"科目条数不对：{info['accounts']}"
    assert info["tax"] == 62, f"税码条数不对：{info['tax']}"
