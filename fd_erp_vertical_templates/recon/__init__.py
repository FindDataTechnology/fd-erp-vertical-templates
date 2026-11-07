"""对账驱动（erp-vertical-crossborder / restaurant-chain 任务 2.3/2.4/3.4/3.6）。

模块：
- three_way.py   三方流逐笔对齐内核（四类差异 + 双侧原始引用，逐店隔离）
- crossborder.py 跨境对账驱动（订单-收款-库存 + 多币种 + 平台费用维度）
- restaurant.py  餐饮门店对账与日结驱动（POS-收款-外卖结算 + 日报三段）
- shares.py      分成对账单与核对指令输出（零资金执行面）
- writeback.py   对账结论回写（内核白名单车道：proposal 待确认 + 资金类禁写）
- kingdee.py     金蝶结算对账腿（凭证推送网关契约 + 日终双边对账；锚点实例）
"""
