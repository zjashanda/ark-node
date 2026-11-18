# 8091 知识服务入口迁移记录（2026-11-18）

## 结论

8091 知识服务入口由旧节点名 `http://bszT14:8091` 全量替换为固定入口
`http://10.11.63.42:8091`。不保留别名（alias），不提供回退（fallback）。

- 生效时间：2026-11-18
- 批准：Stage 4 / NOAH-191，郑保山确认固定入口
- 契约：`templates/noahsark-index/services/knowledge-service.json`，version 1.1.1，
  `discovery.canonical.entry_kind = fixed-ip`

## 影响面

| 对象 | 变化 |
|---|---|
| descriptor `knowledge-service.json` | endpoint/canonical/public_display 改为固定入口语义 |
| catalog `noahsark-index` | 版本 2.1.1；服务条目版本 1.1.1；服务发现规则按 entry_kind 分流 |
| 生成物 `index.md` / `index.json` / `manifest.json` | fixed-ip 服务只写 `endpoint_ref` 与「固定入口（节点侧登记）」，不含任何地址字面量 |
| 节点侧 profile | 沿用实测 `verified_endpoint`，回执见下表 |
| 调用方 | 只按 descriptor 的 `discovery.canonical` 解析，不再尝试旧节点名 |

节点 profile 回执编号：

| 节点 | 回执 |
|---|---|
| bszT14（宿主机） | NOAH-202 |
| linux003 | NOAH-203 |
| pvm1003（VM 边界） | NOAH-204 |
| lsaswx | NOAH-205 |
| win003 | NOAH-206 |
| 节点统一复核 | NOAH-192 |

## 历史数据

历史 Issue 评论、旧回执、原始日志与已发布的历史对象**不改写**。但旧入口
`http://bszT14:8091` 自生效时间起已失效，只能作为历史记录阅读，
**不得**再作为服务发现依据，也不得据此发起探测或写回执。

## 校验与回滚

- 仓库内旧入口命中只允许出现在本迁移记录；`build-index.py` 与 `verify-index.py`
  对生成物做旧入口和 RFC1918 硬校验，命中即失败。
- 回滚方式：还原 `knowledge-service.json`、`catalog.json` 与生成器改动，重新生成
  三件套并复跑 `build-index.py` / `verify-index.py` / `check-services.py --check`；
  回滚同样只改指针，不删除历史对象。
