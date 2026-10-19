# 七表关系与连接约束

机器数据规则 ID：`DATA-001` 要求事件去重，`DATA-002` 要求先聚合 request 再连接一对多明细，`DATA-003` 要求按历史有效区间唯一匹配，`DATA-004` 要求先检查加载完整性；本文件只解释连接关系，不复制阈值。

字段类型、空值含义和枚举以 [field-dictionary.yaml](field-dictionary.yaml) 为准；本说明只描述粒度、关系和防重复要求。既有业务数据库只有下列七张表，不新增虚构数仓表。

| 表 | 粒度 | 主键/连接重点 |
| --- | --- | --- |
| `queries` | 一个 Query 词条 | `query_id`；连接 `search_requests.query_id` |
| `search_requests` | 一次搜索请求 | `request_id`；按时间、城市、区域、场域和 `is_valid` 过滤 |
| `recall_events` | 一次阶段事件投递 | `delivery_id` 为行标识，`event_id` 是可能重复投递的业务事件；连接 `request_id` |
| `historical_candidate_replay` | 一次历史词法候选回放 | `replay_id`；连接 `request_id`、`term_id`、`ad_plan_id`、`offer_id` |
| `historical_eligibility` | 一个候选在一段有效区间内的历史资格快照 | `snapshot_id`；按候选、城市和 `[valid_from, valid_to)` 连接 |
| `ad_events` | 一次广告业务事件 | `event_id`；通过 `request_id` 连接请求，按事件类型统计 |
| `load_manifest` | 一个数据集、日期、城市的加载状态 | 复合主键 `dataset + biz_date + city_id`；用于判断窗口是否完整 |

## 关系

`queries 1:N search_requests`；`search_requests 1:N recall_events/historical_candidate_replay/ad_events`。历史回放通过候选标识连接 `historical_eligibility`；term 到计划的关系已经体现在回放或索引来源中，不能把当前 term—计划映射直接当成历史版本。

当前 API 的 `sku_id → offer → store → ad_plan` 关系属于当前事实链，不与历史资格表混用。一个 SKU 可以有多个 offer，一个 offer 可以有多个计划；连接后必须保留候选主键，不能按 SKU 直接去重掉门店或计划。

## 查询顺序与对账

1. 先在 `search_requests` 过滤任务范围并保留一行一个 request。
2. 对 `recall_events` 按 `event_id` 去重，再按 request 聚合阶段事实。
3. 对历史回放和历史资格按候选及有效时间区间连接；如果一个候选匹配多个或零个适用区间，都记录数据缺口，不任意去重或选择最近一条。
4. 先在 request 粒度判断“有合格候选/无合格候选/未知”，再统计首个失败阶段。
5. 检查 `load_manifest` 覆盖所有窗口日期和城市；`missing` 表示数据不完整，不代表当天没有请求。

任何 SQL 模板都必须使用参数绑定、只读连接和明确上限；SQL 结果需保存参数、来源、版本、运行时间和 `sql_sha256`，不在日志中保存连接密码。
