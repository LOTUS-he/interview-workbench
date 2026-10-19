# 参考资料索引

本目录按业务步骤提供最小必要资料。先读本索引，再只加载当前步骤需要的文件；不要把整个目录当作一次性上下文。

| 需要回答的问题 | 读取 | 主要步骤 |
| --- | --- | --- |
| 任务允许审核什么、不能做什么？ | [business-scope.md](business-scope.md)、[glossary.md](glossary.md) | 1、3 |
| 历史漏损怎样按窗口、粒度和去重口径核验？ | [metric-definitions.md](metric-definitions.md)、[field-dictionary.yaml](field-dictionary.yaml)、[table-relations.md](table-relations.md) | 2 |
| Query 属性、风险和停止条件是什么？ | [admission-rules.md](admission-rules.md)、[glossary.md](glossary.md) | 3 |
| 名称如何只按已确认词典做归一，哪些约束不能丢？ | [terminology-catalog.json](terminology-catalog.json)、[rewrite-rules.md](rewrite-rules.md) | 3、4 |
| term 与商品、当前资格怎样分开审核？ | [relevance-rules.md](relevance-rules.md)、[eligibility-rules.yaml](eligibility-rules.yaml) | 5、6 |
| 本地只读数据如何调用、失败如何区分？ | [integration-contract.md](integration-contract.md) | 2、4、5 |
| 什么条件可以提交人工审批或分层？ | [decision-policy.md](decision-policy.md) | 7 |
| 六份产物、两次 handoff 和引用怎样落盘？ | [output-contract.md](output-contract.md)、`../schemas/handoff.schema.json`、`../schemas/step-result.schema.json` | 1—8 |
| 哪些规则、步骤、脚本和测试相互影响？ | [resource-map.yaml](resource-map.yaml) | 维护与第 8 步 |

## 权威性与维护

- 资格条件和资格时效的唯一机器权威是 `eligibility-rules.yaml`；其他资料只引用其 rule ID，不复制数值。
- 字段与指标分别由 `field-dictionary.yaml`、`metric-definitions.md` 定义；业务解释不在脚本中另维护一套。
- `terminology-catalog.json` 只保存原始标准名、品牌和别名资料，不保存某条 Query 的预期审核结论。
- `resource-map.yaml` 使用 JSON 兼容 YAML 子集；`scripts/validate_resources.py` 会检查路径、rule ID 和引用关系，并生成从步骤到资源、从规则到步骤的反向索引。
- 业务输入数据和脱敏数据是只读输入来源，不能从中复制答案、评测标签或写入修改。
