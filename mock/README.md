# 独立只读脱敏业务环境

本目录复用业务材料包已核验的虚构事实，但不依赖业务包的路径。api_server.py默认监听127.0.0.1:8765，数据放在数据/，演示连接信息放在配置/。这些文件不含审核结论。

在工程根目录运行：

```bash
python3 mock/init_db.py
python3 mock/api_server.py --port 8765 --scenario normal
```

第一条命令在系统临时目录创建SQLite并打印路径。也可用--output指定一个尚不存在的文件。第二条保持运行，Ctrl+C关闭。场景可选normal、stock-change、partial-failure、slow、rate-limit、schema-drift，保持与业务数据服务契约一致。

seed.sql为合成事实，schema.sql为7张表的DDL。与材料示意名不同：recall_trace对应recall_events，当前关键词关联为数据/词计划关联.json，不为对齐示意名而新造业务表。历史回放有独立索引版本。

不把数据装进可安装Skill，不将业务服务伪装成真实业务API。环境运行生成数据库，不随源码分发数据库二进制。数据生成脚本tools/prepare_mock_assets.py仅供维护时显式导入已提供材料，日常运行无需业务包。
