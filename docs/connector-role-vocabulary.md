# 连接器角色词表（受控词汇）

> 角色名是**跨包的公共词汇**：`connector_role_mappings` 以 `(email, role)` 为键，同一个角色
> 在餐饮包里叫 `pos`、在下一个行业包里也必须叫 `pos`，否则客户要为同一件事回答两次。
> 所以这里是一张**受控词表**——新增角色必须同时改两处：本文件的表 + `tests/test_connector_binding_providers.py`
> 的 `ROLE_PROVIDERS` 登记表。任何一处漏了，`pytest` 会红并点名。

## 命名规则

1. **kebab-case，全小写**（`^[a-z][a-z0-9]*(-[a-z0-9]+)*$`）：`supplier-portal` 对，`supplierPortal` / `Supplier_Portal` 错。
2. **写"系统类别"，不写厂商**：`wms` 对，`wangdian` 错——厂商是客户在自己连接器里选的东西，写进包里就等于替客户选。
3. **撞名要当场处理**：角色名不得等于连接器目录里真实存在的 service id。目录以后新增同名 provider 时，
   `test_role_names_do_not_collide_with_catalog_ids` 会红，逼我们在"改角色名"和"改真 id"之间做决定，
   而不是让语义悄悄分叉。
4. **名实相符**：角色名应与本仓 `fd_erp_vertical_templates/connectors/` 里那条连接器车道同名
   （`pos.py` → `pos`、`cn_commerce.py` → `cn-commerce`），排障时不必在两个词之间翻译。

## 词表

| 角色 | 含义 | 对应连接器车道 | 声明方（包） | 客户可选的系统 |
| --- | --- | --- | --- | --- |
| `wms` | 仓库管理系统 | 无同名 provider（按客户连接） | supply-chain `wms-snapshot` | 自己已连通、且操作面覆盖声明类的 WMS |
| `supplier-portal` | 供应商门户 / SRM | 无同名 provider | supply-chain `supplier-portal` | 自己的供应商门户系统 |
| `carrier-portal` | 承运商门户 / TMS | 无同名 provider | supply-chain `carrier-portal` | 自己的承运商（目录有 sf_express / kuaidi100 可作其一） |
| `pos` | 门店收银 / POS | `pos.py` | restaurant-chain `pos-sales` | 自己实际在用的 POS 厂商 |
| `cn-commerce` | 国内电商平台（淘宝/天猫/京东） | `cn_commerce.py`（**状态翻译层，不出网**）+ 后续实名 provider（`taobao`/`jd`/`merchant_erp`） | crossborder `cn-commerce-store` | 自己开店的平台与账号（拼多多不声明：上游 schema 缺失）。措辞取能力矩阵：`unmapped`=需一次接入，`unavailable`=不提供 |

## 不是角色、别登记的东西

- **真实厂商 / 目录 service id**：`kingdee`、`shopify`、`wechatpay`、`meituan`、`wangdian`、
  `sf_express` 等——这些直接写在 `provider` 里，不加 `providerRole`。客户能不能用取决于
  "他有没有连这个厂商"，而不是"他选哪个厂商"。
- **业务动作 / 流程名**：`reconcile`、`daily-close` 这类是能力面，不是外部系统类别。
- **角色加区域之外的自造词**：如果将来要按区域分平台，用后缀（`marketplace-cn`）而不是另起一套
  （`taobao-system`），否则同一件事会有三个名字。

## 什么时候该关掉一条登记

两条路，选其一并改本文档：

1. **改真 id**：目录里有了能覆盖该面的 provider，声明方换成真 id、删掉登记；
2. **不再需要**：没有任何包再声明该角色——`test_role_provider_registry_entries_carry_reason_and_decision`
   与"登记不许腐坏"会强制删除过期条目，词表同步删行。
