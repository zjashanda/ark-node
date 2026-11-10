# 回滚

本仓库对节点只做**可逆**改动：写一个环境变量文件、写一个状态文件、挂一行 shell hook、建任务目录。下面每一项都给出回退步骤。

## 0. 总原则

- 每次 `env --apply` 都会先备份：`~/.config/ark/env-backup.<YYYYMMDD-HHMMSS>.bak`（600）。
- 回滚**只动本仓库写过的文件**，不碰系统服务、不碰旧工具、不碰业务数据。
- 任何回滚前先 `ark-node probe --out before.json`，回滚后再导一份 `after.json` 对比。

## 1. 回退环境变量（user 作用域）

```bash
ls -lt ~/.config/ark/env-backup.*.bak    # 找最近一次
cp ~/.config/ark/env-backup.<stamp>.bak ~/.config/ark/env
chmod 600 ~/.config/ark/env
. ~/.config/ark/env                      # 当前 shell 生效
```

Windows：

```powershell
Copy-Item "$env:USERPROFILE\.config\ark\env-backup.<stamp>.bak" "$env:USERPROFILE\.config\ark\env" -Force
```

注意：`~/.config/ark/env` 是给 shell 用的；Windows 用户级变量若已用 `--scope machine` 写入注册表，见第 3 节。

## 2. 回退系统级（Linux `--scope system`）

```bash
sudo cp /etc/ark/env.bak.<stamp> /etc/ark/env     # 若你手工留过
sudo rm -f /etc/profile.d/ark-env.sh              # 移除 hook
```

没有人工备份时，用状态文件确认改过哪些键（`~/.config/ark/env-state.json` 的 `changed_keys`），逐个手工还原后重启相关服务。

## 3. 回退 Windows machine 作用域

```powershell
# 取消某个变量
[Environment]::SetEnvironmentVariable('KEY_NAME','', 'Machine')
# 或恢复旧值（值取自你的备份）
[Environment]::SetEnvironmentVariable('KEY_NAME','<old-value>', 'Machine')
```

需要管理员 PowerShell。改完**重启**以 SYSTEM 运行的服务，新进程才会读到。

## 4. 回退 shell hook

```bash
# 只删本仓库加的那一行，不要清空 .bashrc
sed -i '\|^\[ -f ~/.config/ark/env \] && \. ~/.config/ark/env$|d' ~/.bashrc
```

## 5. 卸载 ark-node 本身

```bash
python3 -m pip uninstall -y ark-node     # 若曾 pip 安装
rm -rf ~/.config/ark/cache ~/.config/ark/spool ~/.config/ark/quarantine
# 保留 ~/.config/ark/{auth,node.json,env,env-state.json} 以免丢失凭据与身份
```

`~/.ark/tasks` 下的历史任务目录**不自动删除**（属于用户数据）。确需清理时先列清单再逐个确认。

## 6. 回退取件带来的产物

- 哈希不匹配的产物在 `~/.config/ark/quarantine/`，同目录有 `.json` sidecar 记录期望/实际摘要与时间。确认无用后手工删除，**本工具不自动删**。
- 已交付的产物如需撤回，按 `artifact-manifest.json` 的 `dest` 定位后手工处理。

## 7. 完全回退到旧工具

旧工具未被修改，直接继续用即可：

```bash
bash ark-env.sh --pass "$P" --apply              # ark-env
python3 ~/.config/ark/ark_fetch.py status        # ark-mesh
```

两者与 `ark-node` 共用同一批目标文件与配置，**可并存**；确认新入口稳定后再决定是否停用旧脚本（停用需单独批准）。

## 8. 回滚自检

```bash
ark-node identity                 # node_id 未变
ark-node env --diff               # 若回退彻底，会显示将要"改回"远端值的键 —— 属正常
ark-node probe --out after.json   # 与 before.json 对比工具/资源字段
```
