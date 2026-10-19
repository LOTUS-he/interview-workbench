# scripts 使用说明

这些脚本只负责结构校验、确定性数据处理、证据接入和报告汇总，不替代 agent 的 Query 理解、候选改写或商品语义判断。运行环境为 Python 3.10+ 标准库；不依赖全局 `pyyaml`、`jsonschema` 或模型 API。`.yaml` 文件使用 JSON 兼容子集，由 `json.load` 读取。

## 调度器的真实入口

主调度器位于工程根目录的 `tools/run_demo.py`，其公共 CLI 为：

```bash
python3 tools/run_demo.py prepare --input <task.json> --config <config.json> --database <database.sqlite> --output-dir <run-dir>
python3 tools/run_demo.py resume-query --run-dir <run-dir> --decision <query-decision.json>
python3 tools/run_demo.py finalize --run-dir <run-dir> --decision <item-decision.json>
```

`prepare` 先校验输入、保存本次运行证据并等待第一次 Query handoff。调度器在 Query 风险已由 agent 交接确认后，才对允许继续的路径调用 `scripts/query_history.py`；因此业务八步仍按流程的 1—8 逻辑定义，但实际执行预筛可以先做第 3 步早期准入，再决定是否执行第 2 步历史 SQL。高风险 Query 不应为了填充报告先取历史数据。

两次交接必须是实际 agent 返回的 JSON：

- `query-decision.json` 对应 `phase=query`，由 `resume-query` 接收；
- `item-decision.json` 对应 `phase=items`，由 `finalize` 接收。

有两个不进入第二次交接的分支：Query 高风险早停，或允许继续的路径没有候选。它们只在 run 内部留档 `phase=items`、`producer.type=not_executed`、`producer.model=null`、`reviews=[]`；该对象不是 `handoff.schema.json` 的模型输入，不追加 `manifest.model_calls`。此时 manifest step 6 必须是 `skipped` 并说明 `admission_stopped` 或 `no_candidates`。非空候选仍必须由真实 agent 返回 `producer.type=agent` 的第二次 handoff，且逐一覆盖候选。

调度器会把六份公共产物写入同一 run 的 `steps/`，文件和 step_id 固定为：`00_input.json/00`、`10_query_profile.json/10`、`20_rewrite_check.json/20`、`30_term_item_check.json/30`、`40_final_review.json/40`、`50_decision_report.json/50`。业务八步状态另在 `manifest.json` 记录，不用 01—08 替代六份产物的公共 step_id。

## 本目录脚本

### `validate_input.py`

校验任务输入的结构、必填字段、城市/区域关系、场域和带 `+08:00` 的完整自然日窗口：

```bash
python3 skill-source/medical-query-safety-review/scripts/validate_input.py --input <task.json>
```

退出码为 `0` 表示结构通过，`1` 表示发现输入错误。它不推断品牌、规格、人群或业务结论。

### `validate_resources.py`

检查 `references/resource-map.yaml` 的资源路径、步骤、规则 ID、脚本/测试引用，并生成供本次运行使用的反向索引。`--output` 是主调度器已约定的唯一输出路径；Skill 根默认由脚本自身路径定位，不依赖当前工作目录：

```bash
python3 skill-source/medical-query-safety-review/scripts/validate_resources.py --output <run-dir>/backlinks.json
```

默认模式允许主代理尚未合入的文件缺失，并把缺失清单写入 `backlinks.json`；资源图自身的结构错误仍返回非零。最终成品验收使用：

```bash
python3 skill-source/medical-query-safety-review/scripts/validate_resources.py --output <run-dir>/backlinks.json --strict
```

`validate_resources.py` 导出 `load_rule_ids(root=None) -> set[str]`。主调度器在接受任一模型 handoff 后，应将该集合与 `profile.rule_refs`、rewrite `rule_refs`、review `rule_refs` 做差集校验；差集非空即拒绝该交接，不允许模型自行发明 rule ID。稳定 machine-policy IDs 为 `SCOPE-001`、`ADMIT-001`、`REWRITE-001`、`BRAND-001`、`FORM-001`、`PACK-001`、`AUDIENCE-001`、`EVIDENCE-001`、`ELIG-001`、`TIME-001`、`DECISION-001`、`VALUE-001`；解释性 `ADM-*`、`RWR-*`、`REL-*`、`DEC-*` 等也必须在 resource map 登记后才能引用。

## 其他模块接口

以下是实现契约约定的 Python 函数边界；是否存在由当前工程合入状态决定，缺失时报告缺口，不生成替代答案：

| 模块 | 函数 | 确定性职责 |
| --- | --- | --- |
| `query_history.py` | `analyze(database, task)` | 参数绑定、请求级历史漏损统计、独立回放资格证据和 `sql_sha256` |
| `fetch_evidence.py` | `collect_catalog(config)` | 读取索引目录；返回 term，不从 API 生成独立词典 |
| `fetch_evidence.py` | `collect_candidates(config, term_ids, city_id)` | 分页/批量/有限重试，保留原始对象、错误和调用元数据 |
| `check_constraints.py` | `check(task, query_decision, item_decision, evidence, policy, history=None)` | 检查已标准化硬冲突、资格规则和未知处理，不替代语义审核 |
| `validate_outputs.py` | `validate(run_dir)` | 校验六份产物、handoff、证据和 run 绑定 |
| `build_report.py` | `build(run_dir)` | 只汇总结构化结果，使用 `assets/review-report-template.md` 的 `{{report_body}}` 槽位 |

## 运行边界

所有数据库和 API 调用均为只读、脱敏和显式路径；不自动尝试真实网络、账号或投放系统。失败不能变成业务通过，模型不由普通 Python 进程自动调用。安装只提供源码复制到目标 Skill 目录的方式，不自动写全局目录、安装包或修改用户配置。
