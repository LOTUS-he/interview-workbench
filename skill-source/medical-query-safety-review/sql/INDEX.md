# 历史漏损 SQL

`query_gap.sql` 是本工程唯一允许执行的历史查询。`scripts/query_history.py`
固定从同级 `sql/` 目录加载它，不接受调用方传入 SQL、SQL 文件路径或数据库 URI。
所有任务值都通过 SQLite 命名参数绑定。

## 输入参数

| 参数 | 含义 |
| --- | --- |
| `query_id` | 任务 Query 标识 |
| `raw_query` | 必须与 `queries.raw_query` 精确一致的原始 Query |
| `city_id` | 目标城市 |
| `region_id` | 目标区域，必须是同一 `city_id` 的 `-A` 或 `-B` |
| `scene_id` | 场域 |
| `history_start` | `+08:00` 左边界，包含 |
| `history_end_exclusive` | `+08:00` 右边界，不包含 |
| `output_limit` | 工程固定上限，默认 100,000 行 |

数据库必须是已有 SQLite 文件；连接使用 `file:...?...mode=ro`，并启用
`query_only`。执行通过 SQLite progress handler 受 15 秒上限约束。CLI 只提供
`--database --input --output` 三个入口参数。

## 统计口径

先在全表按 `event_id` 去重（取 `ingested_at`、`delivery_id` 的确定性最新记录），
再按请求聚合。请求只有在至少一个候选通过最终 `eligible` 时才计入
`eligible_count`；其他请求按“所有候选都被淘汰”的第一完整阶段互斥归类：

`keyword_miss` → `supply_fail` → `plan_fail`。没有日志、阶段日志缺失、或已通过
计划却没有最终 `eligible` 日志，均为 `unknown`。因此一个候选缺货不能压过同一
请求的另一个合格候选。

`verified_gap_count` 不是曝光、成交或收入。它只统计 `keyword_miss` 请求中，独立
`historical_candidate_replay` 至少有一个候选，且按请求发生时刻、城市和明确区域
匹配到恰好一个 `[valid_from, valid_to)` 历史资格版本，并同时满足库存、在售、商品
资格、商家资格、计划、预算和素材条件的不同请求数。一个 offer 关联多个计划时，
证据明细保留计划级记录，但请求计数只算一次。缺失或重叠的资格版本为 `unknown`，
不算验证机会；独立回放未经语义审核，也不等于线上展示。

## 分区完整性

运行前按历史窗口实际触及的本地日期，逐日核验目标城市的
`search_requests`、`recall_events`、`historical_candidate_replay` 三类加载记录。
每个组合必须恰有一行且 `status=complete`。缺行、重复行和 `missing` 都进入
`load_gaps`；`complete=false` 时即使局部 SQL 有结果，也会把历史机会降为
`unknown`，不会用部分数据生成验证数字。日期没有请求不代表分区缺失，分区状态以
`load_manifest` 为准。

## 返回与证据

`analyze(database, task)` 返回契约要求的统计字段、绑定后的 `parameters`、固定 SQL
的 `sql_sha256`、`source`、`complete`、`load_gaps` 和 `evidence_rows`。证据明细按
`request_id + term_id + ad_plan_id + offer_id` 去重，且保留 `request_id`、`term_id`、
`ad_plan_id`、`offer_id`、`sku_id`、回放版本、历史快照数量、
`verification_status`（verified/unknown/ineligible）和原因，不复制状态别名。
唯一明细为 `evidence_rows`；`candidate_gap_evidence_ref: "#/evidence_rows"` 只是引用，不存第二份列表。
用 `candidate_gap_evidence_count`（返回列表条数）和
`candidate_gap_evidence_total`（SQL 全量条数）标识规模。

`historically_qualified=true` 仅代表历史回放候选的资格快照已唯一且满足历史供给、
计划和素材条件，不代表语义审核通过。公共边界在顶层 `evidence_contract` 声明一次：
`attribute_version_comparison=not_available`、
`semantic_review_status=not_run` 和 `safe_review_gate=hold_pending_semantic_review`；
只有规则代理完成 term—商品语义关联后，才可将其中的 term+sku 送入后续人工入池建议。
当前属性版本与历史版本没有在本 SQL 中强行拼接，缺少历史 catalog 事实时按保守 hold
处理。SQL只返回1行全量汇总和最多500行候选证据，不先输出请求明细占掉限额。
汇总始终为全窗口计数，不能用500行推算；`output_truncated=true` 表示证据明细截断。
明细按request/term/plan/offer取前500行，不是随机抽样。已找到的唯一历史资格证据可用于确认某一关联；截断部分未找到关联时只能hold，不能推断全量不存在机会。
`steps/00_input.json` 只保存 `history_ref.path` 与 `history_ref.sha256`，不再次内嵌历史明细；校验器核验引用与哈希。
