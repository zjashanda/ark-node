# knowledge-ui（8092）

NoahsArk 知识库的**只读**局域网查询页，给人看，不提供任何写入能力。

## 定位

| 项 | 说明 |
|---|---|
| 端口 | `8092` |
| 运行节点 | `bszT14`（与 8091 同机） |
| 数据源 | 8091 `/v1/search`、`/v1/entries/{id}`；文档全文直查 `kb.sqlite`（只读） |
| 放通网段 | `10.11.63.0/24`、`10.210.8.0/24`（可用 `NOAHSARK_UI_NETS` 覆盖） |
| 管理方式 | user systemd：`noahsark-knowledge-ui.service`（用户 `bszheng`） |

## 交互

- 列表只显示标题、状态、一行摘要、来源，便于快速扫读
- 点击展开：条目显示完整 `claim` + scope + evidence；文档显示全文（按 chunk 行序拼接）
- 条目详情走 `/v1/entries/{id}`；文档详情走 `/api/doc/<path>`，由本服务直查 sqlite，不依赖 8091 的 `/v1/documents/`（该接口对含 `/` 的 doc_id 未做解码，返回 404）

## 配置

| 环境变量 | 默认值 |
|---|---|
| `NOAHSARK_UI_HOST` | `0.0.0.0` |
| `NOAHSARK_UI_PORT` | `8092` |
| `NOAHSARK_API_BASE` | `http://127.0.0.1:8091` |
| `NOAHSARK_KB_SQLITE` | `/home/bszheng/NoahsArk-data/knowledge_service/kb.sqlite` |
| `NOAHSARK_UI_NETS` | `10.11.63.0/24,10.210.8.0/24,10.11.198.0/23,10.11.193.0/24,127.0.0.0/8` |

## 部署

```sh
systemctl --user stop noahsark-knowledge-ui
cp app.py index.html /home/zhsh/workspace/services/noahsark-knowledge-ui/
python3 -m py_compile /home/zhsh/workspace/services/noahsark-knowledge-ui/app.py
systemctl --user start noahsark-knowledge-ui
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8092/api/healthz
```

回滚：节点目录内保留 `app.py.bak-<时间戳>` / `index.html.bak-<时间戳>`，覆盖回去再重启即可。

## 依赖

- Python 3 标准库（`http.server`、`sqlite3`、`urllib`），无第三方包
- 8091 知识服务在线
- `kb.sqlite` 可读

## 已知限制

- `kb.sqlite` 用 FTS5 `trigram` 分词，少于 3 个字符的查询无法命中索引，中文双字词（唤醒、音量）会退化为 LIKE 全表扫描
- 搜索命中不含 `claim` 字段，只有截断的 `snippet`；完整内容需按 id 二次请求
