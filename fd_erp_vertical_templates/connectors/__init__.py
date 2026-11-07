"""连接器层（平台连接器归 add-erp-vertical-templates，内核 adapters.py 注明）。

铁律：
- 只读滩头：每个连接器只有 read 流拉取面，无任何外部写方法
  （boundary.py 机械化审计 connectors-readonly）。
- BYOK：凭证只经环境变量注入（ERP_BYOK_<NAME>，与内核同前缀纪律），
  零落库零日志。
- 如实守界：未适配/未支持面一律 NOT_WIRED 如实报出，不伪造自动接入。

模块：
- shopify.py      跨境 Shopify（复用已采纳 a1-x-tech/mcp-shopify-admin 的读写面，
                  本模板只用其读面：订单/收款(payouts)/库存流）
- cn_commerce.py  国内平台只读（mcp-cn-commerce，支持面以实测为准；拼多多不提供）
- pos.py          餐饮 POS BYOK 适配器框架（厂商插拔，逐店授权，导出文件兜底）
- payments.py     收款渠道商户侧适配（微信支付/支付宝/美团，实测可用面为准）
"""
