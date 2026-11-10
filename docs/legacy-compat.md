# 与旧工具的兼容

本仓库是**新增的 canonical 实现**，不替换、不删除任何旧仓库或旧脚本。旧仓库保持原样，验证通过后只做 tag 冻结或只读引用。

## 1. ark-env

旧实现两份：`compat/ark-env/ark-env.sh`、`compat/ark-env/ark-env.ps1`（此处仅作**语义参照**，不参与运行、不被调用）。

| 能力 | ark-env.sh | ark-env.ps1 | ark-node env |
|---|---|---|---|
| 入口 | `bash ark-env.sh --pass X --apply` | `ark-env.ps1 -Pass X -Apply` | `ark-node env --pass-stdin --apply` |
| 作用域 | `--system` → `/etc/ark/env` | `-Scope Machine` | `--scope user\|system\|machine` |
| 口令来源 | `--pass` 或 `~/.config/ark/auth` | `-Pass` → `$env:ARK_HTTP_PASS` → `auth.json` | stdin / `$ARK_HTTP_PASS` / `auth` 文件 |
| 基址探测 | 依次探 `/health` | 同 | 同，候选来自 `~/.config/ark/env-sources.json` |
| 差异输出 | `would set: KEY` | 同 | 同 |
| 版本记录 | `ARK_ENV_VERSION` | 同 | 同 |
| 状态留痕 | 无 | 无 | 新增 `ark-node-env-state.json`（只记键名） |
| 备份 | 无 | 无 | 新增 `env-backup.<stamp>.bak` |

**行为差异（有意为之）**

1. **口令不再作为命令行参数首推**：`--pass` 会进 shell history 和进程表。新入口用 `--pass-stdin`、`$ARK_HTTP_PASS` 或 auth 文件；为兼容旧脚本仍接受 `-Pass` 等价的显式传值，但不写入任何日志。
2. **只写变化的键，且保留其他行**：旧 sh 用 `grep -v` 删除再追加，会打乱顺序并丢掉注释行；新实现按原有顺序原地更新。
3. **每次 apply 先备份**：旧实现直接覆盖，无法回滚。
4. **machine 作用域不降级**：旧 ps1 在非管理员时 `throw`；新实现返回退出码 `6`（`NEEDS_ELEVATION`），语义一致但可被调度器识别。
5. **状态文件只记键名**：旧实现无留痕，新实现可以让"这台机器什么时候同步过、改了哪几个键"可审计，同时不落值。

**迁移**

```bash
# 旧
bash ark-env.sh --pass "$P" --apply
# 新（等价）
printf '%s' "$P" | ark-node env --pass-stdin --apply
```

两者写同一个目标文件（`~/.config/ark/env`），**可共存、可回退**：新入口只改变化的键，旧脚本看到的结果是幂等的。

## 2. ark-mesh

旧实现：`ark_fetch.py`（778 行，标准库），配置 `~/.config/ark/mesh-sources.json`。

| 能力 | ark_fetch.py | ark-node fetch |
|---|---|---|
| 子命令 | `status / index / fetch / archive` | 相同 |
| 全局参数 | `--config` | 相同（另有 `ARK_MESH_CONFIG`） |
| 配置文件 | `~/.config/ark/mesh-sources.json` | 相同，**schema 不变** |
| 台账文件 | `artifact-manifest.json` | 相同 |
| 失败报告 | `fetch-failure-report.json` | 相同 |
| 退出码 | 0/2/3/4 | 增加 5（NETWORK），沿用 0/2/3/4 语义 |
| 哈希不匹配 | 报错 | **新增** quarantine + sidecar，不删文件 |
| 时间字段 | 混合口径 | 统一北京时间无时区 |
| JSON 落盘前 | 原样 | 统一 `redact_obj` |

**配置无需改动即可复用**：把旧 `mesh-sources.json` 原样放到 `~/.config/ark/mesh-sources.json`，`ark-node fetch status --json` 应得到与旧工具一致的节点视图。

**注意**：该配置含真实内网拓扑与机器名，**永远不进入本仓库**，也不得贴进 issue、日志或文档。仓库只提供 `config/mesh-sources.example.json`（`example.com` 占位）。

**迁移**

```bash
# 旧
python3 ~/.config/ark/ark_fetch.py fetch --name X --dest ./inbox
# 新
ark-node fetch fetch --name X --dest ./inbox
```

建议做法：把旧脚本包一层 alias 指向新入口，观察一到两个交付周期后再下线旧脚本（下线需单独批准）。

## 3. node-agent

节点 agent 本体（安装、注册、控制面长连接）**不在本仓库**，仍在部署仓库。本仓库接手的是它周边三件"每台机器都要有"的事：

| 旧位置 | 旧做法 | 新入口 |
|---|---|---|
| `node.sh --node-id X` | 安装时写入 `/etc/noahsark/node.env` | `ark-node identity --node-id X`（写 `~/.config/ark/node.json`，600） |
| 任务目录 `YYYYMMDD-HHMMSS-slug` | 各脚本自行拼 | `ark-node init --task slug`（幂等、可 dry-run） |
| `node-inventory/node-agent.sh --status` | 自定义只读输出 | `ark-node probe`（结构化能力清单，字段固定） |

**边界**：`identity` 不注册节点、不连控制面、不写 `/etc/noahsark/node.env`；`probe` 只读，不改任何系统状态。控制面注册仍由部署流程负责。

**兼容旧输出**：`probe` 的 `node.node_id` 与 `ark-node identity` 的 `node_id` 同源，二者与旧 `node-inventory` 的 `node_id` 取值一致（都遵循同一套 hostname 派生规则 + 显式覆盖优先级）。

## 4. 不做的事

- 不删除旧仓库、不改旧仓库 `main`、不开迁移分支；
- 不修改任何节点的现有配置（本仓库只提供客户端，运行与否由节点侧决定）；
- 不把凭据、内网地址、机器名写进本仓库任何文件；
- 不合并 `venusahisiburner` 等业务 skill 的逻辑——它们依赖 `ark-mesh` 的**接口**，接口在这里稳定即可。
