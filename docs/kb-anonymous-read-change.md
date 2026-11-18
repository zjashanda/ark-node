# 8091 只读接口匿名开放：最小服务改动预览

本文件只描述**待批准的服务改动**，本轮未修改 8091 服务代码、未重启服务。契约真源是
`templates/noahsark-index/services/knowledge-service.json` 的 `auth.anonymous_read`。

## 1. 现状（2026-11-18 实测，无凭据）

| 接口 | 期望 | 现状 |
|---|---|---|
| `GET /healthz` | 匿名 200 | 200 |
| `GET /v1/search` | 匿名 200 | 401 需要 Authorization: Bearer |
| `GET /v1/entries/{entry_id}` | 匿名 200/404 | 401 |
| `GET /v1/sync/status` | 匿名 200 | 401 |
| `POST /v1/proposals` | 无 token 401/403 | 401 |

结论：`anonymous_read.status = pending_service_change`，匿名能力尚未生效。

## 2. 目标契约

- 公开（匿名）：`/healthz`、`/v1/search`、`/v1/entries/{entry_id}`、`/v1/documents/{doc_id}`、
  `/v1/sync/status`。
- 仍鉴权（write token）：proposal 提交/列表/关闭、`/v1/sync/pull`、`/v1/hooks/ark-kb`、
  `/v1/health`、token 与服务管理。
- 公开响应只含已审核 active 知识；pending/candidate 正文、token、服务端绝对路径、个人敏感信息
  一律不得出现。
- 限流按来源，超限 429；请求体上限 262144 字节；分页上限 500；审计不记录 Authorization 与请求体。

## 3. 最小改动（仅这两处）

1. `arkks/auth.py`：新增公开只读路由名单（method + 路径正则），命中即跳过 Bearer 校验；名单只
   覆盖第 2 节的 5 个 GET 路径。写路径继续走原有 scope 校验：缺 token 401、scope 不足 403。
2. `config/service.json`：新增 `auth.public_read`（默认 `false`）。关闭时行为与今天完全一致，
   便于灰度与回滚。

不改台账、不改 outbox、不改索引同步、不改 proposal 校验、不动 SQLite 结构。

## 4. 影响面

- 只放宽 GET 只读；写接口与审核路径权限不变，proposal 仍然 pending 不等于 active。
- 匿名流量会上升：必须同时启用限流与请求体上限，否则按风险项评估后再开。
- 公开响应脱敏需要回归：确认 search 只返回 active、错误体不带路径、健康状况不带内部文件名。
- 服务版本号递增并回填 descriptor 的 `verification`/`anonymous_read.status=verified`。

## 5. 测试

| 用例 | 期望 |
|---|---|
| 匿名 5 个只读接口 | 200（条目不存在时 404） |
| 匿名 `POST /v1/proposals` | 401 |
| 只读 token 调写接口 | 403 |
| write token 调写接口 | 201 / 200 幂等 |
| `auth.public_read=false` | 全部 GET 恢复 401（回滚验证） |
| 匿名 search 含 pending 条目 | 不返回其正文 |
| 超限请求 | 429 且带 Retry-After |
| 响应体过大 | 413 |

## 6. 回滚

把 `auth.public_read` 置回 `false` 并重启服务进程；无数据变更、无迁移、无残留状态。
descriptor 的 `anonymous_read.status` 同时改回 `pending_service_change`。

## 7. 验收口径

匿名 search / detail / sync-status 均 200，匿名 proposal 401/403，公开响应无凭据与绝对路径，
才允许把 `anonymous_read.status` 从 `pending_service_change` 改为 `verified`；否则按
`BLOCKED_KB_ANON_READ` 停止，不得用临时签发 read token 绕过。
