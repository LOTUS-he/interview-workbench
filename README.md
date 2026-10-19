# 医药 Query 安全扩召回 · 面试演示工作台与审核 Skill

本人负责的真实业务项目（数据已脱敏，只读演示环境）。

## 仓库内容

| 路径 | 说明 |
| --- | --- |
| `index.html` | 面试演示工作台（单文件，双击即开 / GitHub Pages 在线版）。内含两例真实业务脱敏运行回看：正常案例（康安维C咀嚼篇60片 → 25 候选 → 双通过 2 → 1 份确认供给，9 项待人工复核）与高风险案例（剂量咨询 → 准入拒绝、跳过改写、0 候选）。六个视图：候选审核 / 历史证据 / 执行记录 / 交付文件（含 SKILL.md 方法论预览）。 |
| `medical-query-safety-review/` | 医药 Query 安全扩召回审核 Skill 本体（31 文件，可安装即用）：SKILL.md 入口、15 份规则库（含资格唯一权威、字段字典、词典、资源索引）、3 份协议 Schema、8 个确定性引擎脚本、SQL 历史漏损查询。 |

## 在线访问

- GitHub Pages：https://lotus-he.github.io/interview-workbench/

## Skill 自检

```bash
python3 medical-query-safety-review/scripts/validate_resources.py --output /tmp/backlinks.json
# 期望 {"valid": true}
```

