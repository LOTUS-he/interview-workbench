# 运行产物与交付契约

机器政策 ID：`OUT-001` 表示六份产物使用统一 envelope，`OUT-002` 表示引用必须绑定本次 run，`OUT-003` 表示执行状态与业务结论分离，`OUT-004` 表示完整交付仍保留未知、阻断和未执行原因。

## 公共 envelope

六份 `steps/*.json` 均使用 `schemas/step-result.schema.json`：

```json
{
  "schema_version": "1.1",
  "run_id": "本次运行ID",
  "step_id": "00|10|20|30|40|50",
  "status": "success|partial|blocked|skipped",
  "data": {},
  "rule_refs": ["本次实际使用的rule_id"],
  "evidence_refs": ["evidence/本次文件.json#/可选指针"]
}
```

`status` 描述步骤执行，不描述业务对象是否通过。`skipped` 必须说明原因；`partial` 必须说明缺失范围；`blocked` 必须保留阻断原因。所有引用均为本次 run 的相对路径，不能引用绝对路径或另一 run。

## 六份产物

| 文件 | step_id | `data` 最小职责 |
| --- | --- | --- |
| `00_input.json` | `00` | 任务副本、已检查的范围、历史参数、可用证据和缺口 |
| `10_query_profile.json` | `10` | Query profile、六项属性、属性证据、风险、准入理由 |
| `20_rewrite_check.json` | `20` | 候选 term 的模型结果、索引命中/版本、词词检查和淘汰原因 |
| `30_term_item_check.json` | `30` | `candidate_id`、term/item 事实、词品结论、当前资格事实和错误隔离 |
| `40_final_review.json` | `40` | 三段终审、映射建议、价值等级、人工复核队列和未确认项 |
| `50_decision_report.json` | `50` | 报告索引、总体建议、是否发布（固定为未发布）、窗口/快照摘要、交接模式 |

SQL 结果和接口响应保存在 `evidence/`，产物引用它们，不把所有原始数据复制进步骤文件。`manifest.json` 保存输入、规则、配置和模型交接的哈希；`audit-log.jsonl` 保存状态变化但不保存 token；`backlinks.json` 由资源校验脚本生成。

## 两次 handoff

`schemas/handoff.schema.json` 表达两种相互排他的交接：

1. `phase=query` 的 `query-decision.json`：profile + rewrites；属性未知为 `null`，已知属性必须有 `raw_span/source_ref`。
2. `phase=items` 的 `item-decision.json`：reviews；必须覆盖本次 evidence 中所有候选的 `(term_id,candidate_id)`，不多不漏。

两次交接都要求 `producer.type=agent`、真实 `producer.model`、理由、`rule_refs` 和 `evidence_refs`。无模型接口时由当前 agent 显式返回 JSON；调度器不能用脚本、固定答案或默认模型名伪造交接。

### 未执行的第二阶段（内部留档）

高风险准入早停，或候选 evidence 为空时，不产生第二次外部 handoff。调度器可以在 run 内部写入以下归档状态，以便报告解释为什么没有 item 语义结果：

```json
{
  "run_id": "本次运行ID",
  "phase": "items",
  "producer": {"type": "not_executed", "model": null},
  "reviews": []
}
```

该对象不是模型交接输入，不应提交给 `handoff.schema.json` 校验，也不应追加 `manifest.model_calls`。manifest 的业务 step 6 写 `status: "skipped"` 并说明 `admission_stopped` 或 `no_candidates`；只有存在非空候选集合时才进入第二次真实 agent handoff。

## 完成和安全边界

报告应同时说明只读/脱敏模式、历史窗口、当前检查时点、已确认事实、未知和待人工事项。没有发布权限时 `published` 必须为 `false`；报告不能把资格通过写成上线保证，不能把潜在历史机会写成收入增量。
