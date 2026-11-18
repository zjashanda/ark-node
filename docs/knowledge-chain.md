# 知识沉淀与复用链路

本文是 8090 证据入口、8091 知识入口与 ark-kb 真源之间的一次任务闭环说明。机器可读契约在
`templates/noahsark-index/services/*.json`，本文只解释字段语义与使用顺序，不展开节点侧内网地址清单与凭据。

## 1. 两条入口与解析规则

| 入口 | 角色 | 用途 |
|---|---|---|
| 8090 Ark Storage Service | 证据入口 | 上传产物与证据、回读校验、写回执、TTS 公共缓存 |
| 8091 NoahsArk Knowledge Service | 知识入口 | 检索、条目详情、proposal 收集、freshness 与降级 |
| ark-kb（Git） | 唯一真源 | 审核通过后的条目与索引 |

服务发现固定按 `node_profile → canonical` 解析：

1. 先取本节点 `discovery.node_profiles[]` 的 `verified_endpoint`；它是本机实测事实，带 `receipt`。
2. 本节点没有 profile 时才用 `discovery.canonical.endpoint`：`entry_kind=node-name` 时是可解析
   节点名，`entry_kind=fixed-ip` 时是已批准固定入口。
3. profile 只在所属节点生效，不得回写 canonical，也不得复制给其他节点。
4. `canonical.fixed_entry` 是批准的固定入口，只登记在 descriptor；生成物必须剥离，出现即按
   `SERVICE_DISCOVERY_DRIFT` 处理。
5. pvm1003 是 VM/跨网段边界：它的 8090 与 8091 入口不同源，必须各用本节点实测 profile，
   禁止用一侧地址推导另一侧，也禁止把它的 profile 当全局替代。

8091 的 canonical 直接就是固定入口 `http://10.11.63.42:8091`（`entry_kind=fixed-ip`），  <!-- noqa: secret-scan -->
旧节点名入口已失效，不保留 alias，也不得回退；8090 仍为可解析节点名
`http://linux003:8090`（`entry_kind=node-name`）并保留 `fixed_entry`。迁移记录见
`docs/migration-20261118-knowledge-entry.md`。

索引与 OSS 只渲染公开字段（节点名、端口、scheme、解析顺序、节点名清单）；fixed-ip 服务的
地址在生成物里写成 `endpoint_ref` 指针与「固定入口（节点侧登记）」，真实值只在 descriptor
与节点回执里。

## 2. 机器可读契约

| 契约 | 关键键 | 语义 |
|---|---|---|
| `discovery`（`ark.service-discovery.v1`） | `canonical`、`resolution`、`node_profiles[]`、`vm_boundary`、`redaction` | canonical 是全局可解析地址；profile 是节点局部实测；`resolution.order` 固定 `node_profile, canonical` |
| `token_bootstrap`（`ark.token-bootstrap.v1`） | `mode`、`per_node`、`bootstrap_api`、`minimum_scopes`、`allowed_scopes`、`storage`、`lifecycle`、`parity` | 每节点独立凭据；`parity.bootstrap_api=false` 时必须声明 `gap` |
| `health_contract`（`ark.service-health.v1`） | `probe`、`required_fields`、`status_rule`、`degraded`、`commits`、`write_path_rule`、`stop_conditions` | 探针路径与判定规则；有 freshness 的服务必须给出 source/indexed 一致性 |
| `auth.anonymous_read`（`ark.service-anonymous-read.v1`） | `status`、`public_api`、`protected_api`、`redaction`、`limits`、`service_change` | 只读匿名边界：公开接口、受保护接口、脱敏与限流、待改服务改动 |
| `token_bootstrap.verification`（`ark.token-bootstrap.verification.v1`） | `state`、`verified_at`、`observed`、`rule` | 自助开通能力的观测证据；声称 `operational` 就必须带观测记录 |

`discovery.redaction.internal_only_fields` 必须同时包含 `discovery.canonical.fixed_entry` 与
`discovery.node_profiles[].verified_endpoint`；生成器剥离，校验器拒绝任何私网字面量。

## 3. 节点最小权限 token bootstrap

- 每节点专用 token，禁止跨节点复制、转借或共用；值与 SHA 不进入 Git、Issue、日志、回执或索引。
- 存储：Linux 用 `~/.config/ark/<service>.token`（目录 0700、文件 0600）；Windows 用
  `%APPDATA%\ark\<service>.token`，ACL 仅当前用户；也可用环境变量，但不得拼进 URL 或命令行参数。
- 生命周期：`bootstrap` 一次性换发、`status` 只看自己的元数据、`rotate` 轮换、`revoke` 立即失效。
- scope：先给最小集合，按需申请 `evidence:write` / `receipts:write` / `backup:write`；不足时按
  `BLOCKED_STORAGE_SCOPE` 处理，不越权重试。
- 8090：`one-time-secret` / `operational`。流程固定四步：管理员预注册 `node_id`、`allowed_scopes`
  与一次性 `bootstrap_secret` 的哈希 → 节点用该 secret 调 `POST /storage/v1/bootstrap/token`
  自助兑换（secret 一次使用后失效，重放按 401）→ token 原子写入 0600 / 仅当前用户 ACL 文件 →
  后续 API 只从本机文件或白名单环境变量读取。轮换 `POST /storage/v1/tokens/rotate`（旧值 300 秒
  grace），吊销 `POST /storage/v1/tokens/revoke`（立即），状态 `GET /storage/v1/tokens/status`
  （只返回自己的元数据，不返回 SHA 与明文）。服务端只存 SHA-256，注册表热加载。
- 8091：`admin-provisioned` / `partial` / `bootstrap_api=false`。只读接口匿名开放后，token 只服务写
  接口：管理员在服务端 token 文件追加 `write:<token>`，按 mtime 热加载；节点只落 0600/ACL 副本。
  该缺口如实记为 `BLOCKED_KB_BOOTSTRAP_PARITY`，不得写成已实现。
- **两套 token 不互通**：不得拿 8090 token 访问 8091，也不得把两者写成同一套签发。

## 3.1 只读公开边界（8091）

- 契约：`auth.anonymous_read`（`ark.service-anonymous-read.v1`），当前 `status=pending_service_change`。
- 公开（匿名，不需要 read token）：`GET /healthz`、`GET /v1/search`、`GET /v1/entries/{entry_id}`、
  `GET /v1/documents/{doc_id}`、`GET /v1/sync/status`。
- 仍鉴权：`POST /v1/proposals`、`GET /v1/proposals`、`POST /v1/proposals/{id}/close`、
  `POST /v1/sync/pull`、`POST /v1/hooks/ark-kb`、`GET /v1/health`。
- 脱敏：公开响应不含 token、凭据、服务端绝对路径、个人敏感信息；只返回已审核 active 条目，
  pending/candidate 正文不得因匿名查询泄露；错误体只回 `code`/`message`/`request_id`。
- 限流与上限：按来源限流（超限 429）、请求体上限 262144 字节、分页上限 500、审计不记
  Authorization 与请求体。
- 现状：2026-11-18 实测匿名 `GET /v1/search`、`GET /v1/entries/{id}`、`GET /v1/sync/status` 仍返回
  401，故状态保持 `pending_service_change`；最小服务改动、影响与回滚见
  `docs/kb-anonymous-read-change.md`，验收通过后才改为 `verified`。
- 未通过前，把匿名 401 当成 `BLOCKED_KB_ANON_READ` 上报，**不得**用临时签发 read token 绕过。

## 4. 闭环九步

| # | 步骤 | 产物 | 失败码 |
|---|---|---|---|
| 1 | 产生证据：产物、日志、录音按任务目录落盘并脱敏 | 本地文件 + manifest（path/bytes/sha256） | `EVIDENCE_GAP` |
| 2 | 上传 8090 证据：先用自助 bootstrap 取 read+evidence:write，再按 evidence/results/receipts 分类写入 | `evidence_ref`（uri/bytes/sha256） | `BLOCKED_STORAGE_AUTH`、`BLOCKED_STORAGE_SCOPE`、`BLOCKED_STORAGE_QUARANTINE` |
| 3 | 写回执：记录 evidence_ref 三项与 request_id | receipt | `EVIDENCE_GAP` |
| 4 | 提交 proposal：用最小 write token 绑定 evidence_uri、request_id、idempotency_key | `proposal_id` + pending | `BLOCKED_KB_AUTH` |
| 5 | 人工审核：核对证据后关闭 accepted / rejected | 审核结论 + entry_id | `SOURCE_MISSING` |
| 6 | 写入 ark-kb：entries + index，走功能分支与 PR | 新 main 提交 | `BLOCKED_GIT_REMOTE` |
| 7 | 增量索引：定时 pull 或 webhook 追平 source/indexed | `indexed_commit`、freshness、degraded | `STALE_INDEX` |
| 8 | 复读校验：由非提交者匿名检索（不配 read token）并核对 citation | 命中 + `kb:<entry_id>@<commit>` | `SOURCE_MISSING` |
| 9 | 跨 Agent 复用：只引用 formal 且非 stale/superseded 条目 | `knowledge_snapshot` + 前置检查 | `STALE_INDEX`、`BLOCKED_KB_UNAVAILABLE` |

## 5. 失败分类与停止条件

| 失败码 | 触发条件 | 处理 | 停止 |
|---|---|---|---|
| `BLOCKED_STORAGE_AUTH` | 8090 缺 token、过期、吊销或节点被禁用 | 用本节点 bootstrap/rotate 换新，不借他人 token | 停止 |
| `BLOCKED_KB_AUTH` | 8091 写接口返回 401/403，或本节点无 write token | 由管理员签发本节点 write token，落 0600 文件 | 停止 |
| `BLOCKED_KB_ANON_READ` | 契约声明匿名的只读接口返回 401/403，或匿名响应泄露凭据、绝对路径、pending 正文 | 按最小服务改动打开 `auth.public_read` 并验收；不得签发 read token 绕过 | 停止 |
| `STALE_INDEX` | source/indexed 不等、freshness 非 fresh 或 degraded 非空 | 等增量索引追平；普通任务可用未过期快照并标注 | 停止 |
| `EVIDENCE_GAP` | evidence_ref 缺 uri/bytes/sha256 或用健康 200 冒充写通 | 补齐上传+回读与回执，否则不进 proposal | 停止 |
| `SOURCE_MISSING` | ark-kb 缺条目/文档，citation 不可解析 | 回到 proposal 审核或补提交，不凭印象补写 | 停止 |
| `SERVICE_DISCOVERY_DRIFT` | canonical 与 profile 冲突、固定入口进入生成物、profile 被跨节点复制 | 按本节点 profile 解析并上报，不静默改 canonical | 降级继续 |
| `BLOCKED_KB_BOOTSTRAP_PARITY` | 期望 8091 自助 bootstrap/rotate，而服务只有管理侧文件开通 | 由管理员签发/轮换，parity 列待办 | 降级继续 |

`STALE_INDEX` 对本表之外的高风险任务（烧录、真机、发布、删除、权限变更）一律按停止处理。

## 6. 健康 200 不等于写链路通

- 8090：健康只说明进程在跑、磁盘可读；写链路必须由 `PUT` 201/200、服务端复算 SHA、回读
  bytes/sha256（或回执）共同证明。
- 8091：健康只说明可读；写链路必须由 `POST /v1/proposals` 返回 `proposal_id`（201 新建、
  200 幂等）、pending 状态与后续审核回执证明，并追平 source/indexed。
- 用健康 200 代替写通证据，按 `EVIDENCE_GAP` 处理。

## 7. 命令清单

```bash
python3 templates/noahsark-index/build-index.py
python3 templates/noahsark-index/verify-index.py
python3 templates/noahsark-index/check-services.py --check
python3 templates/noahsark-index/check-services.py --probe --node <node> --token-file <0600 文件>
python3 templates/noahsark-index/check-services.py --receipt <receipt.json>
python3 templates/noahsark-index/publish-index.py --dry-run
```

`--probe --node <node>` 只从该节点 `discovery.node_profiles` 取探测基址；节点不存在直接失败，
不回退 canonical。`--check` 同时校验 `SERVICE_DISCOVERY_DRIFT`。

## 8. 红线

- 不打印、不落盘 token 或凭据值；诊断只描述 token 是否存在、是否过期、scope 是否足够。
- 不把内网 IP、固定入口或节点 profile 写进 index、manifest、OSS、Issue 或对外文档；需要指代时
  写 `<fixed-entry>`，真实值只在 descriptor 与节点回执。
- 不把 pending proposal 当 active 知识，不用健康 200 冒充写链路。
- 本轮只做本地改动：不 push、不建 PR、不发布 OSS、不改 Agent instructions；合入与发布另行审批。
