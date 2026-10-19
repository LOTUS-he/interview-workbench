# 医药 Query 安全扩召回 · 审核工程与演示工作台

本人负责的真实业务项目（数据已脱敏，只读环境）。

> 版权与授权：本仓库为原创业务工程，保留所有权利（详见 LICENSE）。仅供查看与学习；未经许可不得复制、分发或商用，引用需注明出处。

## 仓库内容

| 路径 | 说明 |
| --- | --- |
| `index.html` | GitHub Pages 在线预览页：展示工作台界面与两例真实业务脱敏运行回看（正常案例 / 高风险案例）。Pages 仅静态托管、不执行审核服务；完整交互演示请在本机启动工作台（见快速开始）。 |
| `skill-source/medical-query-safety-review/` | 医药 Query 安全扩召回审核 Skill 本体（31 文件，可安装即用）：SKILL.md 入口、15 份规则库、3 份协议 Schema、9 个确定性引擎脚本、SQL 历史漏损查询。 |
| `mock/` | 只读业务环境数据层：商品主数据 96 条、门店、待审 Query 180 条、关键词索引 48 条、供给/广告计划快照、标准词与别名；SQLite DDL/种子；只读 API 服务 `api_server.py`（7 个端点，含故障场景注入）。 |
| `config/demo.yaml` | 连接配置：本地只读服务地址、令牌环境变量、超时/重试上限。 |
| `tools/` | 八步审核调度器 `run_demo.py`（prepare → resume-query → finalize，两次 agent 交接，冻结校验）+ 前置检查 `preflight.py`。 |
| `workbench/` | 演示工作台服务端 `server.py`（端口 8877，回看历史 run + 新建运行自动初始化只读环境）+ 前端。 |
| `outputs/` | 两例已完成的运行产物（正常 / 高风险），工作台只读回看。 |
| `examples/` | 两个输入样例（正常 Query / 高风险 Query）。 |

## 快速开始

### 方式一：一键启动工作台

双击 `启动工作台.command`（macOS），或终端执行：

```bash
python3 workbench/server.py
```

浏览器打开 `http://127.0.0.1:8877`。无需安装任何前端依赖：

- 左侧回看两个历史运行（只读）；
- 右上"新建任务"选 Query / 城市 / 场景（normal / 库存变化 / 部分对象失败 / 接口超时 / 接口限流 / 字段漂移），服务端自动初始化独立数据库与只读接口并真实执行准备阶段；
- 在 Query / 商品语义判断处暂停，复制交接说明给当前 agent，导入其生成的 JSON 继续推进。

### 方式二：命令行完整链路（Python 3.10+，仅标准库）

```bash
python3 mock/init_db.py                                          # 初始化数据库，打印 DB 路径
python3 mock/api_server.py --port 8765 --scenario normal         # 启动只读业务服务（另开终端）
python3 tools/preflight.py --config config/demo.yaml --database "<DB_PATH>"
python3 tools/run_demo.py prepare --input examples/case-input.json --config config/demo.yaml --database "<DB_PATH>" --output-dir "<RUN_DIR>"
# 停在 awaiting_query，读取 handoff.json 给 agent 完成 query-decision.json
python3 tools/run_demo.py resume-query --run-dir "<RUN_DIR>" --decision "<QUERY_DECISION>"
# 若需商品审核，agent 完成 item-decision.json
python3 tools/run_demo.py finalize --run-dir "<RUN_DIR>" --decision "<ITEM_DECISION>"
```

## 在线访问

- GitHub Pages（静态预览，仅回看界面与案例）：https://lotus-he.github.io/interview-workbench/
- 完整交互演示：clone 本仓库后在任意本机启动工作台（见快速开始），浏览器打开 `http://127.0.0.1:8877`

## Skill 自检

```bash
python3 skill-source/medical-query-safety-review/scripts/validate_resources.py --output /tmp/backlinks.json
# 期望 {"valid": true}
```
