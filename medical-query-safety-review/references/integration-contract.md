# 数据接入与错误处理契约

机器政策 ID：`TIME-001` 约束证据时点、版本和调用时限；资格字段与资格条件仍只引用 `references/eligibility-rules.yaml` 的 `ELIG-001`。接口细则 ID：`INT-001`、`INT-002`、`INT-003`、`INT-004`、`INT-005` 分别覆盖只读环境、凭证脱敏、部分失败、有限重试和独立词典边界。

## 环境与真实路径

本工程只使用只读脱敏业务环境：配置在 `config/demo.yaml`，数据库路径由 `tools/run_demo.py` 的 `--database` 显式传入，服务源码为 `mock/api_server.py`。默认 base URL 由配置提供；不得从环境变量、用户目录或真实业务系统回退读取数据库。

接口是本地只读 GET 服务，不提供写接口、词池发布、审核决策或投放操作。凭证只通过 `MEDICAL_DEMO_TOKEN` 环境变量或显式本地只读配置提供；任何调用结果、manifest、audit-log 和报告都不得输出 token 或密码。

## 实际调用能力

| 调用 | 参数重点 | 返回事实 |
| --- | --- | --- |
| `/health` | 无 | 服务状态和业务时钟 |
| `/v1/terms` | `q`、`limit`、`cursor` | term、启用状态、索引版本；不做医疗理解 |
| `/v1/recall` | `term_id`、`city_id`、分页参数 | 原始 term—计划—offer—SKU—门店关联，不预过滤资格 |
| `/v1/products` | `sku_ids`、分页参数 | 商品主数据和结构化属性 |
| `/v1/offers` | `sku_ids`、`city_id`、分页参数 | 当前门店供给、库存、上架、区域和版本 |
| `/v1/ad-plans` | `offer_ids`、分页参数 | 计划状态、预算、素材状态和版本 |
| `/v1/stores` | `store_ids`、分页参数 | 门店和配送区域 |
| `/v1/qualifications` | `sku_ids`、分页参数 | 商品类别、处方属性和资格事实 |

`fetch_evidence.collect_catalog(config)` 只返回接口返回的 `terms` 及调用元数据；独立词典来自 `references/terminology-catalog.json`，不能由 API 响应代替。`collect_candidates(config, term_ids, city_id)` 返回候选及原始 `product/offer/plan/store/qualification` 对象，单候选 `errors` 与请求级 `errors` 分开保留。

## 请求和响应约束

复数 ID 每批最多 50 个；分页 `limit` 使用接口允许范围，`cursor` 是当前冻结结果集的偏移，不能跨参数或快照复用。`HTTP 200` 仍可能含单对象 `errors`，成功对象和失败对象必须隔离。`401/400/404/405` 等确定性错误不靠重复重试；仅对契约允许的暂时性错误做有限重试，并受配置的调用次数和总耗时上限控制。

统一响应含 `request_id`、`meta`、`data`、`errors`、`error`。保存 evidence 时保留 `meta.source`、`dataset_version`、`scenario`、`as_of`、`served_at`、分页和错误摘要；不把 credentials 写入输出。对象内部的 `updated_at/as_of` 是事实时间，`served_at` 只是调用时间。

## 失败与模式切换

- 服务不可用、超时、限流或公共请求失败：记录调用和影响范围；依赖该源的业务结论为未知或阻断。
- 批量调用中的单对象失败：保留其他成功对象，失败候选进入自己的 `errors`，不拖垮无关候选。
- 字段类型漂移、版本冲突或资格快照时间不满足 `eligibility-rules.yaml`：不得强转后放行，交给 schema/人工复核。
- 接口失败不能静默改用快照文件伪装成功；只有用户显式指定的输入文件才能作为本次事实来源。
- 无真实网络和账号模式；安装 Skill 只提供源码复制，不自动安装全局包或写配置。
