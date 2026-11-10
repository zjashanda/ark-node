# 架构

## 问题

节点初始化过去散落在三处：

- **ark-env**：从 HTTP 分发服务拉环境变量，Linux 用 shell、Windows 用 PowerShell，两份实现语义需要人工对齐；
- **ark-mesh**：节点间取件与归档，一份独立的 `ark_fetch.py`，配置里混进了内网拓扑；
- **node-agent**：节点身份、任务目录命名、只读排障输出，各自一套约定。

三者的共同点是「每个节点都要有、每个节点都一样」，差异点只是平台。于是把它们收成一个入口：

```
                    ┌───────────────────────────────┐
                    │          ark-node CLI          │
                    │  env identity init probe fetch │
                    └───────────────┬───────────────┘
                                    │
        ┌───────────────┬───────────┴────────┬────────────────┐
        │               │                    │                │
   envsync.py      identity.py          probe.py          fetch.py
   (HTTP 客户端)   (node_id 解析)      (能力清单)       (artifact 通道)
        │               │                    │                │
        │               └──── initspace.py ──┘                │
        │                    (任务目录)                        │
        └────────────── util.py（时间/退出码/脱敏/原子写/子进程）──────────────┘
                                    │
                     compat/ark-env/（旧 sh/ps1，只作语义参照）
```

## 模块职责

| 模块 | 职责 | 不做什么 |
|---|---|---|
| `util.py` | 时间契约、退出码、原子写、sha256、脱敏、子进程封装 | 不含业务分支 |
| `cli.py` | 参数解析与懒加载分发 | 不含任何业务逻辑 |
| `envsync.py` | 拉取 `/ark/version.json` + `/ark/env.json`，按键 diff，落盘，写状态文件 | 不产生配置值、不管理真源 |
| `identity.py` | node_id 四级优先级解析并缓存 | 不注册节点、不连控制面 |
| `initspace.py` | 任务目录 `YYYYMMDD-HHMMSS-slug` 幂等创建、运行时目录 | 不清理历史任务 |
| `probe.py` | 工具/资源/环境键名探测，输出能力清单 | **绝不输出环境变量值** |
| `fetch.py` | status/index/fetch/archive，SHA-256 校验、quarantine、失败报告 | 不承载业务逻辑、不删除用户数据 |

## 数据流

### 环境同步

```
分发服务                     节点
/ark/version.json  ──┐
/ark/env.json      ──┴─► 解析 → 按键对比本地 ──► 变化集
                                              ├─ dry-run：只打印键名
                                              └─ apply：备份 → 原子写 600
                                                        → 写 ARK_ENV_VERSION
                                                        → 挂 shell hook
                                                        → 写 env-state.json
```

三处收敛点：**值与名分离**（状态文件只记键名）、**只写变化的键**（不覆盖本地手工项）、**每次 apply 先备份**（可回滚）。

### 取件

```
config(mesh-sources.json)
   │  candidates 按 priority 排序
   ▼
本地缓存命中? ──是──► 校验 sha256 ──► 交付
   │否
   ▼
逐来源尝试（本地台账 → relay → cache → node）
   ├─ 成功 → sha256 校验 ─┬─ 匹配 → 写 artifact-manifest.json
   │                      └─ 不匹配 → 移入 quarantine/ + sidecar → NO_SOURCE
   └─ 全部失败 → fetch-failure-report.json（逐条列来源与错误）→ NO_SOURCE / NETWORK
```

`status` 只探活、不改状态；`index` 只写台账；`archive` 只做推送。四者互不隐式触发。

## 平台差异

| 关注点 | Linux / macOS | Windows |
|---|---|---|
| 环境变量作用域 | `user`（`~/.config/ark/env`）/ `system`（`/etc/ark/env`） | `user`（`auth.json` 所在用户）/ `machine`（`[Environment]::SetEnvironmentVariable(...,'Machine')`） |
| 权限 | `chmod 600`，目录 700 | `icacls /inheritance:r` 只授当前用户/管理员 |
| shell hook | `~/.bashrc` 或 `/etc/profile.d/ark-env.sh` | 无（环境变量由系统广播，新进程生效） |
| 提权 | `sudo` 运行 `--scope system` | `machine` 作用域缺管理员直接 `EXIT_NEEDS_ELEVATION`，**不降级** |
| 路径 | `~/.config/ark` | `%USERPROFILE%\.config\ark` |

## 能力探测为什么只输出键名

能力清单会被上传、进入台账、被其他节点读。一旦输出环境变量**值**，任何一次转发都是一次凭据泄漏。因此 `probe` 的 `env` 段只有：

```json
{ "version": "2026-11-09b", "source": "process", "missing_keys": ["NAS_PASSWORD"], "key_count": 10 }
```

`missing_keys` 让上层能判断"这个节点缺什么"，而不知道"它有什么"。
