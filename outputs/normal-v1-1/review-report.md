# 医药 Query 安全扩召回审核报告

> 只读脱敏业务环境｜只读证据｜未执行发布或投放变更

- Run：10be6911a015450988e3bc06f61cd794
- Query：康安维C咀嚼篇60片
- 范围：330100 / 330100-B / medical_search
- 历史窗口：2026-09-13T00:00:00+08:00 — 2026-09-20T00:00:00+08:00
- 执行模式：智能体交接；模型记录见manifest，不含自动模型API调用。

## 审核结论

- 准入：L1
- 准入理由：本次明确为康安维C咀嚼商品60片的商品搜索，没有咨询或敏感用药意图。独立词典明确维C咀嚼片对应维生素C咀嚼片，结合原文咀嚼篇及片数，可将单字误写篇纠为片；这只是文字纠错，不作临床替代。保留品牌、剂型、60片；原文未表达特定人群，不补造。
- 建议：材料可提交人工审批（不代表已批准）
- 商业分层：V2
- 双通过计划数：2；按 offer_id 去重后的可用供给数：1。同一供给关联多个计划，不重复计为多份供给。
- 修订方式：沿用原run的真实模型判断和商品快照，重新执行历史SQL及确定性校验；本次修订没有新增模型或商品接口调用。

## 实际执行轨迹

| 步骤 | 状态 |
| --- | --- |
| 1. 任务与规则 | 已完成 |
| 2. 历史漏损 | 已完成 |
| 3. Query准入 | 已完成 |
| 4. 改写与索引 | 已完成 |
| 5. 当前证据 | 已完成 |
| 6. 相关性与资格 | 已完成 |
| 7. 审批建议 | 已完成 |
| 8. 校验与报告 | 结构与引用已校验；最终状态见manifest |

## 历史证据

| 指标 | 结果 |
| --- | ---: |
| 有效请求数 | 12000 |
| 已合格请求数 | 3000 |
| 关键词缺口请求数 | 6000 |
| 供给失败请求数 | 2000 |
| 计划失败请求数 | 1000 |
| 未知请求数 | 0 |
| 独立回放核验的潜在缺口请求数 | 6000 |
| 数据是否完整 | True |

行级证据保留 500 / 6000 条；是否截断：True。顶层统计来自全窗口SQL，不由截断明细推算；明细中未找到关联只能暂缓，不能据此认定全量没有机会。

历史回放只提供潜在承接证据，不代表可追回曝光、成交或收入。

## 改写审核

| term | 硬约束后结论 |
| --- | --- |
| T0001 | pass |
| T0002 | reject |
| T0003 | reject |
| T0004 | reject |

## 逐候选审核

下表一行是一个计划级候选，不是一份独立供给；供给数量按 offer_id 去重。

| term / candidate（计划） | offer_id（供给） | 相关性 | 当前资格 | 依据 |
| --- | --- | --- | --- | --- |
| T0001 / A00001 | O00001 | pass | pass | \[\] |
| T0001 / A00002 | O00002 | pass | reject | \["ELIG-001: stock is not positive"\] |
| T0001 / A00003 | O00003 | pass | reject | \["ELIG-001: remaining\_budget\_fen is not positive"\] |
| T0001 / A00010 | O00010 | reject | pass | \["PACK-001: product pack\_count conflicts (query=60, candidate=100)"\] |
| T0001 / A00011 | O00011 | reject | reject | \["PACK-001: product pack\_count conflicts (query=60, candidate=100)", "ELIG-001: stock is not positive"\] |
| T0001 / A00012 | O00012 | reject | reject | \["PACK-001: product pack\_count conflicts (query=60, candidate=100)", "ELIG-001: remaining\_budget\_fen is not positive"\] |
| T0001 / A00019 | O00019 | reject | pass | \["FORM-001: product dosage\_form conflicts with explicit query attribute"\] |
| T0001 / A00020 | O00020 | reject | reject | \["FORM-001: product dosage\_form conflicts with explicit query attribute", "ELIG-001: stock is not positive"\] |
| T0001 / A00021 | O00021 | reject | reject | \["FORM-001: product dosage\_form conflicts with explicit query attribute", "ELIG-001: remaining\_budget\_fen is not positive"\] |
| T0001 / A00028 | O00028 | reject | pass | \["BRAND-001: product brand\_name conflicts with explicit query brand"\] |
| T0001 / A00029 | O00029 | reject | reject | \["BRAND-001: product brand\_name conflicts with explicit query brand", "ELIG-001: stock is not positive"\] |
| T0001 / A00030 | O00030 | reject | reject | \["BRAND-001: product brand\_name conflicts with explicit query brand", "ELIG-001: merchant\_qualification=expired", "ELIG-001: remaining\_budget\_fen is not positive"\] |
| T0001 / A00037 | O00037 | unknown | pass | \["AUDIENCE-001: product adds a specific audience not expressed by query"\] |
| T0001 / A00038 | O00038 | unknown | reject | \["AUDIENCE-001: product adds a specific audience not expressed by query", "ELIG-001: merchant\_qualification=expired", "ELIG-001: stock is not positive"\] |
| T0001 / A00039 | O00039 | unknown | reject | \["AUDIENCE-001: product adds a specific audience not expressed by query", "ELIG-001: remaining\_budget\_fen is not positive"\] |
| T0001 / A00046 | O00046 | pass | reject | \["ELIG-001: qualification\_status=expired", "ELIG-001: prescription drug expansion is out of scope", "ELIG-001: merchant\_qualification=expired"\] |
| T0001 / A00047 | O00047 | pass | reject | \["ELIG-001: qualification\_status=expired", "ELIG-001: prescription drug expansion is out of scope", "ELIG-001: stock is not positive"\] |
| T0001 / A00048 | O00048 | pass | reject | \["ELIG-001: qualification\_status=expired", "ELIG-001: prescription drug expansion is out of scope", "ELIG-001: remaining\_budget\_fen is not positive"\] |
| T0001 / A00055 | O00055 | unknown | pass | \["PACK-001: product pack\_count conflicts (query=60, candidate=100)", "PACK-001: title and structured pack\_count conflict; no source is preferred"\] |
| T0001 / A00056 | O00056 | unknown | reject | \["PACK-001: product pack\_count conflicts (query=60, candidate=100)", "PACK-001: title and structured pack\_count conflict; no source is preferred", "ELIG-001: stock is not positive"\] |
| T0001 / A00057 | O00057 | unknown | reject | \["PACK-001: product pack\_count conflicts (query=60, candidate=100)", "PACK-001: title and structured pack\_count conflict; no source is preferred", "ELIG-001: remaining\_budget\_fen is not positive"\] |
| T0001 / A00064 | O00064 | unknown | unknown | \["EVIDENCE-001: item model relevance is unknown or incomplete", "ELIG-001: qualification\_status is missing or unknown"\] |
| T0001 / A00065 | O00065 | unknown | reject | \["EVIDENCE-001: item model relevance is unknown or incomplete", "ELIG-001: qualification\_status is missing or unknown", "ELIG-001: stock is not positive"\] |
| T0001 / A00066 | O00066 | unknown | reject | \["EVIDENCE-001: item model relevance is unknown or incomplete", "ELIG-001: qualification\_status is missing or unknown", "ELIG-001: remaining\_budget\_fen is not positive"\] |
| T0001 / A00999 | O00001 | pass | pass | \[\] |

## 待复核事项

- item\_review：AUDIENCE-001: product adds a specific audience not expressed by query
  - 规则：REWRITE-001, EVIDENCE-001, BRAND-001, FORM-001, PACK-001, AUDIENCE-001, ELIG-001, TIME-001；证据：evidence/candidates.json#/candidates/12, evidence/task.json#/raw\_query, evidence/candidates.json#/terms/0, evidence/candidates.json#/candidates/12/product
- item\_review：AUDIENCE-001: product adds a specific audience not expressed by query；ELIG-001: merchant\_qualification=expired；ELIG-001: stock is not positive
  - 规则：REWRITE-001, EVIDENCE-001, BRAND-001, FORM-001, PACK-001, AUDIENCE-001, ELIG-001, TIME-001；证据：evidence/candidates.json#/candidates/13, evidence/task.json#/raw\_query, evidence/candidates.json#/terms/0, evidence/candidates.json#/candidates/13/product
- item\_review：AUDIENCE-001: product adds a specific audience not expressed by query；ELIG-001: remaining\_budget\_fen is not positive
  - 规则：REWRITE-001, EVIDENCE-001, BRAND-001, FORM-001, PACK-001, AUDIENCE-001, ELIG-001, TIME-001；证据：evidence/candidates.json#/candidates/14, evidence/task.json#/raw\_query, evidence/candidates.json#/terms/0, evidence/candidates.json#/candidates/14/product
- item\_review：PACK-001: product pack\_count conflicts (query=60, candidate=100)；PACK-001: title and structured pack\_count conflict; no source is preferred
  - 规则：REWRITE-001, EVIDENCE-001, BRAND-001, FORM-001, PACK-001, ELIG-001, TIME-001；证据：evidence/candidates.json#/candidates/18, evidence/task.json#/raw\_query, evidence/candidates.json#/terms/0, evidence/candidates.json#/candidates/18/product
- item\_review：PACK-001: product pack\_count conflicts (query=60, candidate=100)；PACK-001: title and structured pack\_count conflict; no source is preferred；ELIG-001: stock is not positive
  - 规则：REWRITE-001, EVIDENCE-001, BRAND-001, FORM-001, PACK-001, ELIG-001, TIME-001；证据：evidence/candidates.json#/candidates/19, evidence/task.json#/raw\_query, evidence/candidates.json#/terms/0, evidence/candidates.json#/candidates/19/product
- item\_review：PACK-001: product pack\_count conflicts (query=60, candidate=100)；PACK-001: title and structured pack\_count conflict; no source is preferred；ELIG-001: remaining\_budget\_fen is not positive
  - 规则：REWRITE-001, EVIDENCE-001, BRAND-001, FORM-001, PACK-001, ELIG-001, TIME-001；证据：evidence/candidates.json#/candidates/20, evidence/task.json#/raw\_query, evidence/candidates.json#/terms/0, evidence/candidates.json#/candidates/20/product
- item\_review：EVIDENCE-001: item model relevance is unknown or incomplete；ELIG-001: qualification\_status is missing or unknown
  - 规则：REWRITE-001, EVIDENCE-001, BRAND-001, FORM-001, PACK-001, ELIG-001, TIME-001；证据：evidence/candidates.json#/candidates/21, evidence/task.json#/raw\_query, evidence/candidates.json#/terms/0, evidence/candidates.json#/candidates/21/product
- item\_review：EVIDENCE-001: item model relevance is unknown or incomplete；ELIG-001: qualification\_status is missing or unknown；ELIG-001: stock is not positive
  - 规则：REWRITE-001, EVIDENCE-001, BRAND-001, FORM-001, PACK-001, ELIG-001, TIME-001；证据：evidence/candidates.json#/candidates/22, evidence/task.json#/raw\_query, evidence/candidates.json#/terms/0, evidence/candidates.json#/candidates/22/product
- item\_review：EVIDENCE-001: item model relevance is unknown or incomplete；ELIG-001: qualification\_status is missing or unknown；ELIG-001: remaining\_budget\_fen is not positive
  - 规则：REWRITE-001, EVIDENCE-001, BRAND-001, FORM-001, PACK-001, ELIG-001, TIME-001；证据：evidence/candidates.json#/candidates/23, evidence/task.json#/raw\_query, evidence/candidates.json#/terms/0, evidence/candidates.json#/candidates/23/product

## 追溯

结构化结论见steps/，实际取数与响应见evidence/，规则版本及模型交接记录见manifest.json。
当前资格只对应本次业务快照；不保证未来请求的实时可投状态。

