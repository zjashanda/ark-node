# 让智能体用上 ark-node

本文件给"在节点上跑任务的 agent"一份最小接入范式。三句话：

1. 启动时 `probe` 一次，知道自己能干什么；
2. 干活前 `env --diff` 一次，知道环境是否就绪；
3. 需要文件就走 `fetch`，不要自己拼 scp。

## 启动自检

```bash
set -e
ark-node probe --out "$HOME/.config/ark/last-probe.json"
ark-node identity --json > "$HOME/.config/ark/last-identity.json"
ark-node env --diff || echo "环境有差异，按需 --apply"
```

判断逻辑（伪代码）：

```python
cap = json.load(open("last-probe.json"))
if not cap["tools"]["git"]["present"]:
    raise SystemExit("缺少 git，无法拉取仓库")
if cap["env"]["missing_keys"]:
    print("缺少环境变量:", ",".join(cap["env"]["missing_keys"]))
if cap["resources"]["disk"]["free_gb"] < 5:
    print("磁盘紧张，先清理再下载")
```

## 任务目录

```bash
TASK_DIR=$(ark-node init --task "nightly-report" --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["dir"])')
cd "$TASK_DIR"
```

`init` 幂等：同一 slug 当天重复调用返回同一个目录。产物写进 `artifacts/`，日志写进 `logs/`。

## 取件

```bash
ark-node fetch fetch --name report-template --dest "$TASK_DIR/artifacts" --json
```

**不要**把凭据类文件放进 fetch 通道（守卫会拒绝，返回 4）。这类文件应由部署流程单独下发。

## 回传结果

本仓库不负责归档。需要回传时走归档工具（`ark-archive`），把 `artifacts/` 整个目录交出去，manifest 里带上 `sha256`。

## 退出码约定

```
0 继续
2/4 停下，报给人（参数或凭据问题，重试无用）
3 换来源后重试
5 退避重试（网络）
6 提权后重试
```

## 反模式

- ❌ 在任务里 `git clone` 私有库并硬编码 token → ✅ 用节点级凭据配置，任务里只 `git clone`。
- ❌ 把 `env.json` 的键值打进日志 → ✅ `probe` 只输出键名。
- ❌ 自己 `scp` 绕过台账 → ✅ 走 `fetch`，留 manifest。
- ❌ 用 `date` 生成时间戳 → ✅ 用 `ark-node` 的时间口径（北京时间、无时区）。
