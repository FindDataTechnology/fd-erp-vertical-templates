"""连接器层（平台连接器归 add-erp-vertical-templates，内核 adapters.py 注明）。

铁律：
- **出网单口**：所有商家数据读取经连接器运行时（`runtime_client`，`POST
  /v1/actions/<provider>.<action>`），模板层不拼 vendor URL、不发 HTTP、不读
  凭证。`tests/test_runtime_client.py` 的静态门在守这条（历史上有两条直连）。
- 只读滩头：每个连接器只有 read 流拉取面，无任何外部写方法
  （boundary.py 机械化审计 connectors-readonly）。
- BYOK：凭证由连接器运行时持有，模板层连读都不读；零落库零日志。
- 如实守界：未适配/未支持面一律 NOT_WIRED 如实报出，不伪造自动接入；
  「需一次接入」与「不提供」是两种话，措辞取能力矩阵。

模块：
- shopify.py       Shopify（provider `shopify_admin`：订单/库存/payout）
- tiktok_shop.py   TikTok Shop（provider `tiktok_shop`：订单/结算/库存）
- cn_commerce.py   国内平台状态翻译层（不再出网；读取留给后续 provider）
- merchant_erp.py  国内电商 ERP（provider `merchant_erp`：三腿，库存腿唯一来源）
- pos.py           餐饮 POS BYOK 适配器框架（厂商插拔，导出文件兜底）
- payments.py      收款渠道（provider `wechatpay`/`alipay`/`meituan` 结算流水）
- checkout.py      店面在线收款（provider `paypal`：create/capture，商家自有账户）。
                   **登记过的写面例外**：只读滩头纪律（boundary connectors-readonly）
                   管数据连接器；本模块经运行时对外写（PayPal 订单），写方法名刻意
                   不带 write/push 前缀，出网仍走 RuntimeClient 单口（payment 直连门）
- midplatform.py   中台文件服务（provider `file-service`：发票影像件直传/确认/列表）
                   与端内消息通道（provider `inapp-notify`：对账单推送留痕）
"""
