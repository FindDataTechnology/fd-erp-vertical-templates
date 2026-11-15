"""国别包数据目录（`localization/cn/*.json`）。

**为什么这里需要一个 `__init__.py`（2026-11-15 生产事故的修复）**：
`fd_erp_vertical_templates/localization.py` 用
`BASE = Path(__file__).resolve().parents[1] / "localization" / "cn"` 定位数据包。
这个路径在**源码树**里成立（`localization/` 与包目录同级），但此前
`[tool.setuptools.packages.find] include = ["fd_erp_vertical_templates*"]`
只把包目录装进 site-packages，仓根的 `localization/` 不算包、也没有 package-data，
于是**装进镜像后** `parents[1]` 是 `site-packages`、`site-packages/localization/cn`
不存在。

现场症状（2026-11-15，`fd-erp-vertical-api` 换片）：
`create_app → build_drivers → _cn_pack` 抛
`TemplateError: [INVALID_ARGUMENT] ERP_CN_CHART … 国别包缺文件：manifest.json`，
容器 6 次重启、`BackOff`，服务整体不可用；而本仓 1040 条测试与 api 360 条测试
**全绿**——它们跑在源码树里，同一个 `parents[1]` 恰好指向仓根。

修法：把本目录声明为一个包并带 package-data（见 `pyproject.toml` 的
`packages.find.include` 与 `package-data`），这样 `localization/cn/*.json` 会随
wheel 装到 `site-packages/localization/cn/`，两种布局下同一个表达式都成立。
`tests/test_localization_ships_with_package.py` 用**真造一个 wheel** 来钉住这件事：
只测源码树里能读到，等于没测。
"""
