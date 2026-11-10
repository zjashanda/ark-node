# 操作手册

所有命令都在节点本机执行。默认 dry-run，落盘一律显式 `--apply`。

## 日常

### 看这台机器是什么

```bash
ark-node identity            # 人读
ark-node identity --json     # 机器可读
```

`node_id` 优先级：`--node-id` > `$ARK_NODE_ID` > `~/.config/ark/node.json` > hostname 派生（小写、去域名、非法字符换 `-`）。

### 生成能力清单

```bash
ark-node probe --out capability.json
```

字段说明见 `docs/contracts.md`。**只输出环境变量键名，不输出值。**

### 建任务目录

```bash
ark-node init --task node-patrol --dry-run   # 先看
ark-node init --task node-patrol             # 再建
```

目录形如 `~/.ark/tasks/20261110-190431-node-patrol/{logs,artifacts,tmp}`，附 `.ark-task.json`（600）。同名 slug 当天重复执行会**复用**已存在目录，不会叠加。

## 环境变量同步

### 第一次：配置分发源

```bash
mkdir -p ~/.config/ark && chmod 700 ~/.config/ark
cat > ~/.config/ark/env-sources.json <<'JSON'
{ "schema": "ark-env-sources.v1", "candidates": ["https://ark-env.example.com:8443"] }
JSON
chmod 600 ~/.config/ark/env-sources.json
```

多个候选按顺序探 `/health`，第一个通的生效。

### 口令

```bash
printf '%s' "$ARK_HTTP_PASS" | ark-node env --pass-stdin --apply
```

口令落 `~/.config/ark/auth`（600）。**不要**把口令写在命令行（进 shell history 和进程表），也不要写进仓库、issue、聊天。

### 先看差异，再落盘

```bash
ark-node env --diff                 # 只打印 would set: KEY
ark-node env --apply                # 真正写
ark-node env --apply --scope system # Linux 系统级，需 sudo
ark-node env --apply --scope machine # Windows 机器级，需管理员
```

输出末尾一行 `remote version=X local=Y changed=N`。

### 生效

环境变量是**进程启动时快照**：

- Linux 用户级：新开 shell 即生效（`~/.bashrc` 已挂 hook）；当前 shell 执行 `. ~/.config/ark/env`。
- Linux 系统级：新会话生效；systemd 服务需 `systemctl restart`。
- Windows：新进程生效；以 SYSTEM 运行的服务需重启服务。

### 离线导入

```bash
ark-node env --offline ./env-snapshot.json --apply
```

快照结构同分发服务的 `env.json`（扁平等值 JSON，`__version` 记录版本）。

## 取件

```bash
ark-node fetch status --json                       # 各来源可见性
ark-node fetch fetch --name my-artifact --dest ./inbox
ark-node fetch fetch --name my-artifact --sha256 <expected> --dest ./inbox
ark-node fetch index --name my-artifact --project demo --version 1.2.0 \
    --sha256 <digest> --node node-a --path /data/artifacts/my-artifact-1.2.0.zip
ark-node fetch archive --name my-artifact --to-node node-b
```

- 不给 `--sha256` 时也记账，但状态标 `unverified`；给了就必须匹配，否则进 quarantine。
- 失败会写 `fetch-failure-report.json`，逐条列来源与错误，可直接贴给上游排查（已脱敏）。
- 脚本判断用退出码：`3` 换来源、`5` 退避重试、`4`/`2` 人工介入、`6` 提权重试。

## 排障

| 现象 | 先查 | 处理 |
|---|---|---|
| `env` 返回 2 | 是否给了 `--base` 或 `env-sources.json` | 补基址；`curl <base>/health` 验证 |
| `env` 返回 4 | 口令是否正确、auth 文件是否 600 | 重新 `--pass-stdin` 写入 |
| `env` 返回 5 | DNS / 端口 / 代理 | 换候选基址，确认 `/health` 可达 |
| `env` 返回 6 | 当前用户无提权 | 用 root/管理员重跑，**不要**改成 user 作用域绕过 |
| 值改了但程序读不到 | 进程未重启 | 重启进程/服务 |
| `fetch` 返回 3 | 台账与各来源是否都有该 artifact | 指定 `--source`，或先 `index` |
| `fetch` 返回 4 | 文件名是否命中守卫 | 凭据类文件**不允许**通过本通道传输 |

自检一条命令：

```bash
ark-node identity && ark-node probe && ark-node env --diff
```
