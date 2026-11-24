# 待补复核清单（能力矩阵）

> 由 `scripts/gen_capability_artifacts.py` 从 `capability-matrix.json` 生成，请勿手改（矩阵指纹 `sha256:d5c42757a03ef3044b33f2ea74ebeaabb96cd14919d41c8f3d8c2a643e9a7e59`）。
> 生成时刻的矩阵状态：{'available': 5, 'unavailable': 5, 'unmapped': 22}。

## 未接通（unmapped）：22 格

| 平台 | 流 | 原因 | 下一步 |
|---|---|---|---|
| jd | inventory | 骨架待锚点客户 | 在连接器目录立 provider 并实现只读动作 |
| jd | orders | 骨架待锚点客户：目录无 provider | 在连接器目录立 provider 并实现只读动作 |
| jd | refunds | 骨架待锚点客户 | 在连接器目录立 provider 并实现只读动作 |
| jd | settlements | 骨架待锚点客户 | 在连接器目录立 provider 并实现只读动作 |
| merchant_erp | inventory | 通道未实现；国内三方对账缺此腿不成立 | 在连接器目录立 provider 并实现只读动作 |
| merchant_erp | orders | 通道未实现；国内库存腿的唯一来源，优先实现 | 在连接器目录立 provider 并实现只读动作 |
| merchant_erp | refunds | 通道未实现 | 在连接器目录立 provider 并实现只读动作 |
| merchant_erp | settlements | 通道未实现；取决于所选厂商是否开放结算面 | 在连接器目录立 provider 并实现只读动作 |
| own_channel | refunds | 退款是资金动作，平台二清红线不碰；售后申请只记流程记录。真要退钱走商家自有支付通道，平台无动作面 | 在连接器目录立 provider 并实现只读动作 |
| shopify | refunds | 退款作为独立流尚未在模板层消费；当前由订单状态与差异类型覆盖 | 在连接器目录立 provider 并实现只读动作 |
| taobao | inventory | 骨架待锚点客户 | 在连接器目录立 provider 并实现只读动作 |
| taobao | orders | 骨架待锚点客户：目录无 provider，按 ADR 0002 先有客户再实现 | 确认承载通道是否已部署，再补证据 |
| taobao | refunds | 骨架待锚点客户 | 在连接器目录立 provider 并实现只读动作 |
| taobao | settlements | 骨架待锚点客户 | 在连接器目录立 provider 并实现只读动作 |
| tiktok_shop | inventory | 通道未实现 | 在连接器目录立 provider 并实现只读动作 |
| tiktok_shop | orders | 通道未实现：全仓仅见一句文案，无 provider、无适配器 | 在连接器目录立 provider 并实现只读动作 |
| tiktok_shop | refunds | 通道未实现 | 在连接器目录立 provider 并实现只读动作 |
| tiktok_shop | settlements | 通道未实现 | 在连接器目录立 provider 并实现只读动作 |
| tmall | inventory | 骨架待锚点客户（与淘宝共用同一 provider） | 在连接器目录立 provider 并实现只读动作 |
| tmall | orders | 骨架待锚点客户（与淘宝共用同一 provider） | 在连接器目录立 provider 并实现只读动作 |
| tmall | refunds | 骨架待锚点客户 | 在连接器目录立 provider 并实现只读动作 |
| tmall | settlements | 骨架待锚点客户（与淘宝共用同一 provider） | 在连接器目录立 provider 并实现只读动作 |

## 口径纪律

- `unmapped` = 可接未接；对外说「需一次接入」，不说「不支持」。
- `unavailable` = 确认拿不到；对外说「不提供」，不给接入日期。
- 升格为可得要两半证据：provider 动作存在 + 一次真实读取记录。
