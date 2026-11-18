# 知识沉淀与复用链路

本文是 8090 证据入口、8091 知识入口与 ark-kb 真源之间的一次任务闭环说明。机器可读契约在
`templates/noahsark-index/services/*.json`，本文只解释字段语义与使用顺序，不复制内网地址与凭据。

## 1. 两条入口与解析规则

| 入口 | 角色 | 用途 |
|---|---|---|
| 8090 Ark Storage Service | 证据入口 | 上传产物与证据、回读校验、写回执、TTS 公共缓存 |
| 8091 NoahsArk Knowledge Service | 知识入口 | 检索、条目详情、proposal 收集、freshness 与降级 |
| ark-kb（Git） | 唯一真源 | 审核通过后的条目与索引 |

服务发现固定按 `node_profile → canonical` 解析：

1. 先取本节点 `discovery.node_profiles[]` 的 `verified_endpoint`；它是本机实测事实，带 `receipt`。
2. 本节点没有 profile 时才用 `discovery.canonical.endpoint`（可解析节点名）。
3. profile 只在所属节点生效，不得回写 canonical，也不得复制给其他节点。
4. `canonical.fixed_entry` 是批准的固定入口，只登记在 descriptor；生成物必须剥离，出现即按
   `SERVICE_DISCOVERY_DRIFT` 处理。
5. pvm1003 是 VM/跨网段边界：它的 8090 与 8091 入口不同源，必须各用本节点实测 profile，
   禁止用一侧地址推导另一侧，也禁止把它的 profile 当全局替代。

索引与 OSS 只渲染公开字段（节点名、端口、scheme、解析顺序、节点名清单）；固定入口在文中写作
`<fixed-entry>`，真实值只在 descriptor 与节点回执里。

## 2. 机器可读契约

| 契约 | 关键键 | 语义 |
|---|---|---|
| `discovery`（`ark.service-discovery.v1`） | `canonical`、`resolution`、`node_profiles[]`、`vm_boundary`、`redaction` | canonical 是全局可解析地址；profile 是节点局部实测；`resolution.order` 固定 `node_profile, canonical` |
| `token_bootstrap`（`ark.token-bootstrap.v1`） | `mode`、`per_node`、`bootstrap_api`、`minimum_scopes`、`allowed_scopes`、`storage`、`lifecycle`、`parity` | 每节点独立凭据；`parity.bootstrap_api=false` 时必须声明 `gap` |
| `health_contract`（`ark.service-health.v1`） | `probe`、`required_fields`、`status_rule`、`degraded`、`commits`、`write_path_rule`、`stop_conditions` | 探针路径与判定规则；有 freshness 的服务必须给出 source/indexed 一致性 |
| `failure_taxonomy` | `code`、`when`、`action`、`stop` | 失败码、触发条件、处理动作、是否必须停止 |

`discovery.redaction.internal_only_fields` 必须同时包含 `discovery.canonical.fixed_entry` 与
`discovery.node_profiles[].verified_endpoint`；生成器剥离，校验器拒绝任何私网字面量。

## 3. 节点最小权限 token bootstrap

- 每节点专用 token，禁止跨节点复制、转借或共用；值与 SHA 不进入 Git、Issue、日志、回执或索引。
- 存储：Linux 用 `~/.config/ark/<service>.token`（目录 0700、文件 0600）；Windows 用
  `%APPDATA%\ark\<service>.token`，ACL 仅当前用户；也可用环境变量，但不得拼进 URL 或命令行参数。
- 生命周期：`bootstrap` 一次性换发、`status` 只看自己的元数据、`rotate` 轮换、`revoke` 立即失效。
- scope：先给最小集合，按需申请 `evidence:write` / `receipts:write` / `backup:write`；不足时按
  `BLOCKED_STORAGE_SCOPE` 处理，不越权重试。
- 8090：`self-service`，有 bootstrap/rotate/status/revoke API。
- 8091：`admin-provisioned`，只有管理侧 token 文件热加载，没有自助 bootstrap/rotate；该缺口如实
  记为 `BLOCKED_KB_BOOTSTRAP_PARITY`，不得写成已实现。

## 4. 闭环九步

| # | 步骤 | 产物 | 失败码 |
|---|---|---|---|
| 1 | 产生证据：产物、日志、录音按任务目录落盘并脱敏 | 本地文件 + manifest（path/bytes/sha256） | `EVIDENCE_GAP` |
| 2 | 上传 8090 证据：按 evidence/results/receipts 分类写入 | `evidence_ref`（uri/bytes/sha256） | `BLOCKED_STORAGE_AUTH`、`BLOCKED_STORAGE_SCOPE`、`BLOCKED_STORAGE_QUARANTINE` |
| 3 | 写回执：记录 evidence_ref 三项与 request_id | receipt | `EVIDENCE_GAP` |
| 4 | 提交 proposal：绑定 evidence_uri、request_id、idempotency_key | `proposal_id` + pending | `BLOCKED_KB_AUTH` |
| 5 | 人工审核：核对证据后关闭 accepted / rejected | 审核结论 + entry_id | `SOURCE_MISSING` |
| 6 | 写入 ark-kb：entries + index，走功能分支与 PR | 新 main 提交 | `BLOCKED_GIT_REMOTE` |
| 7 | 增量索引：定时 pull 或 webhook 追平 source/indexed | `indexed_commit`、freshness、degraded | `STALE_INDEX` |
| 8 | 复读校验：由非提交者用另一节点身份检索并核对 citation | 命中 + `kb:<entry_id>@<commit>` | `SOURCE_MISSING` |
| 9 | 跨 Agent 复用：只引用 formal 且非 stale/superseded 条目 | `knowledge_snapshot` + 前置检查 | `STALE_INDEX`、`BLOCKED_KB_UNAVAILABLE` |

## 5. 失败分类与停止条件

| 失败码 | 触发条件 | 处理 | 停止 |
|---|---|---|---|
| `BLOCKED_STORAGE_AUTH` | 8090 缺 token、过期、吊销或节点被禁用 | 用本节点 bootstrap/rotate 换新，不借他人 token | 停止 |
| `BLOCKED_KB_AUTH` | 8091 返回 401/403 或本节点无 read token | 由管理员签发本节点 token，落 0600 文件 | 停止 |
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
