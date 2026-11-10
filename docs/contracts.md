# 契约

本仓库对外只承诺四件事：三份 JSON 结构与一条时间口径。字段一旦发布，只能加不能改语义。

## 0. 时间契约

全仓库统一 **北京时间（UTC+8）、无时区标记**。所有对外时间字段——人读文本、日志、JSON 值、回执、文档示例——只能有一种形态：

| 用途 | 格式 | 示例 |
|---|---|---|
| 一切时间字段（人读 / 机器读 / 排序） | `YYYY-MM-DD HH:MM:SS` | `2026-11-10 19:04:31` |
| 目录名 / 文件名（标识符，不是时间字段） | `YYYYMMDD-HHMMSS` | `20261110-190431` |

禁止事项（time lint 会拦截）：

- 用 `T` 分隔日期与时间；
- `Z` 后缀表示 UTC；
- 任何数字时区偏移，如 `+0800`、`+08:00`；
- 任何等价的带时区序列化（含毫秒的带时区串同理）。

唯一例外：外部系统返回的原始时间串可原样保留，但字段名必须显式带 `raw_` 前缀（例如 `raw_started_at`），且不得被当作排序或比对依据。

实现上只有 `util.now_local()` 一个来源：`datetime.now(BEIJING)`，`BEIJING` 为固定的 UTC+8 时区对象，**不读节点本地时区**。原因：节点遍布不同时区与不同 `TZ` 设置，一旦用本地时间，跨节点台账无法对齐。

格式常量与自检：`util.FMT_DISPLAY` = `%Y-%m-%d %H:%M:%S`，`util.DISPLAY_PATTERN` 为正则 `^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$`；`tools/time_lint.py` 与 `tests/test_time_contract.py` 共用同一口径。解析用 `util.parse_display()`，它返回的 `datetime` 带北京时间 tzinfo，仅供内部计算，不得直接序列化输出。

## 1. `ark-node-capability.v1`

由 `ark-node probe` 产出。

```json
{
  "schema": "ark-node-capability.v1",
  "ark_node_version": "0.1.0",
  "generated_at": "2026-11-10 19:04:31",
  "node": { "node_id": "node-a", "hostname": "node-a", "platform": "linux", "arch": "x86_64", "role": "node" },
  "tools": {
    "python": { "present": true, "version": "3.12.3" },
    "git":    { "present": true, "version": "2.43.0" },
    "ssh":    { "present": true, "version": "9.6p1" },
    "scp":    { "present": true },
    "curl":   { "present": true, "version": "8.5.0" },
    "tar":    { "present": true },
    "powershell": { "present": false, "version": null },
    "usb_tree":   { "present": false }
  },
  "resources": {
    "cpu_count": 8,
    "memory_total_gb": 31.3,
    "disk": { "path": "/home", "free_gb": 412.7, "total_gb": 930.2 }
  },
  "env": { "version": "2026-11-09b", "source": "process", "missing_keys": [], "key_count": 10 },
  "task_root": { "path": "/home/node/.ark/tasks", "exists": true, "writable": true },
  "capabilities": { "network": { "dns": true, "https": true } }
}
```

不变量：

1. `schema` 恒定 `ark-node-capability.v1`。
2. `env` 段**只含键名**，任何字段都不得出现环境变量的值。
3. 工具缺失时 `present: false`、`version: null`，**不是**省略字段。
4. 资源探测失败时值为 `null`，不抛异常。

## 2. `ark-node-manifest.v1`

由 `ark-node fetch` 在成功交付后写出（默认 `artifact-manifest.json`）。

```json
{
  "schema": "ark-node-manifest.v1",
  "artifact": "my-artifact",
  "project": "demo",
  "version": "1.2.0",
  "sha256": "<64 位小写十六进制 sha256>",
  "size": 1048576,
  "source": { "kind": "cache", "node": "cache-1" },
  "dest": "/data/inbox/my-artifact-1.2.0.zip",
  "fetched_at": "2026-11-10 19:04:31",
  "attempts": [ { "source": "ledger", "ok": false, "error": "not found" }, { "source": "cache-1", "ok": true } ]
}
```

不变量：

1. `sha256` 必填，且是**校验后**的值；不匹配的产物不进 manifest，进 quarantine。
2. `fetched_at` 用时间契约的人读格式。
3. `attempts` 至少一条，记录每一次尝试的来源与结果。
4. 写盘前统一过 `util.redact_obj`，错误串里的 token 会被替换为 `<redacted>`。

失败时写 `fetch-failure-report.json`：

```json
{
  "schema": "ark-node-manifest.v1#failure",
  "artifact": "my-artifact",
  "generated_at": "2026-11-10 19:04:31",
  "result": "BLOCKED",
  "sources": [ { "source": "ledger", "ok": false, "error": "not found" } ],
  "next_step": "检查来源节点是否在线，或指定 --source"
}
```

## 3. `ark-node-env-state.v1`

由 `ark-node env` 每次执行后写出（`~/.config/ark/env-state.json`，600）。

```json
{
  "schema": "ark-node-env-state.v1",
  "base": "https://ark-env.example.com:8443",
  "remote_version": "2026-11-09b",
  "local_version_before": "2026-11-08a",
  "scope": "user",
  "mode": "apply",
  "changed_keys": ["NAS_PASSWORD"],
  "skipped_keys": [ { "key": "EMPTY_ONE", "reason": "empty value" } ],
  "target_file": "/home/node/.config/ark/env",
  "applied_at": "2026-11-10 19:04:31"
}
```

不变量：

1. **只有键名，没有值**；没有口令，没有 `Authorization` 头。
2. `mode` ∈ `diff|apply`；dry-run 也写状态（`diff`），便于审计"谁看过什么"。
3. `changed_keys` 是本次实际写入（或将要写入）的键；已一致的键不出现。
4. `target_file` 用绝对路径，便于回滚脚本直接定位。

## 4. 退出码（跨契约）

| 码 | 名称 | 触发 |
|---|---|---|
| 0 | OK | 成功 |
| 1 | FAILED | 未归类失败 |
| 2 | USAGE | 参数错误、缺配置、缺口令 |
| 3 | NO_SOURCE | 所有来源都不可用 / 产物缺失 / 哈希不匹配 |
| 4 | FORBIDDEN | HTTP 401/403、命中安全守卫 |
| 5 | NETWORK | 超时、连接失败、DNS 失败 |
| 6 | NEEDS_ELEVATION | 需要 root/管理员但不具备 |

上层调度**只依赖退出码**判断是否需要重试：`3` 换来源，`5` 退避重试，`4`/`2` 不重试（人工介入），`6` 提权后重试。
