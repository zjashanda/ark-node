# 8091 匿名访问变更（只读 + pending proposal）

本文件只描述**待批准的服务改动**，本轮未修改 8091 服务代码、未重启服务。契约真源是
`templates/noahsark-index/services/knowledge-service.json` 的 `auth.anonymous_read` 与
`auth.anonymous_proposal`。

## 1. 现状（2026-11-18 实测，无凭据）

| 接口 | 期望 | 现状 |
|---|---|---|
| `GET /healthz` | 匿名 200 | 200 |
| `GET /v1/search` | 匿名 200 | 401 需要 Authorization: Bearer |
| `GET /v1/entries/{entry_id}` | 匿名 200/404 | 401 |
| `GET /v1/documents/{doc_id}` | 匿名 200/404 | 401 |
| `GET /v1/sync/status` | 匿名 200 | 401 |
| `POST /v1/proposals` | 匿名 201 pending | 401 |

结论：`anonymous_read.status` 与 `anonymous_proposal.status` 都是 `pending_service_change`，
匿名能力尚未生效。

## 2. 目标路由表

| 接口 | 匿名 | 说明 |
|---|---|---|
| `GET /healthz` | 是 | 健康探针，`?strict=1` 降级 503 |
| `GET /v1/search` | 是 | 只返回 active 条目 |
| `GET /v1/entries/{entry_id}` | 是 | 只返回 active 条目 |
| `GET /v1/documents/{doc_id}` | 是 | 只回标量字段 |
| `GET /v1/sync/status` | 是 | commit 与 freshness |
| `POST /v1/proposals` | 是（仅创建 pending） | 需 provenance/evidence/request_id/idempotency_key，来源须命中局域网网段 |
| `GET /v1/proposals`、`GET /v1/proposals/{id}` | 否 | write token |
| `POST /v1/proposals/{id}/close` | 否 | write token，审核/接受/拒绝 |
| `GET /v1/health` | 否 | write token |
| `POST /v1/sync/pull` | 否 | write token |
| `POST /v1/hooks/ark-kb`、`POST /v1/webhook/github` | 否 | write token |
| token / 服务 / 配置管理 | 否 | 管理员 |

公开响应只含已审核 active 知识；pending/stale/superseded 正文、token、服务端绝对路径、
个人敏感信息一律不得出现。

## 3. 配置开关（默认全部关闭）

| 开关 | 类型 | 默认 | 作用 |
|---|---|---|---|
| `auth.public_read` | bool | `false` | 打开只读公开路由名单 |
| `auth.anonymous_proposal` | bool | `false` | 打开匿名 pending proposal 创建 |
| `auth.anonymous_nets` | list | `[]` | 允许匿名访问的已批准局域网网段（CIDR） |

关闭时行为与今天完全一致，便于灰度与回滚。任一开关为 false 时对应的匿名路由按原逻辑返回 401。

## 4. 匿名 proposal 门禁清单

1. 来源：请求 IP 必须命中 `auth.anonymous_nets`，否则 401。
2. provenance：`provenance.issue`、`provenance.run`、`provenance.agent`、`provenance.node` 必填。
3. evidence：每条必须带 `kind`、`ref`、`bytes`、`sha256`；缺项或不完整 4xx，不落候选。
4. 请求标识：`request_id` 与 `idempotency_key` 必填。
5. 幂等：相同 `idempotency_key` 不产生重复 proposal，重复提交返回已有 `proposal_id`
   （首次 201，重复 200）。
6. 状态：新建只能是 `pending`；匿名不得 review / accept / reject / close / promote。
7. 限额：请求体上限、分页上限、来源速率（每分钟）、outbox 待审总量上限，超限 429。
8. 脱敏：日志只记 `proposal_id`、来源、provenance 标识与状态码，不记正文与任何凭据。

## 5. 最小改动范围

1. `arkks/auth.py`：公开只读路由名单（method + 路径正则）扩展为第 2 节 6 条 GET；
   `_authorize` 增加匿名 proposal 判定（来源网段 + 门禁），其余写路径继续走原有 scope 校验。
2. `arkks/server.py`：注册 `POST /v1/proposals` 的匿名分支与参数校验；匿名分支只允许
   `pending`，并复用现有 outbox 追加逻辑。
3. `arkks/config.py`：新增 `auth.public_read`、`auth.anonymous_proposal`、`auth.anonymous_nets`
   默认值与校验（网段用 CIDR，非法直接启动失败）。
4. `tests/test_anonymous.py`：覆盖第 7 节验收矩阵。

不改台账、不改 SQLite 结构、不改索引同步、不改审核权限模型。

## 6. 影响面

- 只放宽只读 GET 与 pending proposal 创建；审核、入库、sync、admin 权限不变，
  proposal 仍是 pending 不等于 active。
- 匿名流量会上升：必须同时启用限流与请求体上限，否则按风险项评估后再开。
- outbox 待审总量会增长：需要待审上限与定期清理策略，避免无界堆积。
- 日志量上升：只增标识与状态码，不增正文；审计字段固定，避免日志本身成为泄露面。
- 公开响应脱敏需要回归：确认 search/detail 只返回 active、错误体不带路径、
  健康状况不带内部文件名。
- 服务版本号递增并回填 descriptor 的 `anonymous_read.status` / `anonymous_proposal.status=verified`。

## 7. 验收矩阵

| 用例 | 期望 |
|---|---|
| 匿名 `GET /healthz`、`/v1/search`、`/v1/entries/{id}`、`/v1/sync/status` | 4 类均 200（条目不存在时 404） |
| 匿名 `POST /v1/proposals`（字段齐全、来源命中网段） | 201 且 `status=pending`、带 `proposal_id` |
| 同 `idempotency_key` 重复提交 | 200 且同一 `proposal_id` |
| 匿名 `POST /v1/proposals/{id}/close` | 401/403 |
| 匿名 `POST /v1/sync/pull`、`GET /v1/health`、hooks/webhook/admin | 401/403 |
| 匿名 search 含 pending 条目 | 不返回其正文 |
| 来源不在 `auth.anonymous_nets` | 401 |
| 缺 evidence `bytes`/`sha256` | 4xx 且不落候选 |
| 超限请求 | 429 且带 Retry-After |
| 响应体过大 | 413 |
| 开关全关后 | 上述匿名请求恢复 401 |
| 变更后索引 | `source_commit == indexed_commit`、`freshness=fresh`、`degraded=[]` |

## 8. 回滚

把 `auth.public_read`、`auth.anonymous_proposal` 置回 `false`、`auth.anonymous_nets` 置回 `[]`
并重启服务进程；无数据变更、无迁移、无残留状态。descriptor 的两个匿名块 status 同时改回
`pending_service_change`。

## 9. 验收口径

匿名 GET 4 类 200、匿名 POST 返回 201 pending、匿名 close/sync/admin 401/403、公开响应无凭据与
绝对路径、索引仍 fresh，才允许把两个匿名块从 `pending_service_change` 改为 `verified`；
否则按 `BLOCKED_KB_ANON_READ` / `BLOCKED_KB_ANON_PROPOSAL` 停止，不得用临时签发 token 绕过。
